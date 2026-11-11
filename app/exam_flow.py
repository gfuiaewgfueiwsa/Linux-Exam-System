# -*- coding: utf-8 -*-
"""exam_flow —— 连续剧情式实操考核。

一条完整故事线：以 student 身份登录服务器 → 提权 → 给新同事建账号、
设密码、建组调组 → 切到她的身份整理日志 → 权限与属主 → 复制/移动/
软链接/打包 → 查找 → 清理临时账号 → 交回账号。

每一关只校验 PPT 里出现过的命令，并且必须真正改变沙箱状态。
"""

from __future__ import annotations

import json
import os
import time

from linux_engine import LinuxSim

PPT_COMMANDS = [
    "sudo", "su", "touch", "mkdir", "rm", "rmdir", "cd", "vim", "cat",
    "pwd", "ls", "cp", "mv", "clear", "history", "whoami", "exit", "id",
    "find", "which", "ln", "tar", "less", "head", "tail", "wc", "grep",
    "chmod", "chown", "chgrp", "useradd", "passwd", "userdel", "usermod",
    "groupadd", "groupdel", "gpasswd", "groups",
]


def used(sim, *names):
    return all(n in sim.last_names for n in names)


def has_flag(sim, ch):
    for token in sim.last_input.split():
        if token.startswith("-") and len(token) > 1 and ch in token[1:]:
            return True
    return False


def has(sim, path):
    return sim._node_at(path) is not None


def node(sim, path):
    return sim._node_or_link(path)


def out(sim, text):
    return text in (sim.last_output or "")


def mode_is(sim, path, mode):
    n = node(sim, path)
    return n is not None and n.mode == mode


def content_of(sim, path):
    n = node(sim, path)
    if n is None or n.kind != "file":
        return ""
    return sim._read_file(n)


def step(key, act, title, task, expect, check, why, points=2):
    return dict(key=key, act=act, title=title, task=task, expect=expect,
                check=check, why=why, points=points)


