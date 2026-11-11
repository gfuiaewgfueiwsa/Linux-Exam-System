# -*- coding: utf-8 -*-
"""selfcheck —— 简化版判分回归：逐题验证「按参考答案走一遍能全部点亮」。

    python tests/selfcheck.py

三组断言：
1. 题库结构：37 题、分值合计与教师规划表逐行一致、题号唯一、need/after 引用的 key 都存在。
2. 顺序通关：按 expect 的参考命令走一遍，37 题全部点亮、满分交卷。
3. 乱序安全：把无关命令穿插进去、以及「提前做后面的题」不能让题误亮。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import questions  # noqa: E402
from questions import STEPS, ExamSession  # noqa: E402

PASS = 0
FAIL = []


def ok(cond, label, extra=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(f"{label} {extra}")


# ------------------------------------------------------------------ 1 题库结构
def check_structure():
    ok(len(STEPS) == 37, "题量", f"实际 {len(STEPS)}")
    total_points = sum(st.get("points", 2) for st in STEPS)
    # 原始分值严格按教师规划表逐行录入，37 行合计 99；得分按 earned/total*100 归一，
    # 所以做满仍然是 100 分，不为了凑整去改任何一行分值。
    ok(total_points == 99, "分值合计对齐规划表", f"实际 {total_points}")
    keys = [st["key"] for st in STEPS]
    ok(len(set(keys)) == len(keys), "key 唯一")
    known = set(keys)
    for st in STEPS:
        for k in st["after"]:
            ok(k in known, f"{st['key']} 的前置题存在", k)
        ok(isinstance(st["need"], list), f"{st['key']} need 合法")
        ok(bool(st["expect"].strip()), f"{st['key']} 有参考答案")
        ok(bool(st["task"].strip()) and bool(st["title"].strip()), f"{st['key']} 题干完整")
    for act in ("第一幕", "第二幕", "第三幕", "第四幕",
                "第五幕", "第六幕", "第七幕", "第八幕"):
        ok(any(st["act"].startswith(act) for st in STEPS), f"含 {act}")
    # 规划表逐行核对：题号 → 标准命令 + 分值
    spec = [("whoami", 1), ("pwd", 1), ("sudo su", 2), ("groupadd devops", 2),
            ("useradd dev", 2), ("passwd dev", 3), ("gpasswd -a dev devops", 4),
            ("id dev", 2), ("grep devops /etc/group", 3), ("su dev", 2),
            ("mkdir -p ~/www/html/static", 5), ("cd /home/dev/www/html/static", 2),
            ("touch page.html", 2), ("mkdir icons", 1), ("ls -a", 2), ("rmdir icons", 2),
            ("vim page.html", 4), ("cat page.html", 2),
            ("cp page.html /home/dev/www/index.html", 5),
            ("mv /home/dev/www/index.html /home/dev/www/home.html", 5), ("exit", 2),
            ("ls -l /home/dev/www/home.html", 2), ("chmod g+w /home/dev/www/home.html", 5),
            ("chgrp devops /home/dev/www/html/static", 3), ("head -n 2 /etc/passwd", 2),
            ("tail -n 1 /home/student/notes.txt", 2), ("less /etc/passwd", 2),
            ("cat /home/student/notes.txt | wc -l", 4), ("history | grep passwd", 4),
            ('find /home/dev/www -name "*.html"', 5), ("which grep", 2),
            ("rm -rf /home/dev/www", 4), ("userdel dev", 2), ("groupdel devops", 2),
            ("cat /etc/passwd | grep dev", 4), ("clear", 1), ("history", 1)]
    for i, (expect, points) in enumerate(spec):
        st = STEPS[i]
        ok(st["expect"] == expect, f"第 {i + 1} 题标准命令对齐",
           f"{st['expect']!r} ≠ {expect!r}")
        ok(st["points"] == points, f"第 {i + 1} 题分值对齐", str(st["points"]))
    for gone in ("ln_link", "tar_pack", "tar_unpack", "exit_root"):
        ok(gone not in known, f"{gone} 已按新表删除")


# ------------------------------------------------------------------ 参考解答
# 每一项是一条「学生输入」，多行之间用换行分开表示回车多次（密码、vim 正文都算续行）
SOLUTION = [
    "whoami",
    "pwd",
    "sudo su", "root",
    "groupadd devops",
    "useradd dev",
    "passwd dev", "888888", "888888",
    "gpasswd -a dev devops",
    "id dev",
    "grep devops /etc/group",
    "su dev",
    "mkdir -p ~/www/html/static",
    "cd /home/dev/www/html/static",
    "touch page.html",
    "mkdir icons",
    "ls -a",
    "rmdir icons",
    "vim page.html", "i", "hello release", "ESC", ":wq",
    "cat page.html",
    "cp page.html /home/dev/www/index.html",
    "mv /home/dev/www/index.html /home/dev/www/home.html",
    "exit",                                     # 退出 dev 回 root（chmod/chgrp 需要 root）
    "ls -l /home/dev/www/home.html",
    "chmod g+w /home/dev/www/home.html",
    "chgrp devops /home/dev/www/html/static",
    "chmod 750 /home/dev/www/html",             # 规划表外：不点亮任何题
    "head -n 2 /etc/passwd",
    "tail -n 1 /home/student/notes.txt",
    "less /etc/passwd", "q",
    "cat /home/student/notes.txt | wc -l",
    "history | grep passwd",
    "find /home/dev/www -name \"*.html\"",
    "which grep",
    "rm -rf /home/dev/www",
    "userdel dev",
    "groupdel devops",
    "cat /etc/passwd | grep dev",
    "clear",
    "history",
]


def play(lines, session=None):
    sess = session or ExamSession("sid-check", "检查员")
    lit = []
    for line in lines:
        r = sess.run(line)
        lit.extend(r["passed_keys"])
    return sess, lit


def check_solution():
    sess, lit = play(SOLUTION)
    missing = [st["key"] for st in STEPS if not sess.is_done(st["key"])]
    ok(not missing, "顺序通关点亮全部 37 题", f"未点亮：{missing}")
    ok(len(lit) == 37, "每题只点亮一次", f"实际点亮 {len(lit)} 次")
    ok(sess.score() == 100.0, "做满归一化成 100 分", f"实际 {sess.score()}")
    ok(isinstance(sess.score(), int), "满分是整数分", repr(sess.score()))
    partial, _ = play(SOLUTION[:6])
    ok(isinstance(partial.score(), int) and partial.score() > 0,
       "中途分数也是整数分", repr(partial.score()))
    ok(sess.finished, "全部点亮即交卷")
    summary = sess.summary()
    ok(summary["passed"] == 37 and summary["total"] == 37, "成绩单口径一致")


def check_free_order():
    """乱序：穿插无效命令、以及提前做后面的题，都不许误亮。"""
    sess = ExamSession("sid-free", "乱序考生")
    noise = [
        "cat /etc/passwd | grep dev",       # 还没建 dev，确认题不能提前亮
        "groupdel devops",                  # 还没建组，删除题不能提前亮
        "userdel dev",                      # 还没建用户
        "rm -rf /home/dev/www",             # 还没铺目录树
        "ls -a", "whoami", "id dev", "grep devops /etc/group",
        "clear", "exit", "pwd", "which grep", "head -n 2 /etc/passwd",
    ]
    for line in noise:
        sess.run(line)
    lit = [st["key"] for st in STEPS if sess.is_done(st["key"])]
    for must_not in ("grep_gone", "groupdel_devops", "userdel_dev", "rm_tree",
                     "chgrp_static", "id_dev", "grep_group", "passwd_dev",
                     "gpasswd_dev", "su_dev", "hist_passwd", "exit_dev"):
        ok(must_not not in lit, f"提前作答不误亮 {must_not}", str(lit))
    ok("whoami_me" in lit, "无关顺序下 whoami 仍可点亮", str(lit))
    ok("head_passwd" in lit, "head 不依赖前置题", str(lit))
    ok("which_grep" in lit, "which 不依赖前置题", str(lit))

    # 前置补齐后再走一遍参考解答：剩余的题应当全部点亮
    rest = SOLUTION[SOLUTION.index("whoami"):]
    sess2, _ = play(rest, sess)
    missing = [st["key"] for st in STEPS if not sess2.is_done(st["key"])]
    ok(not missing, "先浪费若干命令仍能全部点亮", f"未点亮：{missing}")


def check_history_pair():
    """#29 与 #37 都要 history，只靠有没有管道区分，一条命令不许顺手点亮两题。"""
    sess, _ = play(["whoami", "sudo su", "root", "useradd dev",
                    "passwd dev", "888888", "888888"])
    r = sess.run("history")
    ok(r["passed_keys"] == ["hist_all"], "裸 history 只点亮历史清单题", str(r["passed_keys"]))
    r = sess.run("history | grep passwd")
    ok(r["passed_keys"] == ["hist_passwd"], "管道 history 只点亮过滤题",
       str(r["passed_keys"]))
    s2, _ = play(["history"])
    ok(not s2.is_done("hist_all"), "历史里还没有别的命令时不白送分",
       str([x["key"] for x in s2.records]))


