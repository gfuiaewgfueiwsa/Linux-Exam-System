# -*- coding: utf-8 -*-
"""questions —— 简化版考题库（37 题 · 乱序判定 · 满分 100）。

场景母本沿用完整版的剧情骨架但砍到八幕：本次是给后端开发同事 dev 开通环境、铺发布
目录、核对权限，最后清理现场。链接与打包已经不在考范围内，收尾改成「删除发布目录 →
删号删组 → 自查历史」。所有题目只使用完整版 PPT 命令范围内已经实现的命令。

题号、题面、标准命令与分值严格对齐教师给的规划表（37 行原始分值合计 99，得分按
``earned / total_points * 100`` 归一，做满即 100 分；成绩统一取整为整数分，不改
任何一行的原始分值）。

与完整版的两点关键差异：

1. **乱序判定**：一条命令回车后遍历所有未通过的题目，命中即点亮，不再要求按
   题号顺序作答，因此也不再需要 selected 游标推进。
2. **防误亮双闸门**：乱序之后「状态为空」这类判据极易被提前满足（还没建 dev 就
   断言 dev 不存在），所以每题额外声明
   - ``need``：本轮命令链里必须出现的命令名（谁干的活才算谁的功）；
   - ``after``：必须先点亮的题号（因果前置，替代原来靠顺序天然形成的依赖）。

判分一律读沙箱真实状态（用户表 / 组表 / 文件树 / mode / 当前身份），不比对命令
字符串，所以同义写法（``grep x /etc/group`` 与 ``cat /etc/group | grep x``）都能过。
"""

from __future__ import annotations

import json
import os
import shlex
import time

from sandbox import LinuxSim

# 课堂范围内出现过的命令，教师端「命令使用统计」按这些名字计数
PPT_COMMANDS = [
    "sudo", "su", "touch", "mkdir", "rm", "rmdir", "cd", "vim", "cat",
    "pwd", "ls", "cp", "mv", "clear", "history", "whoami", "exit", "id",
    "find", "which", "ln", "tar", "less", "head", "tail", "wc", "grep",
    "chmod", "chown", "chgrp", "useradd", "passwd", "userdel", "usermod",
    "groupadd", "groupdel", "gpasswd", "groups",
]


# --------------------------------------------------------------------- 判分小工具
def used(sess, *names):
    """本轮命令链里是否出现了这些命令（管道各段与 sudo su 这类多段命令都算）。"""
    return all(n in sess.names for n in names)


def out(s, text):
    return text in (s.last_output or "")


def absent(s, text):
    return text not in (s.last_output or "")


def has(s, path):
    return s._node_at(path) is not None


def node(s, path):
    return s._node_or_link(path)


def mode_is(s, path, mode):
    n = node(s, path)
    return n is not None and n.mode == mode


def owner_is(s, path, owner=None, group=None):
    n = node(s, path)
    if n is None:
        return False
    if owner is not None and n.owner != owner:
        return False
    return not (group is not None and n.group != group)


def content_of(s, path):
    n = s._node_at(path)
    if n is None or n.kind != "file":
        return ""
    return s._read_file(n)


def flag(sess, ch):
    """命令里是否带了含 ch 的短选项（-p、-a、-rf、-czf 之类）。"""
    for token in (sess.line or "").split():
        if token.startswith("-") and not token.startswith("--") and ch in token[1:]:
            return True
    return False


def done(sess, key):
    return sess.is_done(key)


def hist_shown(s, sess):
    """裸 history 这一轮要把真实历史记录铺出来。

    带管道的 history（``history | grep passwd`` 那条题）不算「查看历史命令记录」，
    否则一条命令顺手蹭到两题。
    """
    if "|" in (sess.line or ""):
        return False
    prior = [h for h in s.history if h.strip() != "history"]
    return bool(prior) and prior[0] in (s.last_output or "")


def step(key, act, title, task, expect, check, why, points=2, need=(), after=()):
    return dict(key=key, act=act, title=title, task=task, expect=expect,
                check=check, why=why, points=points, need=list(need),
                after=list(after))


HOME = "/home/dev"
WWW = HOME + "/www"

