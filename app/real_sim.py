# -*- coding: utf-8 -*-
"""real_sim —— 把真实 Linux 虚拟机的状态映射成 exam_flow 评分所需的对象。

exam_flow 里的 87 个检查函数只依赖一组属性：
    last_input / last_names / last_output / cwd / user / stack
    users / groups / _node_at / _node_or_link / _read_file / vim / less_views / clear_flag
本模块用真实系统快照（/etc/passwd、/etc/shadow、/etc/group、文件 stat 树、前台 shell 的
/proc/<pid>/cwd 与 uid）+ 终端输入流镜像来提供这些属性，因此评分代码无需重写。
"""

from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|\r")


def strip_ansi(text):
    return ANSI_RE.sub("", text or "")


class Node:
    """模拟引擎里的文件节点（真实文件系统快照映射）。"""

    __slots__ = ("path", "kind", "mode", "owner", "group", "content", "target", "size")

    def __init__(self, path, raw):
        self.path = path
        self.kind = raw.get("kind", "file")
        self.mode = int(raw.get("mode", 0))
        self.owner = raw.get("owner", "")
        self.group = raw.get("group", "")
        self.content = raw.get("content")
        self.target = raw.get("target")
        self.size = raw.get("size", 0)

    def __repr__(self):
        return f"<Node {self.path} {self.kind} {self.mode:o}>"


class RealSim:
    """LinuxSim 兼容层（后端是 QemuVM 快照）。"""

    is_real = True

    def __init__(self, vm):
        self.vm = vm
        self.history = []
        self.last_input = ""
        self.last_names = []
        self.last_output = ""
        self.cwd = "/home/student"
        self.user = "student"
        self.stack = []
        self.users = {}
        self.groups = {}
        self.nodes = {}
        self.vim = None
        self.less_views = 0
        self.clear_flag = False
        self.secret_prompt = False
        self._last_snapshot = {}

    # ---------------------------------------------------------------- 快照
    def refresh(self):
        if self.vm is None:
            return False
        data = self.vm.snapshot()
        if not data:
            return False
        self._last_snapshot = data
        self.users = data.get("users", {}) or {}
        self.groups = data.get("groups", {}) or {}
        self.nodes = {p: Node(p, raw) for p, raw in (data.get("nodes") or {}).items()}
        shell = data.get("shell") or {}
        if shell.get("user"):
            self.user = shell["user"]
        if shell.get("cwd"):
            self.cwd = shell["cwd"]
        self.secret_prompt = False
        return True

    # ---------------------------------------------------------------- 输入镜像
    def note_command(self, line, output_text):
        """记录一条学生实际输入过的命令（由终端桥接层调用）。"""
        line = (line or "").strip()
        prev_user, prev_cwd = self.user, self.cwd
        self.last_input = line
        self.last_names = line.split()
        self.last_output = strip_ansi(output_text or "")
        self.clear_flag = line == "clear"
        if line:
            self.history.append(line)
            self.history = self.history[-500:]
            if re.search(r"(^|\|\s*)less\b", line):
                self.less_views += 1
        self._track_identity(line, prev_user, prev_cwd)
        self.refresh()

    def _track_identity(self, line, prev_user, prev_cwd):
        names = line.split()
        if not names:
            return
        head = names[0]
        if head == "sudo" and "su" in names[1:]:
            self.stack.append((prev_user, prev_cwd))
            return
        if head == "su":
            args = [n for n in names[1:] if not n.startswith("-")]
            target = args[0] if args else "root"
            if target != prev_user:
                self.stack.append((prev_user, prev_cwd))
            return
        if head in ("exit", "logout"):
            if self.stack:
                self.stack.pop()
            return

    # ---------------------------------------------------------------- 兼容接口
    def feed(self, line):
        """HTTP 兼容入口：直接在虚拟机里执行命令并返回输出。"""
        if not line.strip():
            return ""
        code, out = self.vm.exec_root(f"su - student -c {_quote(line)}", timeout=60)
        self.note_command(line, out)
        return out

    def _node_at(self, path):
        node = self.nodes.get(path)
        if node is None and path:
            alt = path.rstrip("/") or "/"
            node = self.nodes.get(alt)
        return node

    def _node_or_link(self, path):
        return self._node_at(path)

    def _read_file(self, node):
        if node is None:
            return ""
        return node.content or ""

    def prompt(self):
        home = "/root" if self.user == "root" else f"/home/{self.user}"
        cwd = self.cwd or home
        shown = "~" if cwd == home else cwd
        suffix = "#" if self.user == "root" else "$"
        return f"{self.user}@exam-host:{shown}{suffix}"

    def mode_badge(self):
        return ""

    # ---------------------------------------------------------------- 存档
    def dump(self):
        port = self.vm.port if self.vm is not None else 0
        tty = self.vm.tty if self.vm is not None else ""
        return {"real": True, "port": port, "tty": tty,
                "user": self.user, "cwd": self.cwd,
                "stack": [list(x) for x in self.stack]}

    @classmethod
    def restore(cls, data, vm):
        obj = cls(vm)
        obj.user = data.get("user", "student")
        obj.cwd = data.get("cwd", "/home/student")
        obj.stack = [tuple(x) for x in data.get("stack") or []]
        obj.refresh()
        return obj


def _quote(text):
    return "'" + str(text).replace("'", "'\"'\"'") + "'"