STEPS = [
    # ---------------------------------------------------------------- 第一幕 · 身份与提权
    step("whoami1", "第一幕 · 身份与提权", "确认登录身份",
         "确认当前登录的普通用户身份",
         "whoami",
         lambda s: used(s, "whoami") and out(s, "student"),
         "whoami 输出当前登录用户名，是排查权限问题的第一步。"),
    step("sudo1", "第一幕 · 身份与提权", "进入超级管理员",
         "进入 root 超级管理员权限",
         "sudo su",
         lambda s: s.user == "root" and len(s.stack) == 1 and s.stack[-1][0] == "student",
         "sudo su 让普通用户临时获得 root 权限，提示符从 $ 变为 #。", points=3),
    step("pwd1", "第一幕 · 身份与提权", "查看当前路径",
         "查看当前所在的绝对路径",
         "pwd",
         lambda s: used(s, "pwd") and s.cwd == "/home/student",
         "pwd 显示当前工作目录，登录后默认落在自己的主目录。"),

    # ---------------------------------------------------------------- 第二幕 · 创建用户与组
    step("groupadd", "第二幕 · 创建用户与组", "创建 ks 用户组",
         "在系统中创建一个名为 ks 的用户组",
         "groupadd ks",
         lambda s: "ks" in s.groups,
         "用户组是权限的逻辑容器，组内成员共享组的权限位。"),
    step("useradd", "第二幕 · 创建用户与组", "创建 admin 用户",
         "创建一个名为 admin 的新用户",
         "useradd admin",
         lambda s: "admin" in s.users and has(s, "/home/admin"),
         "useradd 自动创建同名主目录，但不会自动设置密码。", points=3),
    step("passwd", "第二幕 · 创建用户与组", "设置 admin 密码",
         "为 admin 用户设置密码为 123456",
         "passwd admin",
         lambda s: out(s, "passwd: password updated successfully"),
         "root 给他人设密码不需要旧密码，设完需验证。", points=3),
    step("gpasswd", "第二幕 · 创建用户与组", "加入 ks 用户组",
         "将 admin 用户追加到 ks 用户组中",
         "gpasswd -a admin ks",
         lambda s: "ks" in s.users.get("admin", {}).get("groups", []),
         "gpasswd -a 追加组成员，不会破坏已有的附加组。", points=3),
    step("id1", "第二幕 · 创建用户与组", "查看 admin 信息",
         "查看 admin 的 UID、GID 及所属组信息",
         "id admin",
         lambda s: used(s, "id") and out(s, "admin") and "ks" in (s.last_output or ""),
         "id 一次看清 uid、gid 与附加组，是排查权限的常用命令。"),
    step("grepgroup", "第二幕 · 创建用户与组", "过滤组信息",
         "查看系统用户组配置文件，过滤出包含 ks 的组信息",
         "cat /etc/group | grep ks",
         lambda s: used(s, "cat", "grep") and "ks" in (s.last_output or ""),
         "/etc/group 是组信息的权威来源，最后一列是组成员。", points=3),

    # ---------------------------------------------------------------- 第三幕 · 切换身份与目录
    step("su1", "第三幕 · 切换身份与目录", "切换到 admin",
         "切换到刚刚创建的 admin 用户身份（输入密码 123456）",
         "su admin",
         lambda s: used(s, "su") and s.user == "admin",
         "root 可免密切换到任何普通用户。"),
    step("mkdirp", "第三幕 · 切换身份与目录", "创建多级目录",
         "在临时目录 /tmp 下，一次性创建多级目录 aa/bb/cc",
         "mkdir -p /tmp/aa/bb/cc",
         lambda s: used(s, "mkdir") and has_flag(s, "p")
         and has(s, "/tmp/aa/bb/cc"),
         "mkdir -p 自动补齐缺失的父目录，一次建多层。", points=3),
    step("cd1", "第三幕 · 切换身份与目录", "进入 aa/bb/cc",
         "切换到 /tmp/aa/bb/cc 目录",
         "cd /tmp/aa/bb/cc",
         lambda s: used(s, "cd") and s.cwd == "/tmp/aa/bb/cc",
         "cd 后跟绝对路径，可直接跳到目标目录。"),
    step("mkdir1", "第三幕 · 切换身份与目录", "创建 test_dir",
         "在当前目录下创建一个名为 test_dir 的空文件夹",
         "mkdir test_dir",
         lambda s: used(s, "mkdir") and has(s, "/tmp/aa/bb/cc/test_dir"),
         "mkdir 创建目录，相对路径会落在当前目录下。"),
    step("lsa", "第三幕 · 切换身份与目录", "查看所有内容",
         "查看当前目录下的所有内容（包含隐藏文件）",
         "ls -a",
         lambda s: used(s, "ls") and has_flag(s, "a"),
         "ls -a 显示隐藏文件，文件名以点开头的是隐藏文件。"),
    step("rmdir", "第三幕 · 切换身份与目录", "删除 test_dir",
         "删除刚刚创建的空文件夹 test_dir",
         "rmdir test_dir",
         lambda s: used(s, "rmdir") and not has(s, "/tmp/aa/bb/cc/test_dir"),
         "rmdir 只删空目录，非空目录会拒绝删除。"),

    # ---------------------------------------------------------------- 第四幕 · 文件与编辑
    step("touch", "第四幕 · 文件与编辑", "创建 code.sh",
         "在当前目录下，创建一个名为 code.sh 的空文件",
         "touch code.sh",
         lambda s: used(s, "touch") and has(s, "/tmp/aa/bb/cc/code.sh"),
         "touch 创建空文件；若文件已存在则只更新时间戳。"),
    step("vim1", "第四幕 · 文件与编辑", "编辑 code.sh",
         "使用 vim 编辑 code.sh，输入内容 echo \"hello\" 并保存退出",
         "vim code.sh",
         lambda s: s.vim is None and "hello" in content_of(s, "/tmp/aa/bb/cc/code.sh"),
         "vim 的 i 进插入、Esc 退插入、:wq 保存退出。", points=3),
    step("cat1", "第四幕 · 文件与编辑", "查看 code.sh 内容",
         "查看 code.sh 的文件内容",
         "cat code.sh",
         lambda s: used(s, "cat") and out(s, "hello"),
         "cat 适合看小文件，内容直接铺到终端。"),
    step("cp", "第四幕 · 文件与编辑", "复制 code.sh",
         "将当前目录下的 code.sh 复制一份到 /tmp 目录中，并命名为 copy.sh",
         "cp code.sh /tmp/copy.sh",
         lambda s: used(s, "cp") and has(s, "/tmp/copy.sh"),
         "cp 复制后原文件仍在。", points=3),
    step("mv", "第四幕 · 文件与编辑", "重命名 copy.sh",
         "将 /tmp 目录下的 copy.sh 重命名为 backup.sh",
         "mv /tmp/copy.sh /tmp/backup.sh",
         lambda s: used(s, "mv") and has(s, "/tmp/backup.sh") and not has(s, "/tmp/copy.sh"),
         "mv 在同一目录下是重命名，原文件名消失。", points=3),

    # ---------------------------------------------------------------- 第五幕 · 查找与权限
    step("find1", "第五幕 · 查找与权限", "搜索 .sh 文件",
         "在 /tmp 目录下搜索所有以 .sh 结尾的文件",
         "find /tmp -name \"*.sh\"",
         lambda s: used(s, "find") and out(s, ".sh"),
         "find -name 配合 * 通配符按文件名匹配。", points=3),
    step("lsl", "第五幕 · 查找与权限", "查看 code.sh 权限",
         "查看 code.sh 的详细权限信息",
         "ls -l code.sh",
         lambda s: used(s, "ls") and has_flag(s, "l") and out(s, "code.sh"),
         "ls -l 的九位权限串是 chmod 的判断依据。"),
    step("chmodux", "第五幕 · 查找与权限", "字母法加执行权限",
         "使用字母法，给 code.sh 的所有者增加执行权限",
         "chmod u+x code.sh",
         lambda s: used(s, "chmod") and "u+x" in s.last_input
         and mode_is(s, "/tmp/aa/bb/cc/code.sh", 0o744),
         "chmod u+x 只给所有者加执行位，不破坏其他人的权限。", points=3),
    step("chmod760", "第五幕 · 查找与权限", "数字法设 760",
         "使用数字法，将 code.sh 权限最终设置为：所有者7，同组6，其他人0",
         "chmod 760 code.sh",
         lambda s: used(s, "chmod") and mode_is(s, "/tmp/aa/bb/cc/code.sh", 0o760),
         "760 = 所有者 rwx、组 rw、其他人无。", points=3),

    # ---------------------------------------------------------------- 第六幕 · 查看与管道
    step("head1", "第六幕 · 查看与管道", "查看前 5 行",
         "查看 /etc/passwd 文件的前 5 行",
         "head -n 5 /etc/passwd",
         lambda s: used(s, "head") and out(s, "root"),
         "head -n 取文件开头若干行。"),
    step("tail1", "第六幕 · 查看与管道", "查看后 3 行",
         "查看 /etc/passwd 文件的最后 3 行",
         "tail -n 3 /etc/passwd",
         lambda s: used(s, "tail") and out(s, "admin"),
         "tail -n 取末尾行，查最新记录常用。"),
    step("less", "第六幕 · 查看与管道", "分页查看",
         "使用分页工具查看 /etc/passwd 文件（按 q 退出）",
         "less /etc/passwd",
         lambda s: s.less_views >= 1,
         "less 不会一次性灌满屏幕，可上下翻页，q 退出。"),
    step("grepadmin", "第六幕 · 查看与管道", "过滤 admin",
         "使用管道过滤出 /etc/passwd 中包含 admin 的用户信息",
         "cat /etc/passwd | grep admin",
         lambda s: used(s, "cat", "grep") and "admin" in (s.last_output or ""),
         "grep 过滤输出，配合管道可精准取出目标行。", points=3),
    step("wc1", "第六幕 · 查看与管道", "统计总行数",
         "使用管道统计 /etc/passwd 文件的总行数",
         "cat /etc/passwd | wc -l",
         lambda s: used(s, "cat", "wc") and (s.last_output or "").strip().isdigit(),
         "wc -l 统计行数，/etc/passwd 一行就是一个账号。", points=3),

    # ---------------------------------------------------------------- 第七幕 · 历史与查找
    step("histgrep", "第七幕 · 历史与查找", "过滤 vim 历史",
         "在历史命令中，过滤查找包含 vim 的命令",
         "history | grep vim",
         lambda s: used(s, "history", "grep") and "vim" in (s.last_output or ""),
         "history | grep 是复盘操作的高频组合。", points=3),
    step("which", "第七幕 · 历史与查找", "查找 tar 路径",
         "查找 tar 命令所在的绝对路径",
         "which tar",
         lambda s: used(s, "which") and out(s, "tar"),
         "which 在 PATH 里查找可执行文件的位置。"),
    step("find2", "第七幕 · 历史与查找", "全局查找 backup.sh",
         "从根目录 / 开始，全局查找名为 backup.sh 的文件",
         "find / -name \"backup.sh\"",
         lambda s: used(s, "find") and "backup.sh" in (s.last_output or ""),
         "find / 会遍历整棵树，生产环境要尽量缩小范围。", points=3),

    # ---------------------------------------------------------------- 第八幕 · 链接与清理
    step("ln", "第八幕 · 链接与清理", "创建软链接",
         "在 /tmp 目录下为 code.sh 创建一个名为 shortcut 的软链接",
         "ln -s /tmp/aa/bb/cc/code.sh /tmp/shortcut",
         lambda s: used(s, "ln") and has_flag(s, "s")
         and node(s, "/tmp/shortcut") is not None
         and node(s, "/tmp/shortcut").kind == "link",
         "ln -s 创建软链接，源文件删除后链接会失效。", points=3),
    step("rmrf", "第八幕 · 链接与清理", "强制递归删除",
         "强制递归删除整个多级目录 /tmp/aa，以及关联的软链接和 /tmp/backup.sh（一条命令执行）",
         "rm -rf /tmp/aa /tmp/shortcut /tmp/backup.sh",
         lambda s: used(s, "rm") and has_flag(s, "r") and has_flag(s, "f")
         and not has(s, "/tmp/aa") and not has(s, "/tmp/shortcut")
         and not has(s, "/tmp/backup.sh"),
         "rm -rf 递归且强制，是最高危命令，执行前必须核对路径。", points=3),

    # ---------------------------------------------------------------- 第九幕 · 收尾
    step("exit1", "第九幕 · 收尾", "退出 admin",
         "退出 admin 用户，回到 root 身份",
         "exit",
         lambda s: used(s, "exit") and s.user == "root",
         "exit 退出当前 shell 用户，回到 su 之前的状态。"),
    step("userdel", "第九幕 · 收尾", "删除 admin 用户",
         "删除 admin 用户，并同时删除其主目录",
         "userdel -r admin",
         lambda s: used(s, "userdel") and "admin" not in s.users and not has(s, "/home/admin"),
         "userdel -r 连主目录一起删；不加 -r 会留下垃圾目录。", points=3),
    step("groupdel", "第九幕 · 收尾", "删除 ks 用户组",
         "删除 ks 用户组",
         "groupdel ks",
         lambda s: used(s, "groupdel") and "ks" not in s.groups,
         "有用户以它为初始组时不能删，必须先把用户移走。"),
    step("grepnoadmin", "第九幕 · 收尾", "确认 admin 已删除",
         "确认 admin 用户已不存在于系统用户配置文件中",
         "cat /etc/passwd | grep admin",
         lambda s: used(s, "cat", "grep") and "admin" not in (s.last_output or ""),
         "grep 无匹配时无输出，确认账号已清理干净。"),
    step("clear", "第九幕 · 收尾", "清空屏幕",
         "使用命令清空当前终端屏幕内容",
         "clear",
         lambda s: s.last_input.strip() == "clear" and s.clear_flag,
         "clear 等价于 Ctrl+L，只清显示不清历史。"),
    step("history", "第九幕 · 收尾", "查看命令历史",
         "查看历史执行过的命令记录",
         "history",
         lambda s: used(s, "history") and (s.last_output or "").strip() != "",
         "history 记录本次会话输入过的命令。"),
    step("exit2", "第九幕 · 收尾", "退出 root",
         "退出 root 权限终端",
         "exit",
         lambda s: used(s, "exit") and s.user == "student",
         "收尾时离开高权限账号，是基本的安全习惯。", points=1),
]