# 幕只用于教师端「考题管理」分组与成绩明细，学生端题单是平铺的 1–37
A1 = "第一幕 · 身份与提权"
A2 = "第二幕 · 开通账号与用户组"
A3 = "第三幕 · 切换身份与铺目录"
A4 = "第四幕 · 内容与归属"
A5 = "第五幕 · 权限核查与设定"
A6 = "第六幕 · 查看与管道"
A7 = "第七幕 · 查找与定位"
A8 = "第八幕 · 清理与收尾"

STEPS = [
    # ============================================================ 第一幕 · 身份与提权
    step("whoami_me", A1, "确认登录身份",
         "确认当前登录的用户身份",
         "whoami",
         lambda s, sess: used(sess, "whoami") and out(s, "student"),
         "whoami 打印有效用户名，是排查权限问题的第一步。",
         points=1),
    step("pwd_home", A1, "查看当前目录",
         "查看当前所在目录",
         "pwd",
         lambda s, sess: used(sess, "pwd") and s.cwd == "/home/student",
         "登录后 shell 的当前目录就是该账号的家目录。",
         points=1),
    step("sudo_su", A1, "提权到 root",
         "提权到 root 超级管理员（按提示输入 root 口令）",
         "sudo su",
         lambda s, sess: used(sess, "sudo") and s.user == "root"
         and len(s.stack) == 1 and s.stack[-1][0] == "student",
         "sudo su 让普通用户临时取得 root 权限，提示符结尾由 $ 变成 #。",
         points=2, need=["sudo"]),

    # ====================================================== 第二幕 · 开通账号与用户组
    step("groupadd_devops", A2, "创建 devops 组",
         "创建一个名为 devops 的用户组",
         "groupadd devops",
         lambda s, sess: used(sess, "groupadd") and "devops" in s.groups,
         "用户组是权限的逻辑容器，一个组可以整体授权给一批人。",
         points=2, need=["groupadd"]),
    step("useradd_dev", A2, "创建 dev 用户",
         "创建一个名为 dev 的新用户",
         "useradd dev",
         lambda s, sess: used(sess, "useradd") and "dev" in s.users
         and has(s, HOME),
         "useradd 会自动建出同名家目录，但不会自动设置密码。",
         points=2, need=["useradd"]),
    step("passwd_dev", A2, "设置 dev 密码",
         "为 dev 用户设置密码 888888",
         "passwd dev",
         lambda s, sess: used(sess, "passwd")
         and s.users.get("dev", {}).get("password") == "888888",
         "root 给别人设密码不需要旧密码，但需要连续输入两次以确认。",
         points=3, need=["passwd"], after=["useradd_dev"]),
    step("gpasswd_dev", A2, "dev 加入 devops",
         "将 dev 用户追加到 devops 用户组",
         "gpasswd -a dev devops",
         lambda s, sess: used(sess, "gpasswd")
         and "devops" in s.users.get("dev", {}).get("groups", []),
         "gpasswd -a 是追加成员，不会破坏该用户已有的附加组。",
         points=4, need=["gpasswd"], after=["groupadd_devops", "useradd_dev"]),
    step("id_dev", A2, "查看 dev 账号信息",
         "查看 dev 用户的 UID、GID 及所属组",
         "id dev",
         lambda s, sess: used(sess, "id") and out(s, "uid=") and "dev" in (s.last_output or ""),
         "id 一行看齐 uid/gid/附加组，是权限排查的常用命令。",
         points=2, need=["id"], after=["useradd_dev"]),
    step("grep_group", A2, "过滤组配置",
         "过滤出包含 devops 的用户组配置信息",
         "grep devops /etc/group",
         lambda s, sess: used(sess, "grep") and "devops" in (s.last_output or ""),
         "/etc/group 是组信息的权威来源，最后一列是组成员。",
         points=3, need=["grep"], after=["groupadd_devops"]),

    # ====================================================== 第三幕 · 切换身份与铺目录
    step("su_dev", A3, "切换到 dev",
         "切换到 dev 用户",
         "su dev",
         lambda s, sess: used(sess, "su") and s.user == "dev",
         "root 切换到任何普通用户都不需要输密码。",
         points=2, need=["su"], after=["useradd_dev"]),
    step("mkdir_p", A3, "创建多级目录",
         "在 dev 家目录下一次性创建多级目录 www/html/static",
         "mkdir -p ~/www/html/static",
         lambda s, sess: used(sess, "mkdir") and flag(sess, "p")
         and has(s, WWW + "/html/static"),
         "mkdir -p 自动补齐缺失的父目录，已存在的层级也不报错。",
         points=5, need=["mkdir"]),
    step("cd_static", A3, "进入 static 目录",
         "切换到 /home/dev/www/html/static 目录",
         "cd /home/dev/www/html/static",
         lambda s, sess: used(sess, "cd") and s.cwd == WWW + "/html/static",
         "cd 跟绝对路径可以直接跳到任意层级目录。",
         points=2, need=["cd"], after=["mkdir_p"]),
    step("touch_page", A3, "创建空页面",
         "在当前目录下创建一个名为 page.html 的空文件",
         "touch page.html",
         lambda s, sess: used(sess, "touch") and has(s, WWW + "/html/static/page.html"),
         "touch 建新空文件；文件已存在时只把时间戳推到当下。",
         points=2, need=["touch"]),
    step("mkdir_icons", A3, "创建 icons 目录",
         "在当前目录下创建一个名为 icons 的空目录",
         "mkdir icons",
         lambda s, sess: used(sess, "mkdir") and has(s, WWW + "/html/static/icons"),
         "不带路径的 mkdir 落在当前工作目录下。",
         points=1, need=["mkdir"], after=["cd_static"]),
    step("ls_a", A3, "查看全部内容",
         "查看当前目录下所有内容（包含隐藏文件）",
         "ls -a",
         lambda s, sess: used(sess, "ls") and flag(sess, "a"),
         "ls -a 连隐藏文件一起列出，排查配置常靠它。",
         points=2, need=["ls"], after=["cd_static"]),
    step("rmdir_icons", A3, "删除 icons 目录",
         "删除 icons 空目录",
         "rmdir icons",
         lambda s, sess: used(sess, "rmdir") and not has(s, WWW + "/html/static/icons"),
         "rmdir 只删空目录，目录里有东西会直接拒绝，防误删。",
         points=2, need=["rmdir"], after=["mkdir_icons"]),

    # ====================================================== 第四幕 · 内容与归属
    step("vim_page", A4, "vim 编辑页面",
         "使用 vim 编辑 page.html，写入一行 hello release 后保存退出",
         "vim page.html",
         lambda s, sess: used(sess, "vim") and s.vim is None
         and "hello" in content_of(s, WWW + "/html/static/page.html"),
         "vim 的 i 进插入、Esc 退插入、:wq 保存退出。",
         points=4, need=["vim"], after=["touch_page"]),
    step("cat_page", A4, "查看页面内容",
         "查看 page.html 的文件内容",
         "cat page.html",
         lambda s, sess: used(sess, "cat") and out(s, "hello"),
         "cat 适合看小文件，内容直接铺到终端。",
         points=2, need=["cat"], after=["vim_page"]),
    step("cp_index", A4, "复制成 index.html",
         "将 page.html 复制到 /home/dev/www 并命名为 index.html",
         "cp page.html /home/dev/www/index.html",
         lambda s, sess: used(sess, "cp") and has(s, WWW + "/index.html"),
         "cp 复制后原文件仍在，改动可以重来。",
         points=5, need=["cp"], after=["touch_page"]),
    step("mv_home", A4, "改名为 home.html",
         "将 /home/dev/www/index.html 重命名为 home.html",
         "mv /home/dev/www/index.html /home/dev/www/home.html",
         lambda s, sess: used(sess, "mv") and has(s, WWW + "/home.html")
         and not has(s, WWW + "/index.html"),
         "mv 在同一目录里就是重命名，改名后旧名字消失。",
         points=5, need=["mv"], after=["cp_index"]),
    step("exit_dev", A4, "退出 dev 回 root",
         "退出 dev 用户，回到 root 身份",
         "exit",
         lambda s, sess: used(sess, "exit") and s.user == "root",
         "exit 结束当前这层 shell，身份退回调用它的那个人。",
         points=2, need=["exit"], after=["su_dev"]),

    # ====================================================== 第五幕 · 权限核查与设定
    step("ls_l_home", A5, "查看详细权限",
         "查看 /home/dev/www/home.html 的详细权限信息",
         "ls -l /home/dev/www/home.html",
         lambda s, sess: used(sess, "ls") and flag(sess, "l")
         and "home.html" in (s.last_output or ""),
         "ls -l 的九位权限串是判断该用 chmod 哪种写法的依据。",
         points=2, need=["ls"], after=["mv_home"]),
    step("chmod_gw", A5, "组加写权限",
         "使用字母法给 /home/dev/www/home.html 的所属组增加写权限，其他权限位保持不变",
         "chmod g+w /home/dev/www/home.html",
         lambda s, sess: used(sess, "chmod") and "g+w" in (sess.line or "")
         and mode_is(s, WWW + "/home.html", 0o664),
         "chmod g+w 只改组的那三位，属主和其他人的权限保持原样。",
         points=5, need=["chmod"], after=["mv_home"]),
    step("chgrp_static", A5, "改 static 属组",
         "将 /home/dev/www/html/static 目录的所属组改为 devops",
         "chgrp devops /home/dev/www/html/static",
         lambda s, sess: used(sess, "chgrp")
         and owner_is(s, WWW + "/html/static", group="devops"),
         "chgrp 只动属组；文件的属主不变，属主本人就有权改属组。",
         points=3, need=["chgrp"], after=["groupadd_devops", "mkdir_p"]),

    # ====================================================== 第六幕 · 查看与管道
    step("head_passwd", A6, "看前两行",
         "查看 /etc/passwd 文件的前 2 行",
         "head -n 2 /etc/passwd",
         lambda s, sess: used(sess, "head") and "-n" in (sess.line or "").split()
         and out(s, "student:") and absent(s, "dev:"),
         "head -n 取文件开头若干行，看表头比 cat 整个文件省眼睛。",
         points=2, need=["head"]),
    step("tail_note", A6, "看最后一行",
         "查看 /home/student/notes.txt 文件的最后 1 行",
         "tail -n 1 /home/student/notes.txt",
         lambda s, sess: used(sess, "tail") and "-n" in (sess.line or "").split()
         and out(s, "日志") and absent(s, "交接备忘"),
         "tail -n 取末尾行，查最新记录最常用。",
         points=2, need=["tail"]),
    step("less_passwd", A6, "分页查看账号表",
         "使用分页工具查看 /etc/passwd 文件",
         "less /etc/passwd",
         lambda s, sess: used(sess, "less") and s.less_views >= 1,
         "less 不会一次灌满屏幕，空格翻页、q 退出。",
         points=2, need=["less"]),
    step("wc_note", A6, "管道统计行数",
         "使用管道统计 /home/student/notes.txt 文件的行数",
         "cat /home/student/notes.txt | wc -l",
         lambda s, sess: used(sess, "cat", "wc") and (s.last_output or "").strip().isdigit(),
         "wc -l 数行数，配合管道可以统计任意命令的输出。",
         points=4, need=["wc"]),
    step("hist_passwd", A6, "过滤历史命令",
         "在历史命令中过滤出包含 passwd 的记录",
         "history | grep passwd",
         lambda s, sess: used(sess, "history", "grep") and "passwd" in (s.last_output or ""),
         "history | grep 是复盘自己做过什么的高频组合。",
         points=4, need=["history"], after=["passwd_dev"]),

    # ====================================================== 第七幕 · 查找与定位
    step("find_html", A7, "按名字找文件",
         "在 /home/dev/www 目录下查找所有以 .html 结尾的文件",
         "find /home/dev/www -name \"*.html\"",
         lambda s, sess: used(sess, "find") and ".html" in (s.last_output or ""),
         "find -name 配合 * 通配符按文件名匹配整棵子树。",
         points=5, need=["find"], after=["touch_page"]),
    step("which_grep", A7, "定位命令路径",
         "查找 grep 命令所在的绝对路径",
         "which grep",
         lambda s, sess: used(sess, "which") and out(s, "/usr/bin/grep"),
         "which 在 PATH 里找可执行文件，排查「命令找不到」先看它。",
         points=2, need=["which"]),

    # ====================================================== 第八幕 · 清理与收尾
    step("rm_tree", A8, "删除发布目录",
         "强制递归删除整个发布目录 /home/dev/www",
         "rm -rf /home/dev/www",
         lambda s, sess: used(sess, "rm") and flag(sess, "r") and flag(sess, "f")
         and not has(s, WWW),
         "rm -rf 递归又强制，是最高危命令，回车前必须核对路径。",
         points=4, need=["rm"], after=["mkdir_p"]),
    step("userdel_dev", A8, "删除 dev 用户",
         "删除 dev 用户",
         "userdel dev",
         lambda s, sess: used(sess, "userdel") and "dev" not in s.users,
         "userdel 删账号；家目录要单独清，默认留在原地。",
         points=2, need=["userdel"], after=["useradd_dev"]),
    step("groupdel_devops", A8, "删除 devops 组",
         "删除 devops 用户组",
         "groupdel devops",
         lambda s, sess: used(sess, "groupdel") and "devops" not in s.groups,
         "还有用户拿它当初始组时删不掉，必须先把人移走。",
         points=2, need=["groupdel"], after=["groupadd_devops"]),
    step("grep_gone", A8, "确认账号已删除",
         "确认 dev 用户已不存在于系统用户配置文件中",
         "cat /etc/passwd | grep dev",
         lambda s, sess: used(sess, "grep") and "dev" in (sess.line or "")
         and "dev" not in (s.last_output or ""),
         "grep 无匹配时没有输出，正好用来确认清理到位。",
         points=4, need=["grep"], after=["userdel_dev"]),
    step("clear_screen", A8, "清空屏幕",
         "清空当前终端屏幕内容",
         "clear",
         lambda s, sess: used(sess, "clear") and s.last_input.strip() == "clear"
         and s.clear_flag,
         "clear 只清显示，不清历史，收尾前把屏幕归零。",
         points=1, need=["clear"]),
    step("hist_all", A8, "查看历史命令",
         "查看历史执行过的命令记录",
         "history",
         lambda s, sess: used(sess, "history") and hist_shown(s, sess),
         "history 把本次会话的命令按序号列出来，交卷前自查用。",
         points=1, need=["history"]),
]