def check_one_command_one_light():
    """一条命令不得顺手点亮多道无关题（除管道组合题本身需要 cat+grep）。"""
    sess = ExamSession("sid-one", "单人")
    r = sess.run("whoami")
    ok(r["passed_keys"] == ["whoami_me"], "whoami 只点亮一题", str(r["passed_keys"]))
    r = sess.run("pwd")
    ok(r["passed_keys"] == ["pwd_home"], "pwd 只点亮一题", str(r["passed_keys"]))
    r = sess.run("sudo su")
    ok(r["awaiting"], "sudo su 先进入等待密码，不提前判题")
    r = sess.run("root")
    ok(r["passed_keys"] == ["sudo_su"], "补完密码后提权题才亮", str(r["passed_keys"]))


def check_serialise():
    """姓名绑定的关键：沙箱状态要能存下来再恢复。"""
    sess, _ = play(["whoami", "pwd", "sudo su", "root", "groupadd devops"])
    data = sess.dump()
    back = ExamSession.restore(data)
    ok(back.sim.user == "root", "恢复后仍是 root 身份")
    ok("devops" in back.sim.groups, "恢复后组还在")
    ok(back.is_done("sudo_su"), "恢复后已点亮的题不丢")
    r = back.run("useradd dev")
    ok(r["passed_keys"] == ["useradd_dev"], "恢复后能继续判题", str(r["passed_keys"]))


def main():
    check_structure()
    check_solution()
    check_free_order()
    check_history_pair()
    check_one_command_one_light()
    check_serialise()
    print(f"通过 {PASS} 项断言")
    if FAIL:
        print(f"失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
