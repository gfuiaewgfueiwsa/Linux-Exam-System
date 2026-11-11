# -*- coding: utf-8 -*-
"""linux_engine —— 项目目录内的 Linux 沙箱。

模拟一台 Linux 主机：多用户、用户组、文件系统、文件权限、
管道、vim/less 交互，以及 PPT 里出现的全部命令。
状态可序列化，供考试系统逐步校验。
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import shlex
import time
from datetime import datetime


# ----------------------------------------------------------------------------
# 文件系统节点
# ----------------------------------------------------------------------------
class VNode:
    __slots__ = ("name", "kind", "owner", "group", "mode", "content", "children",
                 "target", "mtime", "parent")

    def __init__(self, name, kind="file", owner="root", group="root",
                 mode=0o644, content="", target=""):
        self.name = name
        self.kind = kind          # dir | file | link
        self.owner = owner
        self.group = group
        self.mode = mode
        self.content = content
        self.target = target
        self.mtime = time.time()
        self.parent = None
        self.children = {} if kind == "dir" else None

    # 序列化 --------------------------------------------------------------
    def dump(self):
        return {
            "name": self.name, "kind": self.kind, "owner": self.owner,
            "group": self.group, "mode": self.mode, "content": self.content,
            "target": self.target, "mtime": self.mtime,
            "children": {k: v.dump() for k, v in (self.children or {}).items()},
        }

    @classmethod
    def load(cls, data, parent=None):
        node = cls(data["name"], data["kind"], data["owner"], data["group"],
                   data["mode"], data.get("content", ""), data.get("target", ""))
        node.mtime = data.get("mtime", time.time())
        node.parent = parent
        if node.kind == "dir":
            for name, child in data.get("children", {}).items():
                node.children[name] = cls.load(child, node)
        return node

    def clone(self):
        node = VNode(self.name, self.kind, self.owner, self.group, self.mode,
                     self.content, self.target)
        node.mtime = self.mtime
        if self.kind == "dir":
            for name, child in self.children.items():
                node.children[name] = child.clone()
                node.children[name].parent = node
        return node


class LinuxSim:
    """一个独立的 Linux 沙箱实例。"""

    def __init__(self):
        self.users = {}
        self.groups = {}
        self.root = VNode("/", "dir", "root", "root", 0o755)
        self.cwd = "/home/student"
        self.oldpwd = "/home/student"
        self.user = "student"
        self.stack = []
        self.history = []
        self.vim = None
        self.pager = None
        self.pending = None
        self.secret_prompt = False
        self.clear_flag = False
        self.less_views = 0
        self.quit_vim = 0
        self.last_input = ""
        self.last_output = ""
        self.last_names = []
        self._build()

    # ------------------------------------------------------------------ 初始化
    def _build(self):
        self.users = {
            "root": dict(uid=0, gid=0, groups=["root"], home="/root",
                         password="root", shell="/bin/bash"),
            "student": dict(uid=1000, gid=1000, groups=["student"],
                            home="/home/student", password="student",
                            shell="/bin/bash"),
        }
        self.groups = {"root": 0, "student": 1000}

        for path, mode, owner in (("/bin", 0o755, "root"), ("/etc", 0o755, "root"),
                                  ("/etc/ssh", 0o755, "root"), ("/etc/cron.d", 0o755, "root"),
                                  ("/home", 0o755, "root"), ("/root", 0o700, "root"),
                                  ("/tmp", 0o1777, "root"), ("/usr", 0o755, "root"),
                                  ("/usr/bin", 0o755, "root"), ("/var", 0o755, "root"),
                                  ("/var/log", 0o755, "root")):
            self._mkdir_abs(path, owner, "root", mode)

        self._mkdir_abs("/home/student", "student", "student", 0o755)
        self._mkdir_abs("/root", "root", "root", 0o700)

        self._add_file("/etc/ssh/sshd_config", "Port 22\nPermitRootLogin yes\n",
                       "root", "root", 0o600)
        self._add_file("/etc/hostname", "exam-host\n", "root", "root", 0o644)
        for fname, fmode in (("passwd", 0o644), ("group", 0o644), ("shadow", 0o640)):
            self._add_file(f"/etc/{fname}", "", "root", "root", fmode)
        self._add_file("/etc/motd",
                       "Linux 基础命令考核主机\n输入 help 查看本沙箱支持的命令\n",
                       "root", "root", 0o644)
        self._add_file("/home/student/notes.txt",
                       "交接备忘：\n1. 新同事 zhangsan 今天入职\n2. 日志放在 work/logs/2025\n",
                       "student", "student", 0o644)
        self._add_file("/home/student/.bashrc",
                       "export LANG=zh_CN.UTF-8\nalias ll='ls -l'\n",
                       "student", "student", 0o644)
        self._add_file("/var/log/syslog",
                       "Nov  9 09:01:12 exam-host systemd: boot\n"
                       "Nov  9 09:03:40 exam-host sshd: accepted student\n",
                       "root", "root", 0o644)

    def _mkdir_abs(self, path, owner="root", group="root", mode=0o755):
        parts = [p for p in path.split("/") if p]
        node = self.root
        for i, name in enumerate(parts):
            if name not in node.children:
                child = VNode(name, "dir", owner if i == len(parts) - 1 else "root",
                              group if i == len(parts) - 1 else "root", mode)
                child.parent = node
                node.children[name] = child
            node = node.children[name]
        return node

    def _add_file(self, path, content, owner, group, mode):
        node = self._node_at(path)
        if node is None:
            parts = [p for p in path.split("/") if p]
            parent = self.root
            for name in parts[:-1]:
                parent = parent.children[name]
            node = VNode(parts[-1], "file", owner, group, mode, content)
            node.parent = parent
            parent.children[parts[-1]] = node
        else:
            node.content = content
        return node

    # ------------------------------------------------------------------ 查询
    def _gid(self, group_name):
        return self.groups.get(group_name, 0)

    def _members(self, group_name):
        return [u for u, info in self.users.items() if group_name in info["groups"]]

    def _expand(self, path):
        if path is None:
            return self.cwd
        path = str(path)
        if path.startswith("~"):
            home = self.users[self.user]["home"]
            path = home + path[1:]
        if path == "-":
            path = self.oldpwd
        if not path.startswith("/"):
            path = self.cwd.rstrip("/") + "/" + path
        parts = []
        for comp in path.split("/"):
            if comp == "" or comp == ".":
                continue
            if comp == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(comp)
        return "/" + "/".join(parts)

    def _node_at(self, path, follow=True, _depth=0):
        if _depth > 8:
            return None
        abs_path = path if str(path).startswith("/") else self._expand(path)
        if abs_path == "/":
            return self.root
        node = self.root
        parts = [p for p in abs_path.split("/") if p]
        for i, comp in enumerate(parts):
            if node.kind != "dir":
                return None
            child = node.children.get(comp)
            if child is None:
                return None
            last = i == len(parts) - 1
            if child.kind == "link" and (follow or not last):
                target_node = self._node_at(child.target, True, _depth + 1)
                if target_node is None:
                    return None
                node = target_node
            else:
                node = child
        return node

    def _node_or_link(self, path):
        """取节点本身（不穿透软链接），用于 ls -l。"""
        abs_path = self._expand(path)
        parts = [p for p in abs_path.split("/") if p]
        node = self.root
        for comp in parts[:-1]:
            if node.kind != "dir":
                return None
            child = node.children.get(comp)
            if child is None:
                return None
            node = self._node_at(child.target) if child.kind == "link" else child
            if node is None:
                return None
        return node.children.get(parts[-1]) if node.kind == "dir" else None

    def _parent_of_path(self, path):
        abs_path = self._expand(path)
        parts = [p for p in abs_path.split("/") if p]
        if not parts:
            return None, ""
        parent = self._node_at("/" + "/".join(parts[:-1]))
        return parent, parts[-1]

    def path_of(self, node):
        parts = []
        cur = node
        while cur.parent is not None:
            parts.append(cur.name)
            cur = cur.parent
        return "/" + "/".join(reversed(parts))

    def exists(self, path):
        return self._node_at(path) is not None

    def mode_str(self, node):
        head = {"dir": "d", "file": "-", "link": "l"}.get(node.kind, "-")
        bits = ""
        for shift in (6, 3, 0):
            v = (node.mode >> shift) & 7
            bits += ("r" if v & 4 else "-") + ("w" if v & 2 else "-") + ("x" if v & 1 else "-")
        return head + bits

    def _allowed(self, node, need):
        if node is None:
            return False
        if self.user == "root":
            return True
        if node.owner == self.user:
            shift = 6
        elif node.group in self.users[self.user]["groups"]:
            shift = 3
        else:
            shift = 0
        bit = {"r": 4, "w": 2, "x": 1}[need]
        return bool(((node.mode >> shift) & 7) & bit)

    # ------------------------------------------------------------------ 提示符
    def prompt(self):
        home = self.users[self.user]["home"]
        cwd = self.cwd
        if cwd == home:
            cwd = "~"
        elif home != "/" and cwd.startswith(home + "/"):
            cwd = "~" + cwd[len(home):]
        sig = "#" if self.user == "root" else "$"
        return f"{self.user}@exam:{cwd}{sig} "

    def mode_badge(self):
        if self.vim:
            return "插入模式 INSERT" if self.vim["mode"] == "insert" else "普通模式 NORMAL"
        if self.pager:
            return "分页模式 less"
        if self.pending:
            return "等待输入密码"
        return ""

    # ------------------------------------------------------------------ 输入
    def feed(self, line):
        self.last_input = line
        self.last_names = []
        self.clear_flag = False
        self.secret_prompt = False

        if self.pending:
            return self._pending_input(line)
        if self.vim:
            return self._vim_input(line)
        if self.pager:
            return self._pager_input(line)

        if not line.strip():
            self.last_output = ""
            return ""
        self.history.append(line)
        output = ""
        stdin_text = ""
        try:
            for segment in self._split_pipe(line):
                argv = shlex.split(segment)
                if not argv:
                    continue
                self.last_names.append(argv[0])
                output = self.dispatch(argv[0], argv, stdin_text)
                stdin_text = output
        except ValueError as exc:
            output = f"命令解析失败：{exc}"
        except Exception as exc:  # 沙箱不应因为一条命令崩溃
            output = f"shell: 执行出错：{exc}"
        self.last_output = output
        return output

    @staticmethod
    def _split_pipe(line):
        segments, buf, quote = [], "", None
        for ch in line:
            if quote:
                buf += ch
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
                buf += ch
            elif ch == "|":
                segments.append(buf)
                buf = ""
            else:
                buf += ch
        segments.append(buf)
        return segments

    # ------------------------------------------------------------------ 交互
    def _pending_input(self, line):
        p = self.pending
        kind = p["kind"]
        self.secret_prompt = True
        if kind == "passwd":
            return self._passwd_input(line)
        if kind in ("su", "sudo"):
            self.pending = None
            target = "root" if kind == "sudo" else p["target"]
            if line.strip() == self.users[target]["password"] or (
                    kind == "sudo" and line.strip() == "root"):
                login = True if kind == "sudo" else p["login"]
                self._switch_user(target, login)
                return ""
            return "密码错误，认证失败。\n"
        self.pending = None
        return ""

    def _passwd_input(self, line):
        p = self.pending
        target = p["target"]
        stage = p["stage"]
        if stage == "old":
            if line.strip() != self.users[self.user]["password"]:
                self.pending = None
                self.secret_prompt = False
                return "passwd: 当前密码不正确，认证失败。\n"
            p["stage"] = "new"
            return "新的密码："
        if stage == "new":
            if not line.strip():
                return "密码不能为空。\n新的密码："
            p["new"] = line.strip()
            p["stage"] = "confirm"
            return "重新输入新密码："
        if stage == "confirm":
            if line.strip() != p["new"]:
                p["stage"] = "new"
                p["new"] = None
                return "密码不一致，请重新输入。\n新的密码："
            self.users[target]["password"] = p["new"]
            self.users[target]["passwd_set"] = True
            self.pending = None
            self.secret_prompt = False
            return "passwd: 密码已更新成功。\n"
        self.pending = None
        return ""

    def _switch_user(self, target, login):
        self.stack.append((self.user, self.cwd))
        self.oldpwd = self.cwd
        self.user = target
        if login:
            self.cwd = self.users[target]["home"]
        self.secret_prompt = False

    # vim -----------------------------------------------------------------
    def _vim_input(self, line):
        v = self.vim
        path = v["path"]
        if v["mode"] == "insert":
            if line == "ESC" or line.endswith("\x1b"):
                text = line[:-3] if line.endswith("ESC") else line[:-1]
                if text:
                    v["buffer"].append(text)
                v["mode"] = "normal"
                return "[-- 已退出插入模式 --]"
            v["buffer"].append(line)
            return ""
        cmd = line.strip()
        if cmd in ("i", "a", "o", "I", "A"):
            v["mode"] = "insert"
            return "[-- INSERT --] 可以开始输入，按 Esc 退出插入模式"
        if cmd in (":wq", ":w", ":x"):
            node = self._node_at(path, follow=False)
            content = "\n".join(v["buffer"])
            if v["buffer"]:
                content += "\n"
            if node is None:
                parent, name = self._parent_of_path(path)
                if parent is None:
                    self.vim = None
                    return f"vim: 无法保存 {self._expand(path)}: 目录不存在"
                node = VNode(name, "file", self.user,
                             self.users[self.user]["groups"][0], 0o644, content)
                node.parent = parent
                parent.children[name] = node
            else:
                node.content = content
            self.vim = None
            return f'"{self._expand(path)}" 已写入'
        if cmd == ":q!":
            self.vim = None
            self.quit_vim += 1
            return "已放弃修改并退出 vim"
        if cmd == ":q":
            if v["buffer"]:
                return "E37: 已修改但尚未保存（用 :wq 保存，或用 :q! 强制退出）"
            self.vim = None
            return ""
        if line == "ESC":
            return ""
        return f"[vim {v['mode']}] 当前输入：{cmd}（i 进入插入，:wq 保存退出，:q! 放弃退出）"

    # less ----------------------------------------------------------------
    def _pager_input(self, line):
        cmd = line.strip()
        self.secret_prompt = False
        if cmd == "q":
            self.pager = None
            self.less_views += 1
            return ""
        if cmd == "" or cmd == " ":
            p = self.pager
            if p and p["offset"] < len(p["lines"]):
                chunk = p["lines"][p["offset"]:p["offset"] + 20]
                p["offset"] += len(chunk)
                return "\n".join(chunk)
            return ""
        return "（分页模式中：空格翻页，q 退出）"

    # ------------------------------------------------------------------ 命令分发
    def dispatch(self, name, argv, stdin_text=""):
        handler = getattr(self, "cmd_" + name, None)
        if handler is None:
            return f"{name}: 未找到命令（本沙箱只内置 PPT 中的命令，输入 help 查看清单）"
        return handler(argv, stdin_text)

    # 基础 ---------------------------------------------------------------
    def cmd_help(self, argv, stdin_text=""):
        return ("内置命令：pwd ls cd touch mkdir rm rmdir cp mv cat vim less head tail wc "
                "grep find which ln tar chmod chown chgrp useradd userdel usermod groupadd "
                "groupdel gpasswd id groups su sudo whoami history clear exit")

    def cmd_pwd(self, argv, stdin_text=""):
        return self.cwd

    def cmd_whoami(self, argv, stdin_text=""):
        return self.user

    def cmd_clear(self, argv, stdin_text=""):
        self.clear_flag = True
        return ""

    def cmd_exit(self, argv, stdin_text=""):
        if self.stack:
            self.user, self.cwd = self.stack.pop()
            return ""
        if self.user != "student":
            self.user = "student"
            self.cwd = "/home/student"
            self.stack = []
            return ""
        return "logout"

    def cmd_history(self, argv, stdin_text=""):
        return "\n".join(f"{i + 1:>4}  {c}" for i, c in enumerate(self.history))

    def cmd_id(self, argv, stdin_text=""):
        target = argv[1] if len(argv) > 1 else self.user
        if target not in self.users:
            return f"id: “{target}”：无此用户"
        info = self.users[target]
        gids = [f"{self._gid(g)}({g})" for g in info["groups"]]
        return (f"uid={info['uid']}({target}) gid={info['gid']}({info['groups'][0]}) "
                f"groups={','.join(gids)}")

    def cmd_groups(self, argv, stdin_text=""):
        target = argv[1] if len(argv) > 1 else self.user
        if target not in self.users:
            return f"groups: “{target}”：无此用户"
        return f"{target} : {' '.join(self.users[target]['groups'])}"

    # 目录 ---------------------------------------------------------------
    def cmd_ls(self, argv, stdin_text=""):
        show_all = False
        long_fmt = False
        paths = []
        for arg in argv[1:]:
            if arg.startswith("-") and not arg.startswith("--"):
                for ch in arg[1:]:
                    if ch == "a":
                        show_all = True
                    elif ch == "l":
                        long_fmt = True
            else:
                paths.append(arg)
        if not paths:
            paths = ["."]
        blocks = []
        for path in paths:
            node = self._node_or_link(path)
            if node is None:
                blocks.append(f"ls: 无法访问 '{path}': 没有那个文件或目录")
                continue
            if node.kind != "dir":
                blocks.append(self._ls_line(node) if long_fmt else node.name)
                continue
            if not self._allowed(node, "r") or not self._allowed(node, "x"):
                blocks.append(f"ls: 无法打开目录 '{path}': 权限不够")
                continue
            entries = []
            if show_all:
                entries.append(".")
                entries.append("..")
            entries.extend(node.children.keys())
            if not show_all:
                entries = [e for e in entries if not e.startswith(".")]
            entries.sort()
            if long_fmt:
                total = sum(self._size_of(node.children[e]) for e in entries
                            if e not in (".", "..")) // 1024 or 4
                lines = [f"总计 {total}"]
                for name in entries:
                    if name == ".":
                        lines.append(self._ls_line(node, "."))
                    elif name == "..":
                        lines.append(self._ls_line(node.parent or node, ".."))
                    else:
                        lines.append(self._ls_line(node.children[name]))
                blocks.append("\n".join(lines))
            else:
                blocks.append("  ".join(entries))
        header = ""
        return header + "\n".join(blocks)

    def _size_of(self, node):
        if node.kind == "dir":
            return 4096
        if node.kind == "link":
            return 4
        virtual = self._virtual_content(self.path_of(node))
        if virtual is not None:
            return len(virtual.encode("utf-8"))
        return max(1, len((node.content or "").encode("utf-8")))

    def _ls_line(self, node, display_name=None):
        name = display_name or node.name
        dt = datetime.fromtimestamp(node.mtime).strftime("%b %d %H:%M")
        size = self._size_of(node)
        tail = f" -> {node.target}" if node.kind == "link" else ""
        return (f"{self.mode_str(node)} {1:>2} {node.owner:<8} {node.group:<8} "
                f"{size:>6} {dt} {name}{tail}")

    def cmd_cd(self, argv, stdin_text=""):
        target = argv[1] if len(argv) > 1 else "~"
        if target == "-":
            target = self.oldpwd
        node = self._node_at(target)
        if node is None:
            return f"cd: {target}: 没有那个文件或目录"
        if node.kind != "dir":
            return f"cd: {target}: 不是一个目录"
        if not self._allowed(node, "x"):
            return f"cd: {target}: 权限不够"
        new_path = self._expand(target)
        if new_path != self.cwd:
            self.oldpwd = self.cwd
        self.cwd = new_path
        return ""

    def cmd_mkdir(self, argv, stdin_text=""):
        recursive = False
        paths = []
        for arg in argv[1:]:
            if arg.startswith("-"):
                if "p" in arg:
                    recursive = True
                continue
            paths.append(arg)
        if not paths:
            return "mkdir: 缺少操作数"
        messages = []
        for path in paths:
            abs_path = self._expand(path)
            if self._node_at(abs_path) is not None:
                if recursive:
                    continue
                messages.append(f"mkdir: 无法创建目录 “{path}”：文件已存在")
                continue
            if recursive:
                parts = [p for p in abs_path.split("/") if p]
                cur = self.root
                ok = True
                for comp in parts:
                    if comp in cur.children:
                        child = cur.children[comp]
                        if child.kind != "dir":
                            messages.append(f"mkdir: 无法创建目录 “{path}”：文件已存在")
                            ok = False
                            break
                        cur = child
                    else:
                        if not self._allowed(cur, "w") or not self._allowed(cur, "x"):
                            messages.append(f"mkdir: 无法创建目录 “{path}”：权限不够")
                            ok = False
                            break
                        child = VNode(comp, "dir", self.user,
                                      self.users[self.user]["groups"][0], 0o755)
                        child.parent = cur
                        cur.children[comp] = child
                        cur = child
                continue
            parent, name = self._parent_of_path(abs_path)
            if parent is None or parent.kind != "dir":
                messages.append(f"mkdir: 无法创建目录 “{path}”：没有那个文件或目录")
                continue
            if not self._allowed(parent, "w") or not self._allowed(parent, "x"):
                messages.append(f"mkdir: 无法创建目录 “{path}”：权限不够")
                continue
            child = VNode(name, "dir", self.user, self.users[self.user]["groups"][0], 0o755)
            child.parent = parent
            parent.children[name] = child
        return "\n".join(messages)

    # 文件 ---------------------------------------------------------------
    def cmd_touch(self, argv, stdin_text=""):
        args = [a for a in argv[1:] if not a.startswith("-")]
        if not args:
            return "touch: 缺少文件操作数"
        messages = []
        for path in args:
            node = self._node_or_link(path)
            if node is not None:
                node.mtime = time.time()
                continue
            parent, name = self._parent_of_path(path)
            if parent is None or parent.kind != "dir":
                messages.append(f"touch: 无法创建 '{path}': 没有那个文件或目录")
                continue
            if not self._allowed(parent, "w") or not self._allowed(parent, "x"):
                messages.append(f"touch: 无法创建 '{path}': 权限不够")
                continue
            child = VNode(name, "file", self.user, self.users[self.user]["groups"][0], 0o644, "")
            child.parent = parent
            parent.children[name] = child
        return "\n".join(messages)

    def cmd_rm(self, argv, stdin_text=""):
        recursive = False
        force = False
        paths = []
        for arg in argv[1:]:
            if arg.startswith("-"):
                if "r" in arg or "R" in arg:
                    recursive = True
                if "f" in arg:
                    force = True
                continue
            paths.append(arg)
        if not paths:
            return "rm: 缺少操作数"
        messages = []
        for path in paths:
            abs_path = self._expand(path)
            if abs_path == "/":
                messages.append("rm: 危险操作被拦截：不能删除根目录")
                continue
            node = self._node_or_link(abs_path)
            if node is None:
                if not force:
                    messages.append(f"rm: 无法删除 '{path}': 没有那个文件或目录")
                continue
            if node.kind == "dir" and not recursive:
                messages.append(f"rm: 无法删除 '{path}': 是一个目录（需要 -r）")
                continue
            parent, name = self._parent_of_path(abs_path)
            if parent is None:
                continue
            if not self._allowed(parent, "w") or not self._allowed(parent, "x"):
                messages.append(f"rm: 无法删除 '{path}': 权限不够")
                continue
            del parent.children[name]
        return "\n".join(messages)

    def cmd_rmdir(self, argv, stdin_text=""):
        paths = [a for a in argv[1:] if not a.startswith("-")]
        if not paths:
            return "rmdir: 缺少操作数"
        messages = []
        for path in paths:
            node = self._node_or_link(path)
            if node is None:
                messages.append(f"rmdir: 无法删除 '{path}': 没有那个文件或目录")
                continue
            if node.kind != "dir":
                messages.append(f"rmdir: 无法删除 '{path}': 不是目录")
                continue
            if node.children:
                messages.append(f"rmdir: 无法删除 '{path}': 目录非空")
                continue
            parent, name = self._parent_of_path(path)
            if not self._allowed(parent, "w"):
                messages.append(f"rmdir: 无法删除 '{path}': 权限不够")
                continue
            del parent.children[name]
        return "\n".join(messages)

    def cmd_cp(self, argv, stdin_text=""):
        recursive = False
        paths = []
        for arg in argv[1:]:
            if arg.startswith("-"):
                if "r" in arg or "R" in arg:
                    recursive = True
                continue
            paths.append(arg)
        if len(paths) < 2:
            return "cp: 缺少目标文件操作数"
        src, dst = paths[0], paths[1]
        src_node = self._node_at(src)
        if src_node is None:
            return f"cp: 无法获取 '{src}' 的文件状态: 没有那个文件或目录"
        if src_node.kind == "dir" and not recursive:
            return f"cp: 略过目录 '{src}'（复制目录需要 -r）"
        if not self._allowed(src_node, "r"):
            return f"cp: 无法读取 '{src}': 权限不够"
        dst_node = self._node_at(dst)
        if dst_node is not None and dst_node.kind == "dir":
            target_dir, target_name = dst_node, src_node.name
        else:
            parent, target_name = self._parent_of_path(dst)
            target_dir = parent
            if target_dir is None:
                return f"cp: 无法创建 '{dst}': 没有那个文件或目录"
            if not self._allowed(target_dir, "w"):
                return f"cp: 无法创建 '{dst}': 权限不够"
        if target_name in target_dir.children:
            self._remove_child(target_dir, target_name)
        new_node = src_node.clone()
        new_node.name = target_name
        new_node.owner = self.user
        new_node.group = self.users[self.user]["groups"][0]
        new_node.parent = target_dir
        target_dir.children[target_name] = new_node
        return ""

    def _remove_child(self, parent, name):
        node = parent.children.get(name)
        if node is None:
            return
        del parent.children[name]

    def cmd_mv(self, argv, stdin_text=""):
        paths = [a for a in argv[1:] if not a.startswith("-")]
        if len(paths) < 2:
            return "mv: 缺少目标文件操作数"
        src, dst = paths[0], paths[1]
        src_node = self._node_or_link(src)
        if src_node is None:
            return f"mv: 无法获取 '{src}' 的文件状态: 没有那个文件或目录"
        dst_node = self._node_at(dst)
        if dst_node is not None and dst_node.kind == "dir":
            target_dir, target_name = dst_node, src_node.name
        else:
            target_dir, target_name = self._parent_of_path(dst)
            if target_dir is None:
                return f"mv: 无法创建 '{dst}': 没有那个文件或目录"
        if not self._allowed(target_dir, "w"):
            return f"mv: 无法创建 '{dst}': 权限不够"
        src_parent, src_name = self._parent_of_path(src)
        if src_parent is None:
            return f"mv: 无法移动 '{src}'"
        if target_dir is src_node or self._is_descendant(target_dir, src_node):
            return f"mv: 无法将 '{src}' 移动到自身内部"
        self._remove_child(src_parent, src_name)
        if target_name in target_dir.children and dst_node is not None and dst_node.kind != "dir":
            self._remove_child(target_dir, target_name)
        src_node.name = target_name
        src_node.parent = target_dir
        target_dir.children[target_name] = src_node
        return ""

    def _is_descendant(self, node, ancestor):
        cur = node
        while cur is not None:
            if cur is ancestor:
                return True
            cur = cur.parent
        return False

    def _virtual_content(self, path):
        if path == "/etc/passwd":
            lines = []
            for name, info in sorted(self.users.items(), key=lambda kv: kv[1]["uid"]):
                lines.append(f"{name}:x:{info['uid']}:{info['gid']}:"
                             f"{name}:{info['home']}:{info['shell']}")
            return "\n".join(lines) + "\n"
        if path == "/etc/group":
            lines = []
            for name, gid in sorted(self.groups.items(), key=lambda kv: kv[1]):
                members = [u for u in self._members(name) if u != name or True]
                members.sort()
                lines.append(f"{name}:x:{gid}:" + ",".join(members))
            return "\n".join(lines) + "\n"
        if path == "/etc/shadow":
            lines = []
            for name, info in sorted(self.users.items(), key=lambda kv: kv[1]["uid"]):
                pwd = info.get("password") or ""
                hashish = ("$6$exam$" + str(abs(hash(pwd)) % (10 ** 16))) if pwd else ""
                lines.append(f"{name}:{hashish}:19000:0:99999:7:::")
            return "\n".join(lines) + "\n"
        return None

    def _read_file(self, node):
        path = self.path_of(node)
        virtual = self._virtual_content(path)
        if virtual is not None:
            return virtual
        return node.content or ""

    def cmd_cat(self, argv, stdin_text=""):
        number = False
        files = []
        for arg in argv[1:]:
            if arg.startswith("-"):
                if "n" in arg:
                    number = True
                continue
            files.append(arg)
        if not files:
            return stdin_text
        chunks = []
        for path in files:
            node = self._node_at(path)
            if node is None:
                chunks.append(f"cat: {path}: 没有那个文件或目录")
                continue
            if node.kind == "dir":
                chunks.append(f"cat: {path}: 是一个目录")
                continue
            if not self._allowed(node, "r"):
                chunks.append(f"cat: {path}: 权限不够")
                continue
            text = self._read_file(node)
            if number:
                text = "\n".join(f"{i + 1:>6}\t{ln}" for i, ln in enumerate(text.splitlines()))
                if text:
                    text += "\n"
            chunks.append(text)
        return "".join(chunks)

    def cmd_vim(self, argv, stdin_text=""):
        args = [a for a in argv[1:] if not a.startswith("-")]
        if not args:
            return "vim: 缺少文件名"
        path = self._expand(args[0])
        node = self._node_at(path)
        if node is not None:
            if node.kind == "dir":
                return f"vim: {args[0]}: 是一个目录"
            if not self._allowed(node, "r"):
                return f"vim: {args[0]}: 权限不够"
            text = self._read_file(node)
        else:
            parent, _ = self._parent_of_path(path)
            if parent is None or parent.kind != "dir":
                return f"vim: {args[0]}: 没有那个文件或目录"
            text = ""
        buffer = text.splitlines()
        self.vim = {"path": path, "buffer": buffer, "mode": "normal", "name": args[0]}
        shown = "\n".join(buffer) if buffer else "(空文件)"
        return (f'"{args[0]}" {len(buffer)} 行\n{shown}\n'
                "[vim 普通模式] 按 i 进入插入模式，Esc 退出插入，:wq 保存退出，:q! 放弃")

    def cmd_less(self, argv, stdin_text=""):
        files = [a for a in argv[1:] if not a.startswith("-")]
        if files:
            node = self._node_at(files[0])
            if node is None:
                return f"less: {files[0]}: 没有那个文件或目录"
            if not self._allowed(node, "r"):
                return f"less: {files[0]}: 权限不够"
            text = self._read_file(node)
        else:
            text = stdin_text
        lines = text.splitlines()
        if not lines:
            return ""
        first = lines[:20]
        self.pager = {"lines": lines, "offset": len(first), "name": files[0] if files else "stdin"}
        more = "\n（↑↓ 翻页  空格 下一页  q 退出）" if len(lines) > len(first) else "\n（按 q 退出）"
        return "\n".join(first) + more

    def cmd_head(self, argv, stdin_text=""):
        n = 10
        files = []
        i = 1
        while i < len(argv):
            arg = argv[i]
            if arg == "-n" and i + 1 < len(argv):
                n = int(argv[i + 1]); i += 2; continue
            if arg.startswith("-") and arg[1:].isdigit():
                n = int(arg[1:]); i += 1; continue
            files.append(arg); i += 1
        text = self._file_or_stdin(files, stdin_text)
        if text is None:
            return "head: 无法读取输入"
        return "\n".join(text.splitlines()[:n]) + ("\n" if text else "")

    def cmd_tail(self, argv, stdin_text=""):
        n = 10
        files = []
        i = 1
        while i < len(argv):
            arg = argv[i]
            if arg == "-n" and i + 1 < len(argv):
                n = int(argv[i + 1]); i += 2; continue
            if arg.startswith("-") and arg[1:].isdigit():
                n = int(arg[1:]); i += 1; continue
            if arg == "-f":
                i += 1; continue
            files.append(arg); i += 1
        text = self._file_or_stdin(files, stdin_text)
        if text is None:
            return "tail: 无法读取输入"
        return "\n".join(text.splitlines()[-n:]) + ("\n" if text else "")

    def cmd_wc(self, argv, stdin_text=""):
        flag = ""
        files = []
        for arg in argv[1:]:
            if arg.startswith("-"):
                flag = arg
                continue
            files.append(arg)
        text = self._file_or_stdin(files, stdin_text)
        if text is None:
            return "wc: 无法读取输入"
        lines = len(text.splitlines())
        words = len(text.split())
        chars = len(text)
        if flag.endswith("l") or "-l" in argv:
            return str(lines)
        if flag.endswith("w"):
            return str(words)
        if flag.endswith("c"):
            return str(chars)
        return f"{lines} {words} {chars}"

    def _file_or_stdin(self, files, stdin_text):
        if not files:
            return stdin_text
        out = []
        for path in files:
            node = self._node_at(path)
            if node is None:
                return None
            if not self._allowed(node, "r"):
                return None
            out.append(self._read_file(node))
        return "".join(out)

    def cmd_grep(self, argv, stdin_text=""):
        flags = ""
        pattern = None
        files = []
        for arg in argv[1:]:
            if arg.startswith("-") and pattern is None and not arg.startswith("--"):
                flags += arg[1:]
                continue
            if pattern is None:
                pattern = arg
            else:
                files.append(arg)
        if pattern is None:
            return "grep: 缺少搜索模式"
        import re
        try:
            regex = re.compile(pattern, re.IGNORECASE if "i" in flags else 0)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE if "i" in flags else 0)
        text = self._file_or_stdin(files, stdin_text)
        if text is None:
            return "grep: 无法读取输入"
        invert = "v" in flags
        show_no = "n" in flags
        results = []
        for i, line in enumerate(text.splitlines(), 1):
            hit = bool(regex.search(line))
            if invert:
                hit = not hit
            if hit:
                results.append(f"{i}:{line}" if show_no else line)
        return "\n".join(results)

    def cmd_find(self, argv, stdin_text=""):
        if len(argv) < 2:
            return "find: 缺少路径"
        path = argv[1]
        name_pat = None
        if "-name" in argv:
            idx = argv.index("-name")
            if idx + 1 < len(argv):
                name_pat = argv[idx + 1]
        start = self._node_at(path)
        if start is None:
            return f"find: '{path}': 没有那个文件或目录"
        results = []

        def walk(node, cur_path):
            if node.kind == "dir":
                if not self._allowed(node, "r") or not self._allowed(node, "x"):
                    return
            if name_pat is None or fnmatch.fnmatch(node.name, name_pat):
                results.append(cur_path)
            if node.kind == "dir":
                for name in sorted(node.children.keys()):
                    walk(node.children[name], cur_path.rstrip("/") + "/" + name)

        walk(start, self._expand(path))
        return "\n".join(results)

    def cmd_which(self, argv, stdin_text=""):
        known = {"python3", "python", "vim", "bash", "ls", "cat", "grep", "sed", "awk",
                 "systemctl", "ssh", "tar", "find", "chmod", "useradd"}
        if len(argv) < 2:
            return "which: 缺少参数"
        cmd = argv[1]
        if cmd in known:
            return f"/usr/bin/{cmd}"
        return f"which: 在 (/usr/local/bin:/usr/bin:/bin) 中没有找到 {cmd}"

    def cmd_ln(self, argv, stdin_text=""):
        symbolic = False
        args = []
        for arg in argv[1:]:
            if arg.startswith("-"):
                if "s" in arg:
                    symbolic = True
                continue
            args.append(arg)
        if len(args) < 2:
            return "ln: 缺少操作数"
        if not symbolic:
            return "ln: 本沙箱只演示 -s 软链接（硬链接请用 -s）"
        target, link_name = args[0], args[1]
        abs_target = self._expand(target)
        if self._node_at(target) is None:
            return f"ln: 无法创建符号链接 '{link_name}': 目标 '{target}' 不存在"
        if self._node_or_link(link_name) is not None:
            return f"ln: 无法创建符号链接 '{link_name}': 文件已存在"
        parent, name = self._parent_of_path(link_name)
        if parent is None:
            return f"ln: 无法创建符号链接 '{link_name}': 没有那个文件或目录"
        if not self._allowed(parent, "w"):
            return f"ln: 无法创建符号链接 '{link_name}': 权限不够"
        node = VNode(name, "link", self.user, self.users[self.user]["groups"][0], 0o777, "", abs_target)
        node.parent = parent
        parent.children[name] = node
        return ""

    def cmd_tar(self, argv, stdin_text=""):
        flags = ""
        rest = []
        for arg in argv[1:]:
            if arg.startswith("-") and not flags:
                flags = arg[1:]
            elif arg.startswith("-") and flags:
                flags += arg[1:]
            else:
                rest.append(arg)
        if "c" in flags:
            if len(rest) < 2:
                return "tar: 压缩需要 包名 和 源文件"
            archive_name, src = rest[0], rest[1]
            src_node = self._node_at(src)
            if src_node is None:
                return f"tar: {src}: 没有那个文件或目录"
            files = []

            def collect(node, base):
                rel = base
                if node.kind == "dir":
                    files.append({"path": rel, "kind": "dir", "mode": node.mode,
                                  "owner": node.owner, "group": node.group})
                    for name, child in node.children.items():
                        collect(child, rel.rstrip("/") + "/" + name)
                else:
                    files.append({"path": rel, "kind": node.kind, "mode": node.mode,
                                  "owner": node.owner, "group": node.group,
                                  "content": self._read_file(node) if node.kind == "file" else "",
                                  "target": node.target})

            collect(src_node, src_node.name)
            blob = json.dumps({"entries": files}, ensure_ascii=False)
            parent, name = self._parent_of_path(archive_name)
            if parent is None:
                return f"tar: {archive_name}: 无法创建归档"
            node = VNode(name, "file", self.user, self.users[self.user]["groups"][0], 0o644, blob)
            node.parent = parent
            parent.children[name] = node
            listing = "\n".join(f["path"] for f in files)
            return listing + f"\n（已生成 {self._expand(archive_name)}，共 {len(files)} 个条目）"
        if "x" in flags:
            if not rest:
                return "tar: 解压需要指定压缩包"
            archive = self._node_at(rest[0])
            if archive is None:
                return f"tar: {rest[0]}: 没有那个文件或目录"
            try:
                data = json.loads(self._read_file(archive))
            except Exception:
                return f"tar: {rest[0]}: 不是有效的 tar.gz 归档"
            target_dir = self._node_at(self.cwd)
            if target_dir is None or target_dir.kind != "dir":
                return f"tar: 无法在 {self.cwd} 中解压"
            for entry in data.get("entries", []):
                rel = entry["path"]
                parts = rel.split("/")
                cur = target_dir
                ok = True
                for comp in parts[:-1]:
                    if comp in cur.children and cur.children[comp].kind == "dir":
                        cur = cur.children[comp]
                    else:
                        new_dir = VNode(comp, "dir", self.user,
                                        self.users[self.user]["groups"][0], 0o755)
                        new_dir.parent = cur
                        cur.children[comp] = new_dir
                        cur = new_dir
                name = parts[-1]
                if entry["kind"] == "dir":
                    if name not in cur.children:
                        new_dir = VNode(name, "dir", self.user,
                                        self.users[self.user]["groups"][0], entry.get("mode", 0o755))
                        new_dir.parent = cur
                        cur.children[name] = new_dir
                else:
                    node = VNode(name, "file", self.user,
                                 self.users[self.user]["groups"][0], entry.get("mode", 0o644),
                                 entry.get("content", ""))
                    node.parent = cur
                    cur.children[name] = node
            return "\n".join(e["path"] for e in data.get("entries", [])) + "\n（解压完成）"
        return "tar: 需要 -c（压缩）或 -x（解压）"

    # 权限 ---------------------------------------------------------------
    def cmd_chmod(self, argv, stdin_text=""):
        args = [a for a in argv[1:] if not a.startswith("-")] if False else argv[1:]
        if len(args) < 2:
            return "chmod: 缺少操作数"
        mode_spec = args[0]
        targets = args[1:]
        messages = []
        for path in targets:
            node = self._node_or_link(path)
            if node is None:
                messages.append(f"chmod: 无法访问 '{path}': 没有那个文件或目录")
                continue
            if self.user != "root" and node.owner != self.user:
                messages.append(f"chmod: 更改 '{path}' 的权限: 权限不够")
                continue
            if mode_spec.isdigit():
                try:
                    mode = int(mode_spec, 8) & 0o7777
                except ValueError:
                    messages.append(f"chmod: 无效模式：'{mode_spec}'")
                    continue
                node.mode = mode
                continue
            m = re.fullmatch(r"([ugoa]*)([+\-=])([rwx]+)", mode_spec)
            if not m:
                messages.append(f"chmod: 无效模式：'{mode_spec}'")
                continue
            who, op, perms = m.groups()
            who = who or "a"
            mask = 0
            for ch in perms:
                mask |= {"r": 4, "w": 2, "x": 1}[ch]
            targets_bits = []
            if "a" in who:
                targets_bits = [(6, mask), (3, mask), (0, mask)]
            else:
                for ch in who:
                    targets_bits.append(({"u": 6, "g": 3, "o": 0}[ch], mask))
            for shift, bits in targets_bits:
                if op == "+":
                    node.mode |= bits << shift
                elif op == "-":
                    node.mode &= ~(bits << shift)
                else:
                    node.mode = (node.mode & ~(7 << shift)) | (bits << shift)
            node.mode &= 0o7777
        return "\n".join(messages)

    def cmd_chown(self, argv, stdin_text=""):
        args = argv[1:]
        if len(args) < 2:
            return "chown: 缺少操作数"
        if self.user != "root":
            return "chown: 更改所有者: 权限不够（只有 root 可以修改文件所有者）"
        spec = args[0]
        if ":" in spec:
            owner, group = spec.split(":", 1)
        else:
            owner, group = spec, None
        if owner and owner not in self.users:
            return f"chown: 无效的用户：'{owner}'"
        if group and group not in self.groups:
            return f"chown: 无效的组：'{group}'"
        messages = []
        for path in args[1:]:
            node = self._node_or_link(path)
            if node is None:
                messages.append(f"chown: 无法访问 '{path}': 没有那个文件或目录")
                continue
            if owner:
                node.owner = owner
            if group:
                node.group = group
        return "\n".join(messages)

    def cmd_chgrp(self, argv, stdin_text=""):
        args = argv[1:]
        if len(args) < 2:
            return "chgrp: 缺少操作数"
        group = args[0]
        if group not in self.groups:
            return f"chgrp: 无效的组：'{group}'"
        messages = []
        for path in args[1:]:
            node = self._node_or_link(path)
            if node is None:
                messages.append(f"chgrp: 无法访问 '{path}': 没有那个文件或目录")
                continue
            if self.user != "root" and node.owner != self.user:
                messages.append(f"chgrp: 更改 '{path}' 的属组: 权限不够")
                continue
            node.group = group
        return "\n".join(messages)

    # 用户 / 组 -----------------------------------------------------------
    def _require_root(self, action):
        if self.user != "root":
            return f"{action}: 只有 root 用户才能执行此操作（先 sudo su 切到管理员）"
        return None

    def cmd_useradd(self, argv, stdin_text=""):
        err = self._require_root("useradd")
        if err:
            return err
        args = [a for a in argv[1:] if not a.startswith("-")]
        if not args:
            return "useradd: 缺少用户名"
        name = args[0]
        if name in self.users:
            return f"useradd: 用户 '{name}' 已存在"
        uid = max(u["uid"] for u in self.users.values()) + 1
        gid = max(self.groups.values()) + 1
        self.groups[name] = gid
        self.users[name] = dict(uid=uid, gid=gid, groups=[name], home=f"/home/{name}",
                                password="", shell="/bin/bash", passwd_set=False)
        self._mkdir_abs(f"/home/{name}", name, name, 0o755)
        self._add_file(f"/home/{name}/.bashrc", "export LANG=zh_CN.UTF-8\n", name, name, 0o644)
        return ""

    def cmd_userdel(self, argv, stdin_text=""):
        err = self._require_root("userdel")
        if err:
            return err
        remove_home = any("r" in a[1:] for a in argv[1:] if a.startswith("-"))
        args = [a for a in argv[1:] if not a.startswith("-")]
        if not args:
            return "userdel: 缺少用户名"
        name = args[0]
        if name == "root":
            return "userdel: 不能删除 root 用户"
        if name not in self.users:
            return f"userdel: 用户 '{name}' 不存在"
        del self.users[name]
        for g in self.groups:
            pass
        for info in self.users.values():
            if name in info["groups"] and name != info["groups"][0]:
                info["groups"].remove(name)
        if remove_home:
            home = self._node_at(f"/home/{name}")
            if home is not None:
                parent, _ = self._parent_of_path(f"/home/{name}")
                parent.children.pop(name, None)
        return ""

    def cmd_usermod(self, argv, stdin_text=""):
        err = self._require_root("usermod")
        if err:
            return err
        append = False
        group_list = None
        user = None
        i = 1
        while i < len(argv):
            arg = argv[i]
            if arg.startswith("-") and "G" in arg:
                append = "a" in arg
                if i + 1 < len(argv):
                    group_list = argv[i + 1].split(",")
                    i += 2
                    continue
            if not arg.startswith("-") and user is None:
                user = arg
            i += 1
        if user is None:
            return "usermod: 缺少用户名"
        if user not in self.users:
            return f"usermod: 用户 '{user}' 不存在"
        if group_list is None:
            return "usermod: 本沙箱演示 -G（附加组）用法"
        for g in group_list:
            if g not in self.groups:
                return f"usermod: 组 '{g}' 不存在"
        info = self.users[user]
        primary = info["groups"][0]
        if append:
            for g in group_list:
                if g not in info["groups"]:
                    info["groups"].append(g)
        else:
            info["groups"] = [primary] + [g for g in group_list if g != primary]
        return ""

    def cmd_groupadd(self, argv, stdin_text=""):
        err = self._require_root("groupadd")
        if err:
            return err
        args = [a for a in argv[1:] if not a.startswith("-")]
        if not args:
            return "groupadd: 缺少组名"
        name = args[0]
        if name in self.groups:
            return f"groupadd: 组 '{name}' 已存在"
        self.groups[name] = max(self.groups.values()) + 1
        return ""

    def cmd_groupdel(self, argv, stdin_text=""):
        err = self._require_root("groupdel")
        if err:
            return err
        args = [a for a in argv[1:] if not a.startswith("-")]
        if not args:
            return "groupdel: 缺少组名"
        name = args[0]
        if name not in self.groups:
            return f"groupdel: 组 '{name}' 不存在"
        for user, info in self.users.items():
            if info["groups"][0] == name:
                return f"groupdel: 无法删除组 '{name}'：仍有用户以它为初始组（先调整这些用户的主组）"
        del self.groups[name]
        for info in self.users.values():
            if name in info["groups"]:
                info["groups"].remove(name)
        return ""

    def cmd_gpasswd(self, argv, stdin_text=""):
        err = self._require_root("gpasswd")
        if err:
            return err
        if len(argv) < 4:
            return "gpasswd: 用法 gpasswd -a/-d 用户 组"
        action, user, group = argv[1], argv[2], argv[3]
        if user not in self.users:
            return f"gpasswd: 用户 '{user}' 不存在"
        if group not in self.groups:
            return f"gpasswd: 组 '{group}' 不存在"
        info = self.users[user]
        if action == "-a":
            if group not in info["groups"]:
                info["groups"].append(group)
            return f"正在将用户 '{user}' 加入到 '{group}' 组"
        if action == "-d":
            if group in info["groups"] and group != info["groups"][0]:
                info["groups"].remove(group)
            return f"正在将用户 '{user}' 从 '{group}' 组中删除"
        return "gpasswd: 本沙箱演示 -a / -d 用法"

    def cmd_passwd(self, argv, stdin_text=""):
        args = [a for a in argv[1:] if not a.startswith("-")]
        target = args[0] if args else self.user
        if target not in self.users:
            return f"passwd: 用户 '{target}' 不存在"
        if self.user != "root" and target != self.user:
            return "passwd: 只有 root 可以修改其他用户的密码"
        stage = "new" if self.user == "root" else "old"
        self.pending = {"kind": "passwd", "target": target, "stage": stage, "new": None}
        self.secret_prompt = True
        if self.user == "root" and target != "root":
            return f"正在修改用户 {target} 的密码。\n新的密码："
        if target == "root":
            return "新的密码："
        return "当前的密码："

    def cmd_su(self, argv, stdin_text=""):
        login = False
        args = argv[1:]
        if args and args[0] == "-":
            login = True
            args = args[1:]
        target = args[0] if args else "root"
        if target not in self.users:
            return f"su: 用户 {target} 不存在"
        if target == self.user:
            return f"su: 已经是以 {target} 身份运行"
        if self.user == "root":
            self._switch_user(target, login)
            return ""
        self.pending = {"kind": "su", "target": target, "login": login}
        self.secret_prompt = True
        return "Password: "

    def cmd_sudo(self, argv, stdin_text=""):
        if "su" not in argv[1:]:
            return "sudo: 本沙箱只演示 sudo su"
        if self.user == "root":
            return "已经是 root 用户"
        self.pending = {"kind": "sudo", "target": "root", "login": True}
        self.secret_prompt = True
        return "Password: "

    # ------------------------------------------------------------------ 快照
    def dump(self):
        return {
            "users": self.users, "groups": self.groups, "root": self.root.dump(),
            "cwd": self.cwd, "oldpwd": self.oldpwd, "user": self.user,
            "stack": self.stack, "history": self.history,
            "less_views": self.less_views, "quit_vim": self.quit_vim,
        }

    @classmethod
    def restore(cls, data):
        sim = cls.__new__(cls)
        sim.users = data["users"]
        sim.groups = data["groups"]
        sim.root = VNode.load(data["root"], None)
        sim.cwd = data["cwd"]
        sim.oldpwd = data.get("oldpwd", sim.cwd)
        sim.user = data["user"]
        sim.stack = [tuple(x) for x in data.get("stack", [])]
        sim.history = data.get("history", [])
        sim.vim = None
        sim.pager = None
        sim.pending = None
        sim.secret_prompt = False
        sim.clear_flag = False
        sim.less_views = data.get("less_views", 0)
        sim.quit_vim = data.get("quit_vim", 0)
        sim.last_input = ""
        sim.last_output = ""
        sim.last_names = []
        return sim