# --------------------------------------------------------------------- 题库持久化
# 教师端可编辑正文并落盘到 data/questions.json；判分逻辑（check/need/after）不开放编辑。
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUESTIONS_PATH = os.path.join(BASE_DIR, "data", "questions.json")
EDITABLE_FIELDS = ("act", "title", "task", "expect", "why", "points")

_DEFAULT_STEPS = [{k: v for k, v in st.items() if k != "check"} for st in STEPS]
_DEFAULT_MAP = {d["key"]: d for d in _DEFAULT_STEPS}


def _load_overrides():
    if not os.path.exists(QUESTIONS_PATH):
        return {}
    try:
        with open(QUESTIONS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh).get("overrides") or {}
    except Exception:
        return {}


def _save_overrides(overrides):
    os.makedirs(os.path.dirname(QUESTIONS_PATH), exist_ok=True)
    with open(QUESTIONS_PATH, "w", encoding="utf-8") as fh:
        json.dump({"overrides": overrides}, fh, ensure_ascii=False, indent=2)


def _apply_overrides(overrides):
    """先回落到硬编码默认值再叠加覆盖，否则「重置为默认」清不掉上一次的编辑。"""
    for st in STEPS:
        d = _DEFAULT_MAP.get(st["key"]) or {}
        for f in EDITABLE_FIELDS:
            if f in d:
                st[f] = d[f]
        ov = overrides.get(st["key"]) or {}
        for f in EDITABLE_FIELDS:
            if ov.get(f) is not None:
                st[f] = ov[f]


