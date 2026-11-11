# -*- coding: utf-8 -*-
"""e2e_http —— 简化版端到端实测：真发 HTTP，覆盖学生作答、姓名找回、教师端全接口。

    python tests/e2e_http.py

用与浏览器完全相同的接口调用（POST /api/exam/*、GET /api/admin/*）跑一遍，
验证的正是 index.html 与 teacher.html 依赖的那份契约：
1. 登录即绑定：填姓名 → 拿到 sid、题单 37 条、prompt、gate。
2. 乱序作答：先做后面的题不误亮，随意顺序逐题点亮，参考答案走完满分交卷。
3. 断线恢复：只凭姓名（不带 sid）resume 回到同一个 sid、同一份沙箱状态。
4. 进程重启恢复：内存会话清空后仍能从 SQLite 还原身份与已点亮的题。
5. 教师端：config / questions / submissions / stats / live / logs / progress /
   两个 CSV 导出 / force_pass 逐字段可用。
6. 交卷与扣分：finish 落库、退出全屏罚分进入成绩。
7. 闸门：教师还没设定开考时间（not_set）时学生进不来也答不了；进行中被撤销排期
   要立刻停止判分，重新排期后恢复。
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))
sys.path.insert(0, os.path.join(BASE, "tests"))

import server  # noqa: E402
from selfcheck import SOLUTION  # noqa: E402

STUDENT_PORT = int(os.environ.get("E2E_STUDENT_PORT", "8199"))
TEACHER_PORT = int(os.environ.get("TEACHER_PORT", "8101"))
PASS = 0
FAIL = []

STATE_KEYS = ("sid", "name", "prompt", "badge", "tasks", "progress", "score",
              "remaining", "finished", "total", "history", "gate")
RESULT_KEYS = ("output", "prompt", "badge", "awaiting", "cleared", "passed",
               "passed_keys", "passed_titles", "lit_points", "tasks", "progress",
               "score", "remaining", "elapsed", "finished", "total")


def ok(cond, label, extra=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(f"{label} {extra}")


def call(port, path, body=None, raw=False):
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            payload = res.read()
            if raw:
                return res.status, payload.decode("utf-8")
            return res.status, json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload.decode("utf-8"))
        except Exception:
            return exc.code, {"error": payload.decode("utf-8", "replace")}


def s(path, body=None, raw=False):
    return call(STUDENT_PORT, path, body, raw)


def t(path, body=None, raw=False):
    return call(TEACHER_PORT, path, body, raw)


def run(sid, line):
    code, r = s("/api/exam/run", {"sid": sid, "line": line})
    ok(code == 200, f"run {line!r} 返回 200", f"实际 {code} {r}")
    if code == 200:
        ok(all(k in r for k in RESULT_KEYS), f"run 结果字段齐全（{line!r}）",
           str([k for k in RESULT_KEYS if k not in r]))
    return r


def lit_of(sid, lines):
    """喂一串输入，返回本次新点亮的 key。"""
    keys = []
    for line in lines:
        r = run(sid, line)
        keys.extend(r.get("passed_keys") or [])
    return keys


# ------------------------------------------------------------------ 0 闸门未排期
def check_gate_locked():
    """教师端还没设定开考时间 = 这场考试还没安排，学生必须整个锁在门外。

    这是真实事故：控制台刚打开、时间还没填，学生已经能从公网进考场把题做完。
    """
    code, h = s("/api/health")
    ok(code == 200 and h["gate"]["status"] == "not_set", "未排期时闸门是 not_set",
       str(h.get("gate")))
    code, st = s("/api/exam/start", {"name": "试探甲"})
    ok(code == 403, "未排期禁止进入考试", f"{code} {st}")
    ok(st.get("gate", {}).get("status") == "not_set", "拒绝时带回闸门状态", str(st))
    ok(bool(st.get("error")), "拒绝时要说明原因", str(st))
    code, st = s("/api/exam/resume", {"sid": "", "name": "试探甲"})
    ok(code == 404, "未排期不会留下可恢复的会话", str(code))
    code, subs = t("/api/admin/submissions")
    ok(not any(x["name"] == "试探甲" for x in subs), "被挡在门外不产生成绩记录")


def open_gate():
    code, cfg = t("/api/admin/config", {"action": "now", "minutes": 120})
    ok(code == 200 and cfg["gate"]["status"] == "open", "教师点开始之后闸门开放", str(cfg))
    return cfg


def check_gate_locked_mid_exam():
    """已经在线的会话：老师把开考时间清空，作答通道要立刻关掉（看题不拦，判分拦）。"""
    code, st = s("/api/exam/start", {"name": "试探乙"})
    ok(code == 200, "排期后正常进入", str(code))
    keep = server.CONFIG.get("exam_start", "")
    server.CONFIG["exam_start"] = ""
    server.save_config(server.CONFIG)
    try:
        code, err = s("/api/exam/run", {"sid": st["sid"], "line": "whoami"})
        ok(code == 403, "时间被撤销后禁止作答", f"{code} {err}")
        ok(err.get("gate", {}).get("status") == "not_set", "作答拒绝带回 not_set", str(err))
        code, st2 = s("/api/exam/state?sid=" + st["sid"])
        ok(code == 200 and st2["gate"]["status"] == "not_set", "状态轮询同步读到 not_set")
        ok(not any(t_["done"] for t_ in st2["tasks"]), "被拦下的这一行没有点亮任何题")
    finally:
        server.CONFIG["exam_start"] = keep
        server.save_config(server.CONFIG)
    code, r = s("/api/exam/run", {"sid": st["sid"], "line": "whoami"})
    ok(code == 200, "重新排期后恢复作答", str(code))


# ------------------------------------------------------- 0 学生端页面静态契约
def check_student_page():
    """全屏锁定遮罩与整数分数展示必须在学生端页面里真实存在。"""
    code, html = s("/", raw=True)
    ok(code == 200, "学生端首页可访问", str(code))
    ok('id="fsLock"' in html and 'id="btnFsRestore"' in html,
       "退出全屏有锁定遮罩与恢复全屏按钮")
    ok("backdrop-filter:blur" in html, "锁定遮罩带背景模糊")
    ok("scoreText(" in html, "分数走整数格式化")
    ok('Number(r.score).toFixed(1)' not in html, "学生端不再显示一位小数分数")
    code, teacher = t("/", raw=True)
    ok(code == 200 and "scoreText(" in teacher, "教师端分数走整数格式化")
    ok('Number(s.score).toFixed(1)' not in teacher, "教师端不再显示一位小数分数")


# --------------------------------------------------------------------- 1 登录
def check_login():
    code, h = s("/api/health")
    ok(code == 200 and h["ok"], "学生端 /api/health")
    ok(h.get("steps") == 37, "健康检查报 37 题", str(h.get("steps")))
    code, st = s("/api/exam/start", {"name": "张三", "student_no": ""})
    ok(code == 200, "填姓名即可进入", str(code) + " " + str(st))
    ok(all(k in st for k in STATE_KEYS), "state 字段齐全",
       str([k for k in STATE_KEYS if k not in st]))
    ok(len(st["tasks"]) == 37, "题单 37 条", str(len(st["tasks"])))
    ok(not any("expect" in x for x in st["tasks"]), "学生端不下发标准答案")
    ok(st["prompt"].startswith("student@"), "初始身份是 student", st["prompt"])
    ok("sandbox" in st and st["sandbox"] is True, "标记沙箱环境")
    return st["sid"]


# ------------------------------------------------------------------ 2 乱序作答
GATED = {st["key"] for st in server.STEPS if st["after"]}


def check_free_order(sid):
    done = set()
    # 后面的题提前做：凡是有因果前置的题，一盏都不许亮
    noise = ["cat /etc/passwd | grep dev", "groupdel devops", "userdel dev",
             "rm -rf /home/dev/www", "ls -a", "clear"]
    for line in noise:
        r = run(sid, line)
        bad = [k for k in (r["passed_keys"] or []) if k in GATED]
        ok(not bad, f"提前作答 {line!r} 不误亮有前置的题", str(bad))
        done.update(r["passed_keys"] or [])

    # 新表把链接与打包三题砍掉了：这些命令还能跑，但一盏灯都不许点亮
    for loose in ["ln -s /home/dev/www/home.html /home/dev/www/latest.html",
                  "tar -czf /home/dev/www/site.tar.gz /home/dev/www/html",
                  "chown dev:devops /home/student/notes.txt"]:
        r = run(sid, loose)
        ok(not r["passed_keys"], f"{loose!r} 已不在考范围内", str(r["passed_keys"]))

    # 完全独立、不含状态前置的四条：顺序随意，各自只亮自己
    keys = lit_of(sid, ["which grep", "head -n 2 /etc/passwd", "pwd", "whoami"])
    ok(sorted(keys) == sorted(["which_grep", "head_passwd", "pwd_home", "whoami_me"]),
       "无关顺序的四条各自点亮", str(keys))
    done.update(keys)

    # 提权链：sudo su 需要两次输入，中途不许判题
    r = run(sid, "sudo su")
    ok(r["awaiting"] and not r["passed_keys"], "sudo su 等待密码时不判题")
    r = run(sid, "root")
    ok(r["passed_keys"] == ["sudo_su"], "补密码后提权点亮", str(r["passed_keys"]))
    ok(r["prompt"].startswith("root#") or r["prompt"].startswith("root@"),
       "提示符变成 root", r["prompt"])
    done.add("sudo_su")

    # 因果链按题面要求补齐后再跳序：建组、建号、切身份、铺目录（mkdir -p 用 ~）
    keys = lit_of(sid, ["groupadd devops", "useradd dev", "passwd dev", "888888", "888888",
                        "gpasswd -a dev devops", "id dev", "grep devops /etc/group",
                        "su dev", "mkdir -p ~/www/html/static",
                        "cd /home/dev/www/html/static"])
    want = ["groupadd_devops", "useradd_dev", "passwd_dev", "gpasswd_dev", "id_dev",
            "grep_group", "su_dev", "mkdir_p", "cd_static"]
    ok(sorted(keys) == sorted(want), "建号建目录各按自己的判据点亮", str(keys))
    done.update(keys)
    r = run(sid, "touch page.html")
    ok(r["passed_keys"] == ["touch_page"], "touch 点亮空页面题", str(r["passed_keys"]))
    done.add("touch_page")

    # vim 多行：进插入、正文、Esc、:wq，只有 :wq 那次才亮
    r = run(sid, "vim page.html")
    ok(r["awaiting"], "vim 打开后等待输入")
    r = run(sid, "i")
    ok(r["awaiting"] and "插入模式" in r["badge"], "i 进入插入模式", r["badge"])
    r = run(sid, "hello release")
    ok(r["awaiting"] and not r["passed_keys"], "插入正文不判题")
    r = run(sid, "ESC")
    ok(r["awaiting"], "Esc 只退出插入，仍在 vim")
    r = run(sid, ":wq")
    ok(r["passed_keys"] == ["vim_page"], ":wq 保存后才点亮", str(r["passed_keys"]))
    done.add("vim_page")

    # 剩下的按参考解答续完：已点亮的题重复执行不会再亮一次，最终必须满分
    for line in SOLUTION[SOLUTION.index("mkdir icons"):]:
        r = run(sid, line)
        done.update(r.get("passed_keys") or [])
    ok(len(done) == 37, "乱序 + 续跑累计点亮 37 题", f"实际 {len(done)}")
    st_code, st = s("/api/exam/state?sid=" + sid)
    ok(st_code == 200, "state 可查", str(st_code))
    lit_now = {x["key"] for x in st["tasks"] if x["done"]}
    ok(len(lit_now) == 37, "乱序 + 补完共点亮 37 题", f"实际 {len(lit_now)}")
    ok(abs(st["score"] - 100.0) < 0.05, "乱序走完全分", str(st["score"]))
    ok(isinstance(st["score"], int), "学生端接口分数为整数", repr(st["score"]))
    ok(st["finished"], "全部点亮自动交卷")
    return st


# ------------------------------------------------------------- 3/4 姓名找回
def check_rebind():
    code, st = s("/api/exam/start", {"name": "李四"})
    ok(code == 200, "第二个姓名进入", str(code))
    sid = st["sid"]
    lit_of(sid, ["whoami", "pwd", "sudo su", "root", "groupadd devops", "useradd dev"])
    code, st2 = s("/api/exam/state?sid=" + sid)
    before = {k: v for k, v in st2.items() if k in ("prompt", "score", "total")}
    done_before = len([x for x in st2["tasks"] if x["done"]])

    # 换个浏览器（sessionStorage 清空）：只凭姓名 resume
    code, rb = s("/api/exam/resume", {"sid": "", "name": "李四"})
    ok(code == 200, "只凭姓名可恢复", str(code) + " " + str(rb))
    ok(rb["sid"] == sid, "恢复到的还是那一台", f"{rb['sid']} != {sid}")
    ok(rb["name"] == "李四", "恢复后姓名一致", rb["name"])
    ok(rb["prompt"] == before["prompt"], "当前身份一致", rb["prompt"])
    ok(len([x for x in rb["tasks"] if x["done"]]) == done_before, "已点亮题数一致")
    r = run(rb["sid"], "id dev")
    ok("dev" in r["output"], "恢复后沙箱里 dev 账号还在", r["output"])
    done_now = len([x for x in r["tasks"] if x["done"]])
    ok(done_now == done_before + 1, "恢复后能继续点亮新题", f"{done_before} -> {done_now}")

    # 姓名首尾空白折叠后仍算同一个人
    code, rb2 = s("/api/exam/resume", {"sid": "", "name": "  李四  "})
    ok(code == 200 and rb2["sid"] == sid, "姓名首尾空白不影响绑定", str(code))
    # 别人的 sid + 自己的姓名：不能接管别人的环境（换机上下一个人坐过来）
    code, hijack = s("/api/exam/resume", {"sid": sid, "name": "路人甲"})
    ok(code == 404 or hijack.get("sid") != sid, "他人的 sid 配自己的姓名不会串号",
       f"{code} {hijack}")
    # 同标签页刷新：sid + 同名照常恢复
    code, self_ok = s("/api/exam/resume", {"sid": sid, "name": "李四"})
    ok(code == 200 and self_ok["sid"] == sid, "同名同 sid 正常恢复", str(code))
    # 名字中间多一个空格就是另一个环境（绑定的代价，README 里要写明）
    code, other = s("/api/exam/resume", {"sid": "", "name": "李 四"})
    ok(code == 404, "姓名写错按没考过处理", str(code))

    # 进程重启：内存会话清空，从 SQLite 还原
    server._sessions.clear()
    server.restore_sessions()
    code, rb3 = s("/api/exam/resume", {"sid": "", "name": "李四"})
    ok(code == 200 and rb3["sid"] == sid, "进程重启后凭姓名回到同一台", str(code))
    ok(len([x for x in rb3["tasks"] if x["done"]]) == done_now, "重启后进度不丢")
    ok(rb3["prompt"] == before["prompt"], "重启后身份不丢", rb3["prompt"])
    r = run(sid, "passwd dev")
    ok(r["awaiting"], "重启后交互命令依然可用")
    run(sid, "888888")
    run(sid, "888888")
    code, st3 = s("/api/exam/state?sid=" + sid)
    ok(len([x for x in st3["tasks"] if x["done"]]) > done_now, "重启后继续判题",
       f"{done_now} -> {len([x for x in st3['tasks'] if x['done']])}")
    return sid


# --------------------------------------------------------------------- 5 教师端
def check_teacher(sid):
    code, cfg = t("/api/admin/config")
    ok(code == 200 and "config" in cfg and "gate" in cfg, "教师 config")
    ok(cfg["gate"]["status"] in ("not_set", "open", "before", "closed"), "gate 状态合法",
       cfg["gate"]["status"])

    code, qs = t("/api/admin/questions")
    ok(code == 200 and len(qs) == 37, "教师考题列表 37 条", str(len(qs)))
    ok(all(k in qs[0] for k in ("key", "no", "act", "title", "task", "expect",
                                "why", "points")), "考题可编辑字段齐全", str(list(qs[0])))

    code, stt = t("/api/admin/stats")
    ok(code == 200, "教师 stats")
    for k in ("count", "finished", "avg", "max", "min", "pass_rate",
              "online", "total_students", "lab"):
        ok(k in stt, f"stats 含 {k}", str(list(stt)))
    ok(isinstance(stt["lab"].get("nodes"), list), "stats.lab.nodes 是数组",
       str(stt["lab"]))
    if stt["lab"]["nodes"]:
        n = stt["lab"]["nodes"][0]
        ok("id" in n and "status" in n, "lab 节点有 id/status（教师页小灯渲染依赖）",
           str(n))
        ok(n["status"] in ("pending", "starting", "idle", "busy"), "节点状态取值合法",
           n["status"])

    code, live = t("/api/admin/live")
    ok(code == 200 and isinstance(live, list), "教师 live 是数组", str(code))
    for item in live:
        for k in ("sid", "name", "student_no", "done", "total", "score", "penalty",
                  "remaining", "finished", "last_active", "tasks"):
            ok(k in item, f"live 项含 {k}", str(list(item)))
        if item["tasks"]:
            tk = item["tasks"][0]
            ok("key" in tk and "no" in tk and "title" in tk and "expect" in tk
               and "done" in tk, "live.tasks 含 expect（教师端看答案）", str(list(tk)))
        break

    code, lg = t("/api/admin/logs")
    ok(code == 200 and isinstance(lg, list) and lg, "教师 logs 非空")
    ok(all("sid" in x and "name" in x and isinstance(x["commands"], list) for x in lg),
       "logs 字段齐全")
    ok(any(x["sid"] == sid for x in lg), "logs 覆盖到作答过的会话")
    secrets = [c for x in lg for c in x["commands"]
               if c.strip() in ("root", "888888", "i", "ESC", ":wq", "q")]
    ok(not secrets, "日志里没有密码与交互续行", str(secrets[:5]))

    code, pg = t("/api/admin/progress")
    ok(code == 200 and isinstance(pg, list) and pg, "教师 progress 非空")
    for k in ("name", "student_no", "score", "series", "done", "total", "run"):
        ok(k in pg[0], f"progress 项含 {k}", str(list(pg[0])))

    code, csv_txt = t("/api/admin/export.csv", raw=True)
    ok(code == 200 and "得分" in csv_txt, "成绩 CSV 可下载")
    code, mx = t("/api/admin/export_matrix.csv", raw=True)
    ok(code == 200 and "姓名" in mx, "成绩矩阵 CSV 可下载")
    ok(mx.count(",") > 40, "矩阵含每题列", str(mx.count(",")))

    # force_pass：教师手动点亮一题
    code, st0 = s("/api/exam/state?sid=" + sid)
    undone = [x["key"] for x in st0["tasks"] if not x["done"]]
    if undone:
        code, fp = t("/api/admin/force_pass", {"sid": sid, "key": undone[0]})
        ok(code == 200 and fp.get("ok"), "force_pass 成功", str(fp))
        code, st1 = s("/api/exam/state?sid=" + sid)
        ok(any(x["done"] and x["key"] == undone[0] for x in st1["tasks"]),
           "force_pass 在学生端题单亮灯")


# ------------------------------------------------------------------ 6 交卷扣分
def check_submit_and_penalty():
    code, st = s("/api/exam/start", {"name": "王五"})
    sid = st["sid"]
    run(sid, "whoami")
    code, st0 = s("/api/exam/state?sid=" + sid)
    ok(code == 200, "罚分前能查到成绩", str(st0))
    base = st0["score"]
    ok(base > 0, "whoami 确实得分", str(base))
    code, pen = s("/api/exam/fullscreen", {"sid": sid})
    ok(code == 200 and pen["penalty"] == 1, "退出全屏记 1 次罚分", str(pen))
    # 分值表逐行给原始分，成绩再按 earned/total 归一到百分制，所以这里只咬住
    # 「扣 1 分」这个差值，不把某一道题的分值写死进测试。
    ok(abs(pen["score"] - (base - 1)) < 0.001, "罚分即时进成绩：正好扣 1 分",
       f"{base} → {pen['score']}")
    code, sm = s("/api/exam/finish", {"sid": sid})
    ok(code == 200 and sm["name"] == "王五", "交卷返回成绩单")
    ok(sm["submitted"] is True and sm["total"] == 37, "成绩单口径")
    ok(sm["penalty"] == 1, "成绩单含罚分")
    ok(len(sm["detail"]) == 37, "成绩单逐题明细")
    ok(abs(sm["score"] - (base - 1)) < 0.001, "交卷后成绩仍带着那 1 分罚分",
       str(sm["score"]))
    code, r = s("/api/exam/run", {"sid": sid, "line": "pwd"})
    ok(code in (404, 409), "交卷后不能再作答", str(code))
    # 交卷后同名不复用旧会话
    code, fresh = s("/api/exam/resume", {"sid": "", "name": "王五"})
    ok(code == 404, "已交卷的姓名不再恢复旧会话", str(code))
    code, subs = t("/api/admin/submissions")
    ok(any(x["name"] == "王五" and x["finished"] for x in subs), "成绩已落库供教师查看")

    # 闸门：设置一个未来开考时间后，学生不能执行命令
    code, cfg = t("/api/admin/config", {"action": "save", "start": "23:59",
                                        "minutes": 120})
    ok(code == 200, "教师保存考试时间", str(cfg))
    ok(cfg["gate"]["status"] in ("before", "open"), "gate 反映新设置")
    code, st = s("/api/exam/start", {"name": "赵六"})
    if cfg["gate"]["status"] == "before" and cfg["gate"]["seconds_to_start"] > server.EARLY_SECONDS:
        # 距开考还远：与完整版一致，挡在门外并告诉学生还要等多久
        ok(code == 403, "距开考较远时禁止进入", f"{code} {st}")
        ok(st.get("gate", {}).get("status") == "before", "错误体带回闸门", str(st))
        ok(st.get("gate", {}).get("seconds_to_start", 0) > 0, "错误体带倒计时")
    else:
        ok(code == 200, "提前 5 分钟内可进入看题", f"{code} {st}")
        ok(st["gate"]["status"] == cfg["gate"]["status"], "学生端读到同一闸门")
        code, out = s("/api/exam/run", {"sid": st["sid"], "line": "ls"})
        if cfg["gate"]["status"] == "before":
            ok(code == 403, "未开考禁止执行命令", str(code))
    # 恢复成开放状态，别把配置留在未来时间
    t("/api/admin/config", {"action": "now", "minutes": 120})
    code, cfg2 = t("/api/admin/config")
    ok(cfg2["gate"]["status"] == "open", "立即开始后闸门开放", cfg2["gate"]["status"])


def main():
    # 测试专用数据目录：不污染真实考试数据，也不会改掉教师设置的时间
    tmp = os.path.join(BASE, "data", "e2e_tmp")
    os.makedirs(tmp, exist_ok=True)
    server.DATA = tmp
    server.DB_PATH = os.path.join(tmp, "exam.db")
    server.CONFIG_PATH = os.path.join(tmp, "config.json")
    for f in (server.DB_PATH, server.CONFIG_PATH):
        if os.path.exists(f):
            os.remove(f)
    server.load_config()
    student = server.run_server(host="127.0.0.1", port=STUDENT_PORT)
    teacher = server.run_teacher_server(port=TEACHER_PORT)
    try:
        check_student_page()
        check_gate_locked()
        open_gate()
        check_gate_locked_mid_exam()
        sid = check_login()
        check_free_order(sid)
        bind_sid = check_rebind()
        check_teacher(bind_sid)
        check_submit_and_penalty()
    finally:
        teacher.shutdown()
        student.shutdown()
    print(f"通过 {PASS} 项断言")
    if FAIL:
        print(f"失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  x", f)
        return 1
    print("端到端全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