# --------------------------------------------------------------------- 考题持久化
# 考题正文可被教师端编辑并落盘到 data/questions.json，评分逻辑（check）保持硬编码。
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUESTIONS_PATH = os.path.join(BASE_DIR, "data", "questions.json")

# 教师端可编辑字段白名单（check 为评分函数，不开放编辑）
EDITABLE_FIELDS = ("act", "title", "task", "expect", "why", "points")

# 保留一份原始默认值，用于“与默认 diff”与“重置为默认”
_DEFAULT_STEPS = [{k: v for k, v in st.items() if k != "check"} for st in STEPS]
_DEFAULT_MAP = {d["key"]: d for d in _DEFAULT_STEPS}


def _load_overrides():
    if not os.path.exists(QUESTIONS_PATH):
        return {}
    try:
        with open(QUESTIONS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("overrides") or {}
    except Exception:
        return {}


def _save_overrides(overrides):
    os.makedirs(os.path.dirname(QUESTIONS_PATH), exist_ok=True)
    with open(QUESTIONS_PATH, "w", encoding="utf-8") as fh:
        json.dump({"overrides": overrides}, fh, ensure_ascii=False, indent=2)


def _apply_overrides(overrides):
    """把教师保存的字段覆盖到 STEPS（check 保持不变）。

    必须先把每题恢复到硬编码默认值再覆盖：只覆盖不恢复时，「重置为默认」
    清空了 overrides，STEPS 里上一次的编辑值却原封不动留着，重置永远不生效。
    """
    for st in STEPS:
        d = _DEFAULT_MAP.get(st["key"]) or {}
        for f in EDITABLE_FIELDS:
            if f in d:
                st[f] = d[f]
        ov = overrides.get(st["key"]) or {}
        for f in EDITABLE_FIELDS:
            if f in ov and ov[f] is not None:
                st[f] = ov[f]


# 模块加载即应用持久化覆盖，保证教师端与学生端读到同一份考题
_apply_overrides(_load_overrides())


def all_questions():
    """返回全部考题的可编辑字段（按顺序，含 no 与 key），供教师端展示/编辑。"""
    return [{
        "key": st["key"], "no": i + 1, "act": st["act"], "title": st["title"],
        "task": st["task"], "expect": st["expect"],
        "why": st["why"], "points": st.get("points", 2),
    } for i, st in enumerate(STEPS)]


def save_questions(questions):
    """教师端提交考题列表：diff 出相对默认值的改动，持久化并热更新 STEPS。"""
    known = {st["key"] for st in STEPS}
    default_map = {d["key"]: d for d in _DEFAULT_STEPS}
    overrides = {}
    for q in questions:
        key = str(q.get("key", ""))
        if key not in known:
            continue
        d = default_map[key]
        diff = {}
        for f in EDITABLE_FIELDS:
            if f not in q:
                continue
            val = q.get(f)
            if f == "points":
                try:
                    val = max(1, min(100, int(val)))
                except (TypeError, ValueError):
                    val = d.get(f, 2)
            elif val is None:
                continue
            else:
                val = str(val).strip()
            if val != d.get(f):
                diff[f] = val
        if diff:
            overrides[key] = diff
    _save_overrides(overrides)
    _apply_overrides(overrides)
    return len(STEPS)


def reset_questions():
    """清空覆盖，恢复硬编码默认考题。"""
    _save_overrides({})
    _apply_overrides({})
    return len(STEPS)


# 由 server 注入：sid -> 真实虚拟机（用于恢复会话时重新绑定终端）
REAL_VM_LOOKUP = None


class ExamSession:
    TOTAL_SECONDS = 7200  # 120 分钟（未设置统一截止时间时使用）

    def __init__(self, sid, name, student_no="", deadline=None):
        self.sid = sid
        self.name = name
        self.student_no = student_no
        self.sim = LinuxSim()
        self.started = time.time()
        self.last_active = self.started
        self.deadline = deadline          # 统一交卷时间（时间戳），None 表示按个人 120 分钟
        self.finished_at = None
        self.submitted = False
        self.records = []                 # 已完成任务
        self.logs = []
        self.penalty = 0                  # 考试中退出全屏次数（每次扣 1 分）
        self.selected = STEPS[0]["key"] if STEPS else None

    # ------------------------------------------------------------------
    def _done_keys(self):
        return {r["key"] for r in self.records}

    def _task(self, key):
        for st in STEPS:
            if st["key"] == key:
                return st
        return None

    def _next_key(self):
        done = self._done_keys()
        for st in STEPS:
            if st["key"] not in done:
                return st["key"]
        return None

    @property
    def finished(self):
        return self.submitted or len(self.records) >= len(STEPS)

    def elapsed(self):
        end = self.finished_at or time.time()
        return max(0, int(end - self.started))

    def remaining(self):
        if self.deadline:
            return max(0, int(self.deadline - time.time()))
        return max(0, self.TOTAL_SECONDS - self.elapsed())

    # ------------------------------------------------------------------
    def exec(self, line):
        """兼容入口：模拟引擎执行一条命令并判定。"""
        return self.submit_line(line, self.sim.feed(line), fed=True)

    def submit_line(self, line, output, fed=False):
        """真实终端路径：命令已经在 Linux 中执行完，这里只做状态判定。"""
        if self.finished:
            return {"output": "本次考核已经交卷。", "finished": True, "passed": False,
                    "prompt": self.sim.prompt(), "remaining": self.remaining(),
                    "score": self.score(), "progress": self.progress(), "selected": None}
        if self.remaining() <= 0:
            self.finish()
            return {"output": "考试时间已到，系统已自动交卷。", "finished": True,
                    "passed": False, "prompt": "", "remaining": 0,
                    "score": self.score(), "progress": self.progress(), "selected": None}

        self.last_active = time.time()
        passed = False
        passed_key = None
        step = self._task(self.selected)
        if (step is not None and step["key"] not in self._done_keys()
                and line.strip() and step["check"](self.sim)):
            passed = True
            passed_key = step["key"]
            if step["key"] not in self._done_keys():
                self.records.append({
                    "key": step["key"], "title": step["title"], "act": step["act"],
                    "at": time.time(),
                })
            nxt = self._next_key()
            self.selected = nxt
            if nxt is None:
                self.finish()

        if line.strip():
            # 只存命令本身：教师端「机器命令日志」只读 in，命令输出没有任何
            # 消费方，而 dump() 会在学生每敲一次回车时整体写入 SQLite，存
            # 3000 字符输出会让单个会话的持久化体积涨到 700KB 以上。
            self.logs.append({"in": line[:500]})
            del self.logs[:-300]

        return {
            "output": output,
            "passed": passed,
            "passed_key": passed_key,
            "finished": self.finished,
            "selected": self.public_task(self.selected) if self.selected else None,
            "progress": self.progress(),
            "score": self.score(),
            "remaining": self.remaining(),
        }

    def force_pass(self, key):
        """教师端手动判定某题通过，绕过顺序判题，用于学生乱序卡住时救场。"""
        if self.finished:
            return {"ok": False, "error": "已交卷"}
        task = self._task(key)
        if task is None:
            return {"ok": False, "error": "题目不存在"}
        if key not in self._done_keys():
            self.records.append({
                "key": task["key"], "title": task["title"], "act": task["act"],
                "at": time.time(),
            })
        self.last_active = time.time()
        self.selected = self._next_key()
        if self.selected is None:
            self.finish()
        return {"ok": True, "passed_key": key, "selected": self.selected,
                "progress": self.progress(), "finished": self.finished}

    def public_task(self, key):
        step = self._task(key)
        if step is None:
            return None
        return {
            "key": step["key"], "act": step["act"], "title": step["title"],
            "task": step["task"],
            "done": step["key"] in self._done_keys(),
        }

    def public_tasks(self, include_expect=False):
        """学生端题目列表。

        include_expect 只对教师端开放：expect 就是该题的标准命令，下发给学生端
        等于把答案送进浏览器（F12 → 网络响应即可看到全部 41 条命令）。
        """
        done = self._done_keys()
        tasks = []
        for i, st in enumerate(STEPS):
            item = {
                "key": st["key"], "no": i + 1, "act": st["act"], "title": st["title"],
                "task": st["task"],
                "done": st["key"] in done,
            }
            if include_expect:
                item["expect"] = st["expect"]
            tasks.append(item)
        return tasks

    def progress(self):
        return {"done": len(self.records), "total": len(STEPS),
                "percent": round(len(self.records) / len(STEPS) * 100, 1)}

    def score(self):
        total_points = sum(st.get("points", 2) for st in STEPS)
        earned = sum(st.get("points", 2) for st in STEPS
                     if st["key"] in self._done_keys())
        if total_points <= 0:
            return 0.0
        base = earned / total_points * 100
        # 每次退出全屏扣 1 分，最低 0 分
        return max(0.0, round(base - self.penalty, 1))

    def add_penalty(self):
        """记录一次考试中退出全屏的违规，自动扣 1 分。"""
        self.penalty += 1
        self.last_active = time.time()

    def finish(self):
        self.submitted = True
        if self.finished_at is None:
            self.finished_at = time.time()

    def dump(self):
        return {
            "sid": self.sid, "name": self.name, "student_no": self.student_no,
            "started": self.started, "deadline": self.deadline,
            "last_active": self.last_active, "finished_at": self.finished_at,
            "submitted": self.submitted,
            "records": self.records, "logs": self.logs, "selected": self.selected,
            "penalty": self.penalty,
            "sim": self.sim.dump(),
        }

    @classmethod
    def restore(cls, data):
        obj = cls(data["sid"], data["name"], data.get("student_no", ""),
                  deadline=data.get("deadline"))
        sim_data = data.get("sim") or {}
        if sim_data.get("real"):
            from real_sim import RealSim
            vm = REAL_VM_LOOKUP(obj.sid) if REAL_VM_LOOKUP else None
            obj.sim = RealSim.restore(sim_data, vm) if vm is not None else RealSim(None)
        else:
            obj.sim = LinuxSim.restore(sim_data)
        obj.started = data.get("started", time.time())
        obj.last_active = data.get("last_active", obj.started)
        obj.finished_at = data.get("finished_at")
        obj.submitted = data.get("submitted", obj.finished_at is not None)
        obj.records = data.get("records", [])
        obj.logs = data.get("logs", [])
        obj.penalty = data.get("penalty", 0)
        obj.selected = data.get("selected") or obj._next_key()
        return obj

    def summary(self):
        total = len(STEPS)
        passed_keys = self._done_keys()
        detail = []
        for i, st in enumerate(STEPS):
            detail.append({
                "no": i + 1, "act": st["act"], "title": st["title"],
                "status": "pass" if st["key"] in passed_keys else "unfinished",
            })
        import shlex
        command_hits = {}
        for line in self.sim.history:
            for seg in line.split("|"):
                try:
                    argv = shlex.split(seg.strip())
                except ValueError:
                    continue
                if argv and argv[0] in PPT_COMMANDS:
                    command_hits[argv[0]] = command_hits.get(argv[0], 0) + 1
        return {
            "name": self.name, "student_no": self.student_no, "sid": self.sid,
            "score": self.score(), "passed": len(self.records), "total": total,
            "elapsed": self.elapsed(),
            "detail": detail, "commands": command_hits,
            "finished": self.finished, "submitted": self.submitted,
            "penalty": self.penalty,
        }
