# -*- coding: utf-8 -*-
"""wsproto —— 标准库实现的极简 WebSocket（RFC6455）。

只服务本项目的一个场景：浏览器 xterm.js <-> 后端 PTY 双向字节流。
支持：文本/二进制帧、分片、ping/pong、close。

实现要点：
* 直接基于 socket 维护接收缓冲，读超时不会丢失半帧数据（旧实现用
  rfile.read 超时会把已读到的帧头/载荷丢掉，导致后续帧全部错位）。
* recv() 在无完整帧时返回 None，调用方可借此周期性检查连接存活状态。
* last_recv 记录最后一次收到任何字节的时间（含浏览器自动回复的 pong），
  供上层看门狗判断死连接。
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 4 * 1024 * 1024
RECV_POLL = 5.0          # recv 每次最长阻塞时间（秒）


class WebSocket:
    def __init__(self, sock):
        self.sock = sock
        self._send_lock = threading.Lock()
        self.closed = False
        self._buf = bytearray()
        self._frag_opcode = None
        self._frag_buf = bytearray()
        self.last_recv = time.time()
        self.last_send = time.time()

    # ------------------------------------------------------------ 握手
    @classmethod
    def accept(cls, handler):
        key = handler.headers.get("Sec-WebSocket-Key")
        if not key:
            return None
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        handler.send_response_only(101, "Switching Protocols")
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
        handler.wfile.flush()
        sock = handler.connection
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        # 读超时只用于让 recv 周期性返回 None，缓冲在类内部，不会破坏帧边界
        try:
            sock.settimeout(RECV_POLL)
        except OSError:
            pass
        return cls(sock)

    # ------------------------------------------------------------ 收帧
    def _take_frame(self):
        """从缓冲区取出一个完整帧，返回 (fin, opcode, payload) 或 None。"""
        buf = self._buf
        if len(buf) < 2:
            return None
        b1, b2 = buf[0], buf[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        off = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = struct.unpack(">H", bytes(buf[2:4]))[0]
            off = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = struct.unpack(">Q", bytes(buf[2:10]))[0]
            off = 10
        if length > MAX_FRAME:
            raise ConnectionError("frame too large")
        if masked:
            if len(buf) < off + 4:
                return None
            mask = bytes(buf[off:off + 4])
            off += 4
        else:
            mask = b""
        if len(buf) < off + length:
            return None
        payload = bytes(buf[off:off + length])
        del buf[:off + length]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self):
        """返回 (opcode, payload)；超时无完整帧返回 None；opcode=8 表示关闭。"""
        while True:
            frame = self._take_frame()
            if frame is not None:
                fin, opcode, payload = frame
                if opcode == 0x9:          # ping -> 立即回 pong
                    self.send(0xA, payload)
                    continue
                if opcode == 0xA:          # pong
                    continue
                if opcode == 0x8:
                    return 8, payload
                if opcode == 0x0:          # 续帧
                    self._frag_buf.extend(payload)
                    if fin:
                        data = bytes(self._frag_buf)
                        op = self._frag_opcode or 1
                        self._frag_buf.clear()
                        self._frag_opcode = None
                        return op, data
                    continue
                if not fin:
                    self._frag_opcode = opcode
                    self._frag_buf = bytearray(payload)
                    continue
                return opcode, payload
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError as exc:
                self.closed = True
                raise ConnectionError(str(exc))
            if not chunk:
                self.closed = True
                raise ConnectionError("closed")
            self.last_recv = time.time()
            self._buf.extend(chunk)

    # ------------------------------------------------------------ 发帧
    def send(self, opcode, payload: bytes):
        if self.closed:
            return False
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        with self._send_lock:
            if self.closed:
                return False
            try:
                self.sock.sendall(bytes(header) + payload)
                self.last_send = time.time()
                return True
            except OSError:
                self.closed = True
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return False

    def ping(self, payload: bytes = b""):
        """发送 WebSocket 协议层 ping（opcode 0x9）。

        浏览器会在协议层自动回复 pong，不依赖页面 JS 定时器，因此不受后台标签页
        节流影响；用于让 Cloudflare 隧道上的空闲连接持续有数据流动，避免被判定
        为空闲而断开。
        """
        return self.send(0x9, payload)

    def send_text(self, text: str):
        return self.send(0x1, text.encode("utf-8"))

    def send_bytes(self, data: bytes):
        return self.send(0x2, data)

    def close(self):
        if self.closed:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        try:
            self.send(0x8, b"\x03\xe8")
        finally:
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