_apply_overrides(_load_overrides())


def all_questions():
    return [{
        "key": st["key"], "no": i + 1, "act": st["act"], "title": st["title"],
        "task": st["task"], "expect": st["expect"], "why": st["why"],
        "points": st.get("points", 2),
    } for i, st in enumerate(STEPS)]


def save_questions(questions):
    known = {st["key"] for st in STEPS}
    overrides = {}
    for q in questions:
        key = str(q.get("key", ""))
        if key not in known:
            continue
        d = _DEFAULT_MAP[key]
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
    _save_overrides({})
    _apply_overrides({})
    return len(STEPS)


class ExamSession:
    """一场考试：姓名绑定的沙箱 + 乱序判定 + 逐题点亮。"""

    TOTAL_SECONDS = 7200

    def __init__(self, sid, name, student_no="", deadline=None):
        self.sid = sid
        self.name = name
        self.student_no = student_no
        self.sim = LinuxSim()
        self.started = time.time()
        self.last_active = self.started
        self.deadline = deadline
        self.finished_at = None
        self.submitted = False
        self.records = []
        self.logs = []
        self.penalty = 0
        # 命令链归属：sudo su 之后还要喂密码、vim 要喂正文与 :wq、less 要喂 q，
        # 这些续行本身不含命令名，必须一路记回最初那条命令，否则判分时
        # 「谁干的」这条线索就断了（sim.last_names 在续行里会被清空）。
        self.line = ""
        self.names = []

    # ------------------------------------------------------------------
    def is_done(self, key):
        return any(r["key"] == key for r in self.records)

    def _task(self, key):
        for st in STEPS:
            if st["key"] == key:
                return st
        return None

    @property
    def finished(self):
        return self.submitted or len(self.records) >= len(STEPS)

    def elapsed(self):
        return max(0, int((self.finished_at or time.time()) - self.started))

    def remaining(self):
        if self.deadline:
            return max(0, int(self.deadline - time.time()))
        return max(0, self.TOTAL_SECONDS - self.elapsed())

    # ------------------------------------------------------------------ 执行一条输入
    def run(self, line):
        """把一行输入喂给沙箱，交互流程走完（不再等密码/不在 vim、less 里）后判题。"""
        line = str(line or "")[:500]
        busy_before = bool(self.sim.pending or self.sim.vim or self.sim.pager)
        if not busy_before:
            self.line = line
            self.names = self._names_of(line)
        output = self.sim.feed(line)
        busy_after = bool(self.sim.pending or self.sim.vim or self.sim.pager)
        if line.strip() and not busy_before:
            # 只记「一条真正的命令」：密码续行、vim 正文与 :wq、less 的 q 都不是
            # shell 命令，真实 bash 也不会把它们写进 history；把它们存进日志等于
            # 把明文密码抄给教师端和学生自己的回显。
            self.logs.append({"in": line[:500]})
            del self.logs[:-300]
        self.last_active = time.time()

        if busy_after:
            # 还在等密码 / 还在 vim / 还在 less：这一行不配判题，也不配点亮任何题
            return self._result(output, [], awaiting=True)
        if self.finished:
            return self._result("本次考核已经交卷。", [], finished=True)
        if self.remaining() <= 0:
            self.finish()
            return self._result("考试时间已到，系统已自动交卷。", [], finished=True)
        return self._result(output, self._grade())

    @staticmethod
    def _names_of(line):
        """取这行命令里每个管道段的命令名，作为「谁干的」归属依据。

        只认每段第一个非选项 token：``sudo su`` 归给 sudo（不归给 su），
        ``cat x | grep y`` 同时归给 cat 与 grep。
        """
        names = []
        for segment in LinuxSim._split_pipe(line):
            try:
                argv = shlex.split(segment.strip())
            except ValueError:
                argv = segment.split()
            for token in argv:
                if token.startswith("-") or "=" in token:
                    continue
                base = os.path.basename(token)
                if base and base not in names:
                    names.append(base)
                break
        return names

    def _grade(self):
        """遍历所有未点亮的题，命中判据就点亮，返回本次新点亮的题号列表。"""
        lit = []
        if not self.line.strip():
            return lit
        for st in STEPS:
            if self.is_done(st["key"]):
                continue
            if st["need"] and not all(n in self.names for n in st["need"]):
                continue
            if st["after"] and not all(self.is_done(k) for k in st["after"]):
                continue
            try:
                hit = bool(st["check"](self.sim, self))
            except Exception:
                hit = False
            if hit:
                self.records.append({"key": st["key"], "title": st["title"],
                                     "act": st["act"], "at": time.time()})
                lit.append(st["key"])
        return lit

    def _result(self, output, lit, awaiting=False, finished=None):
        return {
            "output": output,
            "prompt": self.sim.prompt(),
            "badge": self.sim.mode_badge(),
            "awaiting": awaiting,
            # 真实终端里 clear 会擦掉屏幕，沙箱只用标志位记录，画面交给前端擦
            "cleared": bool(self.sim.clear_flag),
            "passed": bool(lit),
            "passed_keys": lit,
            "passed_titles": [self._task(k)["title"] for k in lit if self._task(k)],
            "lit_points": sum((self._task(k) or {}).get("points", 2) for k in lit),
            "tasks": self.public_tasks(),
            "progress": self.progress(),
            "score": self.score(),
            "remaining": self.remaining(),
            "elapsed": self.elapsed(),
            "finished": self.finished if finished is None else finished,
            "total": len(STEPS),
        }

    # ------------------------------------------------------------------
    def force_pass(self, key):
        """教师手动点亮某题。"""
        if self.finished:
            return {"ok": False, "error": "已交卷"}
        task = self._task(key)
        if task is None:
            return {"ok": False, "error": "题目不存在"}
        if not self.is_done(key):
            self.records.append({"key": key, "title": task["title"], "act": task["act"],
                                 "at": time.time()})
        self.last_active = time.time()
        return {"ok": True, "passed_key": key, "progress": self.progress(),
                "finished": self.finished}

    def public_tasks(self, include_expect=False):
        """学生端题单。

        expect 就是标准命令，只对教师端下发；给学生端等于把 37 条答案送进浏览器。
        """
        items = []
        for i, st in enumerate(STEPS):
            item = {"key": st["key"], "no": i + 1, "act": st["act"], "title": st["title"],
                    "task": st["task"], "points": st.get("points", 2),
                    "done": self.is_done(st["key"])}
            if include_expect:
                item["expect"] = st["expect"]
            items.append(item)
        return items

    def public_task(self, key):
        st = self._task(key)
        if st is None:
            return None
        return {"key": st["key"], "act": st["act"], "title": st["title"],
                "task": st["task"], "done": self.is_done(st["key"])}

    def progress(self):
        return {"done": len(self.records), "total": len(STEPS),
                "percent": round(len(self.records) / len(STEPS) * 100, 1)}

    def score(self):
        """百分制成绩，统一为整数分。

        原始分值表合计 99，按 earned / 99 * 100 归一后四舍五入取整；罚分是整数，
        先扣分再取整，避免出现 87.4 这类小数分。做满仍是 100 分。
        """
        total_points = sum(st.get("points", 2) for st in STEPS)
        earned = sum(st.get("points", 2) for st in STEPS if self.is_done(st["key"]))
        if total_points <= 0:
            return 0
        return max(0, int(round(earned / total_points * 100 - self.penalty)))

    def add_penalty(self):
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
            "submitted": self.submitted, "records": self.records, "logs": self.logs,
            "penalty": self.penalty, "sim": self.sim.dump(),
        }

    @classmethod
    def restore(cls, data):
        obj = cls(data["sid"], data["name"], data.get("student_no", ""),
                  deadline=data.get("deadline"))
        obj.sim = LinuxSim.restore(data.get("sim") or {})
        obj.started = data.get("started", time.time())
        obj.last_active = data.get("last_active", obj.started)
        obj.finished_at = data.get("finished_at")
        obj.submitted = data.get("submitted", obj.finished_at is not None)
        obj.records = data.get("records", [])
        obj.logs = data.get("logs", [])
        obj.penalty = data.get("penalty", 0)
        return obj

    def summary(self):
        keys = {r["key"] for r in self.records}
        detail = [{"no": i + 1, "act": st["act"], "title": st["title"],
                   "status": "pass" if st["key"] in keys else "unfinished"}
                  for i, st in enumerate(STEPS)]
        import shlex
        hits = {}
        for line in self.sim.history:
            for seg in line.split("|"):
                try:
                    argv = shlex.split(seg.strip())
                except ValueError:
                    continue
                if argv and argv[0] in PPT_COMMANDS:
                    hits[argv[0]] = hits.get(argv[0], 0) + 1
        return {
            "name": self.name, "student_no": self.student_no, "sid": self.sid,
            "score": self.score(), "passed": len(self.records), "total": len(STEPS),
            "elapsed": self.elapsed(), "detail": detail, "commands": hits,
            "finished": self.finished, "submitted": self.submitted,
            "penalty": self.penalty,
        }
