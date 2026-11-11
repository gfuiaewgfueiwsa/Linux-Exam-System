# -*- coding: utf-8 -*-
"""term_bridge —— 浏览器 xterm.js 与真实 Linux 虚拟机 PTY 的双向桥接。

两种工作模式：
* TermBridge：练习会话使用，一个 WebSocket 对应一个短生命周期终端。
* TerminalWorker：考试会话使用，每个 sid 只保留一个常驻 shell 和读线程，
  WebSocket 断开/重连只切换订阅者，不会重复创建 shell、不会多个线程竞争
  读取 stdout，也不会因为刷新页面丢失 cwd/环境。

* 二进制帧：原始终端字节流
* 文本帧：JSON 控制/状态消息（resize、心跳、考试成绩推送）
"""

from __future__ import annotations

import json
import re
import threading
import time

from lab_pool import POOL
from real_sim import strip_ansi

PASSWORD_HINT = re.compile(r"(?i)(password|密码)[^\n:]*:\s*$")
ESC_SEQ = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]|\x1b[OPQRS]|\x1b\[[0-9;]*$")
PROMPT_TAIL = re.compile(r"^[\w.-]+@[\w.-]+:.*[$#]\s*$")


def strip_prompt(text):
    """去掉输出末尾的 shell 提示符，避免提示符里的用户名污染评分判定。"""
    lines = text.splitlines()
    while lines and PROMPT_TAIL.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines)


class TermBridge:
    def __init__(self, ws, vm, session=None, on_update=None, on_activity=None,
                 rows=32, cols=110):
        self.ws = ws
        self.vm = vm
        self.session = session
        self.on_update = on_update
        self.on_activity = on_activity
        self.proc = None
        self.in_buf = bytearray()
        self.out_buf = bytearray()
        self.pending = None          # (command, secret)
        self._secret_cmd = None      # 进入密码输入模式前的原始命令
        self.secret_mode = False
        self._esc = 0                # ESC 转义序列状态（方向键等）：0 无 / 1 ESC / 2 序列体
        self.last_out = time.time()
        self.stopped = threading.Event()
        self.size = (max(12, min(80, int(rows))), max(40, min(300, int(cols))))
        self._last_resize = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 主循环
    def run(self):
        try:
            self.proc = self.vm.open_shell(self.size[0], self.size[1])
        except Exception as exc:               # noqa: BLE001
            self.ws.send_text(json.dumps({"type": "error", "message": f"终端启动失败：{exc}"},
                                         ensure_ascii=False))
            self.ws.close()
            return
        head = self.vm.read_tty(8.0)
        if head:
            self.ws.send_bytes(head.encode("utf-8", "replace"))
        threading.Thread(target=self._pump_output, daemon=True).start()
        threading.Thread(target=self._checker, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        try:
            while not self.stopped.is_set():
                try:
                    frame = self.ws.recv()
                except (ConnectionError, OSError):
                    break
                if frame is None:
                    if self.ws.closed:
                        break
                    continue
                opcode, payload = frame
                if opcode == 8:
                    break
                if opcode == 1:
                    self._control(payload)
                    continue
                self._feed(payload)
        except (ConnectionError, OSError):
            pass
        finally:
            self.close()

    # ------------------------------------------------------------------ 输入
    def _feed(self, payload: bytes):
        with self._lock:
            if self.proc.poll() is not None:
                return
            try:
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.stopped.set()
                return
            for byte in payload:
                if self._esc:
                    # 方向键/Home/End 形如 ESC [ … 终止字节；整段吞掉，否则回看
                    # 历史命令后回车，镜像出来的“命令”会是 "[A" 之类乱码。
                    if self._esc == 1:
                        self._esc = 2 if byte in (0x5B, 0x4F) else 0
                    elif 0x40 <= byte <= 0x7E:
                        self._esc = 0
                    continue
                if byte in (13, 10):
                    text = self.in_buf.decode("utf-8", "ignore")
                    self.in_buf.clear()
                    cmd = text.strip()
                    self.pending = (cmd, self.secret_mode) if cmd else None
                    self.secret_mode = False
                    self.out_buf.clear()
                    self.last_out = time.time()
                elif byte in (8, 127):
                    if self.in_buf:
                        self.in_buf.pop()
                elif byte == 27:
                    self._esc = 1
                elif byte in (3, 21):
                    self.in_buf.clear()
                elif byte < 32 and byte != 9:
                    # Ctrl+L 等只重绘屏幕，不属于命令行内容
                    continue
                else:
                    if len(self.in_buf) < 4096:
                        self.in_buf.append(byte)

    def _control(self, payload: bytes):
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        kind = msg.get("type")
        if kind == "ping":
            self.ws.send_text(json.dumps({"type": "pong"}))
            return
        if kind == "resize":
            try:
                rows = max(8, min(int(msg.get("rows") or 32), 120))
                cols = max(20, min(int(msg.get("cols") or 110), 400))
            except (TypeError, ValueError):
                return
            if (rows, cols) == self.size:
                return
            if time.time() - self._last_resize < 0.5:
                return
            self.size = (rows, cols)
            self._last_resize = time.time()
            threading.Thread(target=self.vm.set_winsize, args=(rows, cols),
                             daemon=True).start()

    # ------------------------------------------------------------------ 保活
    def _heartbeat(self):
        """周期性发送协议层 ping，让 WebSocket 连接持续有数据流动。

        前端应用层心跳依赖 JS setInterval，浏览器切换到后台标签页时会被节流，
        导致云隧道（Cloudflare）误判连接空闲而断开。协议层 ping 由浏览器在底层
        自动回复 pong，不经过页面 JS，不受节流影响，可稳定保持空闲连接。
        """
        while not self.stopped.is_set():
            self.stopped.wait(15.0)
            if self.stopped.is_set():
                break
            try:
                self.ws.ping()
            except Exception:
                pass

    # ------------------------------------------------------------------ 输出
    def _pump_output(self):
        try:
            while not self.stopped.is_set():
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                self.ws.send_bytes(chunk)
                with self._lock:
                    self.out_buf.extend(chunk)
                    if len(self.out_buf) > 200000:
                        self.out_buf = self.out_buf[-100000:]
                    self.last_out = time.time()
        except (OSError, ValueError):
            pass
        self.stopped.set()
        try:
            self.ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ 判定
    def _checker(self):
        while not self.stopped.is_set():
            time.sleep(0.15)
            if self.on_activity:
                try:
                    self.on_activity()
                except Exception:
                    pass
            if self.session is None:
                continue
            with self._lock:
                pending = self.pending
                quiet = time.time() - self.last_out
                if pending is None or quiet < 0.35:
                    continue
                self.pending = None
                output = strip_prompt(strip_ansi(self.out_buf.decode("utf-8", "ignore")))[-6000:]
                secret = pending[1]
                tail = output[-200:]
            if secret:
                still_hint = bool(PASSWORD_HINT.search(tail))
                self.secret_mode = still_hint
                if still_hint:
                    continue
                cmd = self._secret_cmd
                self._secret_cmd = None
                if not cmd:
                    continue
            elif PASSWORD_HINT.search(tail):
                self._secret_cmd = pending[0]
                self.secret_mode = True
                continue
            else:
                cmd = pending[0]
            sim = self.session.sim
            if sim is not None and getattr(sim, "is_real", False):
                sim.note_command(cmd, output)
            result = self.session.submit_line(cmd, output)
            if self.on_update:
                try:
                    self.on_update(self.session, result)
                except Exception:
                    pass
            self._push_state(result)

    def _push_state(self, result):
        sess = self.session
        payload = {
            "type": "state",
            "passed": result.get("passed", False),
            "passed_key": result.get("passed_key"),
            "score": sess.score(), "progress": sess.progress(),
            "remaining": sess.remaining(), "finished": sess.finished,
            "selected": sess.public_task(sess.selected) if sess.selected else None,
            "records": [r["key"] for r in sess.records],
        }
        try:
            self.ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    # ------------------------------------------------------------------ 收尾
    def close(self):
        self.stopped.set()
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        try:
            self.ws.close()
        except Exception:
            pass


# ====================================================================== 考试常驻终端
class TerminalWorker:
    """一个考试会话的常驻终端。

    * 只创建一个 shell，输出由一个读线程持续读取并写入环形缓冲。
    * WebSocket 只是“订阅者”：断开时仅解除订阅，shell/评分线程继续运行。
    * 启动虚拟机期间持续推送状态，前端不会因为长时间收不到心跳而断开。
    """

    def __init__(self, key, session=None, on_update=None, on_activity=None):
        self.key = key
        self.sid = key[len("exam:"):] if key.startswith("exam:") else key
        self.session = session
        self.on_update = on_update
        self.on_activity = on_activity
        self.vm = None
        self.proc = None
        self.ready = False
        self.closed = False
        self.stopped = threading.Event()
        self.ws = None
        self.ws_lock = threading.Lock()
        self._lock = threading.RLock()
        self.size = (32, 110)
        self.in_buf = bytearray()
        self.out_buf = bytearray()
        self.pending = None
        self._secret_cmd = None
        self.secret_mode = False
        self._esc = 0                # ESC 转义序列状态（方向键等）：0 无 / 1 ESC / 2 序列体
        self.last_out = time.time()
        self._last_resize = 0.0
        self._status = "正在准备 Linux 环境…"
        self._status_since = time.time()
        self._start_ts = time.time()
        self._pump_gen = 0
        self._reopening = False
        self._runner_active = False
        self._runner_lock = threading.Lock()
        self._checker_started = False
        threading.Thread(target=self._runner, daemon=True).start()
        threading.Thread(target=self._status_loop, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    # ---------------------------------------------------------------- 生命周期
    def bind(self, session=None, on_update=None, on_activity=None):
        if session is not None:
            self.session = session
        if on_update is not None:
            self.on_update = on_update
        if on_activity is not None:
            self.on_activity = on_activity

    def _set_status(self, message):
        with self._lock:
            if message != self._status:
                self._status = message
                self._status_since = time.time()
                changed = True
            else:
                changed = False
        if changed:
            self._send_json({"type": "status", "message": message})

    def _runner(self):
        with self._runner_lock:
            if self._runner_active:
                return
            self._runner_active = True
        try:
            attempts = 0
            while not self.closed:
                self._set_status("正在准备 Linux 环境…")
                try:
                    vm = POOL.acquire(self.key, timeout=420)
                except Exception as exc:      # noqa: BLE001
                    attempts += 1
                    if self.closed:
                        return
                    self._set_status(f"Linux 环境启动失败，正在重试：{exc}")
                    if attempts >= 4:
                        self._set_status("Linux 环境启动失败，请点击“重新连接”")
                        return
                    time.sleep(min(10, 2 * attempts))
                    continue
                if self.closed:
                    # 交卷/删除记录时 Worker 可能在 acquire 途中被关闭；
                    # 若此时虚拟机已启动，必须同步回收，避免变成无人使用的孤儿进程。
                    try:
                        POOL.release(self.key, reset=True)
                    except Exception:
                        pass
                    return
                self.vm = vm
                if self.session is not None:
                    try:
                        self.session.sim.vm = vm
                        self.session.sim.refresh()
                    except Exception:
                        pass
                self._open_shell(initial=True)
                return
        finally:
            with self._runner_lock:
                self._runner_active = False

    def _open_shell(self, initial=False):
        if self.closed or self.vm is None:
            return
        self._set_status("正在连接终端…")
        rows, cols = self.size
        try:
            proc = self.vm.open_shell(rows, cols)
        except Exception as exc:          # noqa: BLE001
            self._set_status(f"终端连接失败：{exc}")
            if initial:
                threading.Timer(2.0, self._runner).start()
            return
        try:
            head = self.vm.read_tty(8.0)
        except Exception:
            head = ""
        if head:
            self._emit(head.encode("utf-8", "replace"))
        with self._lock:
            self.proc = proc
            self.ready = True
            self._pump_gen += 1
            gen = self._pump_gen
        self._set_status("")
        self._send_json({"type": "machine", "id": getattr(self.vm, "label", "")})
        if self.session is not None:
            self._push_state({}, force=True)
        threading.Thread(target=self._pump_output, args=(gen,), daemon=True).start()
        if initial:
            with self._lock:
                if self._checker_started:
                    return
                self._checker_started = True
            threading.Thread(target=self._checker, daemon=True).start()

    def _pump_output(self, gen):
        proc = self.proc
        while not self.closed and gen == self._pump_gen and proc is not None:
            try:
                chunk = proc.stdout.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._lock:
                self.out_buf.extend(chunk)
                if len(self.out_buf) > 400000:
                    self.out_buf = self.out_buf[-200000:]
                self.last_out = time.time()
            self._emit(chunk)
        if self.closed or gen != self._pump_gen:
            return
        self.ready = False
        self._set_status("终端连接中断，正在自动恢复…")
        self._schedule_reopen()

    def _schedule_reopen(self):
        with self._lock:
            if self._reopening or self.closed:
                return
            self._reopening = True
        threading.Thread(target=self._reopen_shell, daemon=True).start()

    def _reopen_shell(self):
        try:
            for attempt in range(1, 6):
                if self.closed or self.vm is None:
                    return
                time.sleep(1.5 * attempt)
                try:
                    self.vm.detach()
                    self._open_shell(initial=False)
                    if self.ready:
                        return
                except Exception:
                    pass
            self._set_status("终端恢复失败，正在重新申请 Linux 环境…")
        finally:
            with self._lock:
                self._reopening = False
        if not self.closed and not self.ready:
            # 虚拟机可能已经崩溃；交给 _runner 重新 acquire（会沿用持久镜像）
            self.vm = None
            threading.Timer(2.0, self._runner).start()

    def _emit(self, chunk):
        with self.ws_lock:
            ws = self.ws
        if ws is None:
            return
        if not ws.send_bytes(chunk):
            with self.ws_lock:
                if self.ws is ws:
                    self.ws = None

    # ---------------------------------------------------------------- WS 订阅
    def attach(self, ws, rows=32, cols=110):
        self.size = (max(12, min(80, int(rows))), max(40, min(300, int(cols))))
        with self.ws_lock:
            old = self.ws
            self.ws = ws
        if old is not None and old is not ws:
            try:
                old.close()
            except Exception:
                pass
        with self._lock:
            status = self._status
        if status:
            ws.send_text(json.dumps({"type": "status", "message": status},
                                    ensure_ascii=False))
        if self.ready:
            self._send_json({"type": "machine", "id": getattr(self.vm, "label", "")})
            if self.session is not None:
                self._push_state({}, force=True)
            # 重新挂接后按一次回车，让 bash 重绘提示符（空命令不参与评分）
            with self._lock:
                proc = self.proc
            try:
                if proc is not None and proc.poll() is None:
                    proc.stdin.write(b"\r")
                    proc.stdin.flush()
            except Exception:
                pass
            if self.vm is not None:
                try:
                    threading.Thread(target=self.vm.set_winsize,
                                     args=(self.size[0], self.size[1]),
                                     daemon=True).start()
                except Exception:
                    pass

    def detach(self, ws):
        with self.ws_lock:
            if self.ws is ws:
                self.ws = None

    def serve_ws(self, ws):
        """在当前 HTTP 线程中消费该 WebSocket，直到连接断开。"""
        while not self.closed:
            with self.ws_lock:
                if self.ws is not ws:
                    break
            try:
                frame = ws.recv()
            except (ConnectionError, OSError):
                break
            if frame is None:
                if ws.closed:
                    break
                # 90 秒没有收到任何字节（浏览器协议层 pong 每 15 秒一次）
                if time.time() - ws.last_recv > 90:
                    print(f"[term] 会话 {self.sid} 连接 90 秒无数据，判定为死连接")
                    break
                continue
            opcode, payload = frame
            if opcode == 8:
                break
            if opcode == 1:
                self._control(payload, ws)
            else:
                self._feed(payload)
        self.detach(ws)
        try:
            ws.close()
        except Exception:
            pass

    def _send_json(self, payload):
        with self.ws_lock:
            ws = self.ws
        if ws is None:
            return
        if not ws.send_text(json.dumps(payload, ensure_ascii=False)):
            with self.ws_lock:
                if self.ws is ws:
                    self.ws = None

    # ---------------------------------------------------------------- 输入
    def _feed(self, payload: bytes):
        with self._lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.ready = False
                return
            for byte in payload:
                if self._esc:
                    # 方向键/Home/End 形如 ESC [ … 终止字节；整段吞掉，否则回看
                    # 历史命令后回车，镜像出来的“命令”会是 "[A" 之类乱码。
                    if self._esc == 1:
                        self._esc = 2 if byte in (0x5B, 0x4F) else 0
                    elif 0x40 <= byte <= 0x7E:
                        self._esc = 0
                    continue
                if byte in (13, 10):
                    text = self.in_buf.decode("utf-8", "ignore")
                    self.in_buf.clear()
                    cmd = text.strip()
                    self.pending = (cmd, self.secret_mode) if cmd else None
                    self.secret_mode = False
                    self.out_buf.clear()
                    self.last_out = time.time()
                elif byte in (8, 127):
                    if self.in_buf:
                        self.in_buf.pop()
                elif byte == 27:
                    self._esc = 1
                elif byte in (3, 21):
                    self.in_buf.clear()
                elif byte < 32 and byte != 9:
                    # Ctrl+L 等只重绘屏幕，不属于命令行内容
                    continue
                else:
                    if len(self.in_buf) < 4096:
                        self.in_buf.append(byte)

    def _control(self, payload: bytes, ws):
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        kind = msg.get("type")
        if kind == "ping":
            ws.send_text(json.dumps({"type": "pong"}))
            return
        if kind == "resize":
            try:
                rows = max(8, min(int(msg.get("rows") or 32), 120))
                cols = max(20, min(int(msg.get("cols") or 110), 400))
            except (TypeError, ValueError):
                return
            if (rows, cols) == self.size:
                return
            if time.time() - self._last_resize < 0.5:
                return
            self.size = (rows, cols)
            self._last_resize = time.time()
            if self.vm is not None:
                threading.Thread(target=self.vm.set_winsize, args=(rows, cols),
                                 daemon=True).start()

    # ---------------------------------------------------------------- 保活
    def _status_loop(self):
        """启动期间每 3 秒推送一次状态，避免前端长时间收不到任何消息。"""
        while not self.stopped.is_set():
            self.stopped.wait(3.0)
            if self.stopped.is_set() or self.ready:
                continue
            with self._lock:
                status = self._status
                since = self._status_since
            if not status:
                continue
            waited = int(time.time() - since)
            message = status if waited < 5 else f"{status}（已等待 {waited} 秒）"
            self._send_json({"type": "status", "message": message})

    def _heartbeat(self):
        while not self.stopped.is_set():
            self.stopped.wait(15.0)
            if self.stopped.is_set():
                break
            with self.ws_lock:
                ws = self.ws
            if ws is not None:
                try:
                    ws.ping()
                except Exception:
                    pass

    # ---------------------------------------------------------------- 评分
    def _checker(self):
        while not self.stopped.is_set():
            time.sleep(0.15)
            if self.on_activity:
                try:
                    self.on_activity()
                except Exception:
                    pass
            if self.session is None:
                continue
            with self._lock:
                pending = self.pending
                quiet = time.time() - self.last_out
                if pending is None or quiet < 0.35:
                    continue
                self.pending = None
                output = strip_prompt(strip_ansi(self.out_buf.decode("utf-8", "ignore")))[-6000:]
                secret = pending[1]
                tail = output[-200:]
            if secret:
                still_hint = bool(PASSWORD_HINT.search(tail))
                self.secret_mode = still_hint
                if still_hint:
                    continue
                cmd = self._secret_cmd
                self._secret_cmd = None
                if not cmd:
                    continue
            elif PASSWORD_HINT.search(tail):
                self._secret_cmd = pending[0]
                self.secret_mode = True
                continue
            else:
                cmd = pending[0]
            sim = self.session.sim
            if sim is not None and getattr(sim, "is_real", False):
                sim.note_command(cmd, output)
            result = self.session.submit_line(cmd, output)
            if self.on_update:
                try:
                    self.on_update(self.session, result)
                except Exception:
                    pass
            self._push_state(result)

    def _push_state(self, result, force=False):
        sess = self.session
        if sess is None:
            return
        with self.ws_lock:
            has_ws = self.ws is not None
        if not has_ws and not force:
            return
        payload = {
            "type": "state",
            "passed": result.get("passed", False),
            "passed_key": result.get("passed_key"),
            "score": sess.score(), "progress": sess.progress(),
            "remaining": sess.remaining(), "finished": sess.finished,
            "selected": sess.public_task(sess.selected) if sess.selected else None,
            "records": [r["key"] for r in sess.records],
        }
        self._send_json(payload)

    # ---------------------------------------------------------------- 收尾
    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stopped.set()
        with self.ws_lock:
            ws = self.ws
            self.ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        with self._lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


# ====================================================================== 注册表
_WORKERS = {}
_WORKERS_LOCK = threading.Lock()


def get_worker(key, session=None, on_update=None, on_activity=None):
    """取得（或创建）某考试会话的常驻终端。"""
    with _WORKERS_LOCK:
        worker = _WORKERS.get(key)
        if worker is None or worker.closed:
            worker = TerminalWorker(key, session=session, on_update=on_update,
                                    on_activity=on_activity)
            _WORKERS[key] = worker
        else:
            worker.bind(session, on_update, on_activity)
        return worker


def close_worker(key):
    """交卷/清除记录时彻底关闭该会话的常驻终端。"""
    with _WORKERS_LOCK:
        worker = _WORKERS.pop(key, None)
    if worker is not None:
        worker.close()


def worker_state():
    with _WORKERS_LOCK:
        return {"count": len(_WORKERS),
                "sids": sorted(k[len("exam:"):] for k in _WORKERS)}
