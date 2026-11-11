# -*- coding: utf-8 -*-
"""api_smoke —— HTTP interface regression for the Linux exam system.

Boots the student (8010) and teacher (8011) servers in-process against a
throwaway SQLite database / config / question store, so no QEMU virtual
machine is ever started and the real data/ directory is left untouched.

Run:  python tests/api_smoke.py
Console output is ASCII only (Git Bash mangles CJK on stdout).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

STUDENT_PORT = int(os.environ.get("SMOKE_STUDENT_PORT", "8010"))
TEACHER_PORT = int(os.environ.get("SMOKE_TEACHER_PORT", "8011"))

TMP = tempfile.mkdtemp(prefix="exam-smoke-")

# lab_pool.LabPool() starts its _prepare thread at import time, and _prepare
# purges unassigned exam slot images from the real data/lab directory. Patch it
# out *before* lab_pool is imported so the smoke test never deletes real files.
import lab_qemu  # noqa: E402

REAL_PURGE_SLOTS = lab_qemu.purge_unassigned_slot_images
REAL_ORPHAN_IMAGES = lab_qemu.orphan_session_images
lab_qemu.purge_unassigned_slot_images = lambda: 0
lab_qemu.provision_golden = lambda: None

import server  # noqa: E402
import exam_flow  # noqa: E402

# run_server() reports orphan images from the real data/lab directory; keep the
# smoke test off the real disk entirely.
server.orphan_session_images = lambda known: []

server.DB_PATH = os.path.join(TMP, "exam.db")
server.CONFIG_PATH = os.path.join(TMP, "config.json")
exam_flow.QUESTIONS_PATH = os.path.join(TMP, "questions.json")
server.CONFIG = server.load_config()

# Keep the smoke test hermetic: never touch the real QEMU pool. Setting the
# exam gate would otherwise prewarm 14 virtual machines and copy 14 x 200MB
# disk images into data/lab/images/sessions/.
from lab_pool import POOL  # noqa: E402

POOL.prewarm_exam = lambda count=None: None
POOL.stop_exam_prewarm = lambda: None
POOL.release = lambda key, reset=False: None
POOL.prewarm = lambda: None

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("[PASS] " if ok else "[FAIL] ") + name.ljust(52) + (" | " + str(detail) if detail else ""))
    return bool(ok)


def req(port, path, body=None, method=None, raw=False):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers,
                               method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            payload = resp.read()
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        code = exc.code
    if raw:
        return code, payload
    try:
        return code, json.loads(payload.decode("utf-8"))
    except Exception:
        return code, {"_raw": payload.decode("utf-8", "replace")}


def S(path, body=None, raw=False):
    return req(STUDENT_PORT, path, body, raw=raw)


def T(path, body=None, raw=False):
    return req(TEACHER_PORT, path, body, raw=raw)


def open_gate(minutes=60, offset_seconds=0):
    start = time.time() + offset_seconds
    stamp = time.strftime("%Y-%m-%dT%H:%M", time.localtime(start))
    server.CONFIG["exam_start"] = stamp
    server.CONFIG["exam_minutes"] = minutes
    server.CONFIG["exam_closed"] = False
    server.save_config(server.CONFIG)
    return server.gate_state()


def new_student(name="smoke"):
    code, body = S("/api/exam/start", {"name": name})
    assert code == 200, (code, body)
    return body["sid"]


# ---------------------------------------------------------------------------
def test_isolation():
    code, _ = S("/api/admin/live")
    check("T01 student port rejects /api/admin/*", code == 404, "got %s" % code)
    code, _ = T("/api/exam/gate")
    check("T02 teacher port rejects /api/exam/*", code == 404, "got %s" % code)
    code, _ = S("/vendor/../../app/server.py")
    check("T03 vendor path traversal blocked", code == 404, "got %s" % code)
    code, _ = S("/api/result?sid=doesnotexist")
    check("T04 unknown result -> 404", code == 404, "got %s" % code)


def test_gate():
    server.CONFIG["exam_start"] = ""
    server.CONFIG["exam_closed"] = False
    server.save_config(server.CONFIG)
    code, gate = S("/api/exam/gate")
    check("T05 no start time -> gate open", code == 200 and gate["status"] == "open", gate.get("status"))
    sid = new_student("no-deadline")
    _, state = S("/api/exam/state?sid=" + sid)
    check("T06 no deadline -> personal 7200s", abs(state["remaining"] - 7200) <= 5, state["remaining"])

    server.CONFIG["exam_closed"] = True
    server.save_config(server.CONFIG)
    code, body = S("/api/exam/start", {"name": "blocked"})
    check("T07 closed gate blocks start", code == 403, code)

    open_gate(minutes=60, offset_seconds=3600)
    code, body = S("/api/exam/start", {"name": "too-early"})
    check("T08 >5min before start blocks entry", code == 403, code)
    open_gate(minutes=60, offset_seconds=120)
    code, body = S("/api/exam/start", {"name": "early-ok"})
    check("T09 within 5min allows early entry", code == 200, code)
    _, state = S("/api/exam/state?sid=" + body["sid"])
    # exam_start is stored with minute precision, so allow up to 60s of slack
    check("T10 early entry deadline = gate end",
          3660 <= state["remaining"] <= 3720, state["remaining"])

    code, body = S("/api/exam/start", {"name": "   "})
    check("T11 blank name rejected", code == 400, code)


def test_answer_leak():
    open_gate(minutes=60)
    sid = new_student("leak")
    _, state = S("/api/exam/state?sid=" + sid)
    leaked = [t["title"] for t in state["tasks"] if t.get("expect")]
    check("T12 student payload carries no answer commands", not leaked,
          "%d/%d tasks leak expect" % (len(leaked), len(state["tasks"])))
    check("T13 student payload has no selected answer",
          not (state.get("selected") or {}).get("expect"),
          json.dumps(state.get("selected"), ensure_ascii=False)[:80])

    _, live = T("/api/admin/live")
    row = next((r for r in live if r["sid"] == sid), None)
    check("T14 teacher live still exposes expect", bool(row) and all(t.get("expect") for t in row["tasks"]),
          "row=%s" % (bool(row),))

    last_key = exam_flow.STEPS[-1]["key"]
    code, _ = S("/api/exam/select", {"sid": sid, "key": last_key})
    check("T67 manual question selection endpoint is gone", code == 404, code)
    _, state = S("/api/exam/state?sid=" + sid)
    check("T68 current question cannot be jumped over",
          (state.get("selected") or {}).get("key") == exam_flow.STEPS[0]["key"],
          (state.get("selected") or {}).get("key"))


def test_force_pass():
    open_gate(minutes=60)
    sid = new_student("forcepass")
    keys = [q["key"] for q in exam_flow.all_questions()]
    for k in keys:
        code, body = T("/api/admin/force_pass", {"sid": sid, "key": k})
        if code != 200 or not body.get("ok"):
            check("T15 force_pass every question", False, "%s -> %s %s" % (k, code, body))
            return
    check("T15 force_pass every question", True, "%d keys" % len(keys))
    _, state = S("/api/exam/state?sid=" + sid)
    check("T16 session finished after last force_pass", state["finished"] is True, state["finished"])
    check("T17 score is 100 after all passed", abs(state["score"] - 100.0) < 0.01, state["score"])
    _, subs = T("/api/admin/submissions")
    row = next((r for r in subs if r["sid"] == sid), None)
    check("T18 force-passed finish writes a submission row", row is not None,
          "rows=%d" % len(subs))
    if row:
        check("T19 submission row scored 100 / 41 passed",
              abs(row["score"] - 100.0) < 0.01 and row["passed"] == len(keys),
              "score=%s passed=%s" % (row["score"], row["passed"]))
    code, body = T("/api/admin/force_pass", {"sid": sid, "key": keys[0]})
    check("T20 force_pass on finished session reports error",
          code == 200 and body.get("ok") is False, body)
    code, body = T("/api/admin/force_pass", {"sid": "", "key": ""})
    check("T21 force_pass validates arguments", code == 400, code)


def test_partial_force_pass_progress():
    open_gate(minutes=60)
    sid = new_student("partial")
    keys = [q["key"] for q in exam_flow.all_questions()]
    T("/api/admin/force_pass", {"sid": sid, "key": keys[0]})
    T("/api/admin/force_pass", {"sid": sid, "key": keys[1]})
    _, state = S("/api/exam/state?sid=" + sid)
    check("T22 partial force_pass advances selected", state["selected"]["key"] == keys[2],
          state["selected"])
    check("T23 partial force_pass not finished", state["finished"] is False, state["finished"])
    # persistence: a fresh process view must see the same progress
    conn = server.db()
    row = conn.execute("SELECT payload FROM sessions WHERE sid=?", (sid,)).fetchone()
    conn.close()
    saved = json.loads(row["payload"])
    check("T24 force_pass progress persisted to db", len(saved["records"]) == 2, len(saved["records"]))


def test_penalty():
    open_gate(minutes=60)
    sid = new_student("penalty")
    keys = [q["key"] for q in exam_flow.all_questions()]
    T("/api/admin/force_pass", {"sid": sid, "key": keys[0]})
    pts0 = exam_flow.STEPS[0].get("points", 2)
    body = None
    for _ in range(3):
        code, body = S("/api/exam/fullscreen", {"sid": sid})
    check("T25 fullscreen exit deducts 1 point each",
          code == 200 and body["penalty"] == 3 and abs(body["score"] - max(0.0, pts0 - 3)) < 0.01,
          body)
    _, live = T("/api/admin/live")
    row = next((r for r in live if r["sid"] == sid), None)
    check("T26 teacher live reports penalty", row and row["penalty"] == 3, row and row["penalty"])
    _, prog = T("/api/admin/progress")
    prow = next((r for r in prog if r["sid"] == sid), None)
    check("T27 progress chart score matches penalised score",
          prow and abs(prow["score"] - body["score"]) < 0.01, prow and prow["score"])
    for _ in range(5):
        _, body = S("/api/exam/fullscreen", {"sid": sid})
    check("T27b score floors at zero", body["score"] == 0.0 and body["penalty"] == 8, body)


def test_finish_and_export():
    open_gate(minutes=60)
    sid = new_student("finisher")
    code, summary = S("/api/exam/finish", {"sid": sid})
    check("T28 manual finish returns summary", code == 200 and summary["sid"] == sid, code)
    _, subs = T("/api/admin/submissions")
    row = next((r for r in subs if r["sid"] == sid), None)
    check("T29 finish writes submission", row is not None, len(subs))
    code, body = S("/api/result?sid=" + sid)
    check("T30 result readable after finish", code == 200 and body["score"] == 0, body.get("score"))

    code, raw = T("/api/admin/export.csv", raw=True)
    text = raw.decode("utf-8", "replace")
    check("T31 export.csv has BOM and header", code == 200 and text.startswith("\ufeff") and "姓名" in text, code)
    code, raw = T("/api/admin/export_matrix.csv", raw=True)
    text = raw.decode("utf-8", "replace")
    header = text.lstrip("\ufeff").splitlines()[0].split(",")
    expect_cols = 2 + len(exam_flow.STEPS) + 1
    check("T32 matrix csv column count = 2 + steps + 1",
          len(header) == expect_cols, "%d vs %d" % (len(header), expect_cols))


def test_janitor():
    open_gate(minutes=60)
    sid = new_student("overtime")
    with server._lock:
        server._sessions[sid].deadline = time.time() - 1
    deadline_hit = time.time() + 12
    row = None
    while time.time() < deadline_hit:
        _, subs = T("/api/admin/submissions")
        row = next((r for r in subs if r["sid"] == sid), None)
        if row:
            break
        time.sleep(0.5)
    check("T33 janitor auto-submits an expired session", row is not None, row)


def test_questions_admin():
    code, questions = T("/api/admin/questions")
    check("T34 teacher can list questions", code == 200 and len(questions) == len(exam_flow.STEPS), len(questions))
    edited = [dict(q) for q in questions]
    edited[0]["title"] = "smoke-title"
    edited[0]["points"] = "9"
    edited[1]["points"] = "0"          # must clamp to 1
    edited[2]["points"] = "abc"         # must fall back to default
    code, body = T("/api/admin/questions", {"action": "save", "questions": edited})
    check("T35 save questions ok", code == 200 and body.get("ok"), body)
    _, after = T("/api/admin/questions")
    check("T36 edited title persisted", after[0]["title"] == "smoke-title", after[0]["title"])
    check("T37 points string coerced to int", after[0]["points"] == 9, after[0]["points"])
    check("T38 points 0 clamped to 1", after[1]["points"] == 1, after[1]["points"])
    check("T39 points 'abc' falls back to default",
          after[2]["points"] == questions[2]["points"], after[2]["points"])
    total = sum(q["points"] for q in after)
    check("T40 total points still reported", total == 100 - 2 + 9 + (1 - questions[1]["points"]), total)
    code, body = T("/api/admin/questions", {"action": "reset"})
    _, reset = T("/api/admin/questions")
    check("T41 reset restores defaults",
          reset[0]["title"] == questions[0]["title"] and sum(q["points"] for q in reset) == 100,
          sum(q["points"] for q in reset))
    code, body = T("/api/admin/questions", {"action": "save"})
    check("T42 save without question list rejected", code == 400, code)


def test_config_admin():
    code, body = T("/api/admin/config", {"action": "save", "start": "23:59", "minutes": 0})
    check("T43 minutes must be >= 1", code == 400, code)
    code, body = T("/api/admin/config", {"action": "save", "start": "23:59", "minutes": 601})
    check("T44 minutes must be <= 600", code == 400, code)
    code, body = T("/api/admin/config", {"action": "save", "start": "not-a-time", "minutes": 60})
    check("T45 bad start rejected", code == 400, code)
    code, body = T("/api/admin/config", {"action": "save", "start": "23:59", "minutes": 60})
    check("T46 HH:MM start accepted", code == 200 and body["gate"]["status"] == "before",
          body.get("gate", {}).get("status"))
    code, body = T("/api/admin/config", {"action": "bogus"})
    check("T47 unknown action rejected", code == 400, code)
    code, body = T("/api/admin/config", {"action": "now", "minutes": 60})
    check("T48 start now -> gate open", code == 200 and body["gate"]["status"] == "open",
          body.get("gate", {}).get("status"))


def test_resume():
    open_gate(minutes=60)
    sid = new_student("resume-me")
    T("/api/admin/force_pass", {"sid": sid, "key": exam_flow.STEPS[0]["key"]})
    code, body = S("/api/exam/resume", {"sid": sid, "name": "resume-me"})
    check("T49 refresh can resume the same session",
          code == 200 and body.get("sid") == sid, "%s %s" % (code, body.get("sid")))
    if code == 200:
        check("T50 resumed session keeps progress",
              body["progress"]["done"] == 1, body["progress"])
    code, body = S("/api/exam/resume", {"sid": "0" * 16, "name": "nobody-here"})
    check("T51 resume never creates a session", code == 404, code)
    code, _ = S("/api/exam/resume", {})
    check("T51b resume requires sid or name", code == 400, code)


def test_name_binding():
    """姓名是虚拟机的绑定键：同名必须回到同一 sid（= 同一台虚拟机）。"""
    open_gate(minutes=60)
    before = len(server._sessions)
    sid = new_student("绑定测试")
    code, body = S("/api/exam/start", {"name": "绑定测试"})
    check("T71 same name re-enters the same session",
          code == 200 and body.get("sid") == sid, "%s %s" % (code, body.get("sid")))
    check("T72 same name does not allocate a second machine",
          len(server._sessions) == before + 1, len(server._sessions) - before)
    T("/api/admin/force_pass", {"sid": sid, "key": exam_flow.STEPS[0]["key"]})
    # 关标签页后 sessionStorage 里的 sid 没了，只剩 localStorage 里的姓名
    code, body = S("/api/exam/resume", {"name": "绑定测试"})
    check("T73 name alone recovers the machine after the tab was closed",
          code == 200 and body.get("sid") == sid, "%s %s" % (code, body.get("sid")))
    check("T74 recovered session keeps its progress",
          code == 200 and body["progress"]["done"] == 1, body.get("progress"))
    code, body = S("/api/exam/resume", {"sid": "0" * 16, "name": "绑定测试"})
    check("T75 stale sid still falls back to the name binding",
          code == 200 and body.get("sid") == sid, "%s %s" % (code, body.get("sid")))
    code, body = S("/api/exam/start", {"name": "  绑定测试  "})
    check("T76 surrounding whitespace hits the same machine",
          code == 200 and body.get("sid") == sid, body.get("sid"))
    other = new_student("另一个学生")
    check("T77 a different name gets its own machine", other != sid, other)
    # 交卷后同名不再复用旧会话，否则学生永远看不到第二场考试
    S("/api/exam/finish", {"sid": sid})
    code, body = S("/api/exam/start", {"name": "绑定测试"})
    check("T78 after submitting the same name starts a fresh exam",
          code == 200 and body.get("sid") != sid, "%s %s" % (code, body.get("sid")))
    code, resumed = S("/api/exam/resume", {"name": "绑定测试"})
    check("T79 resume binds to the new live session, not the submitted one",
          code == 200 and resumed.get("sid") == body.get("sid"), code)


def test_name_binding_concurrent():
    """两个同名请求同时到达时只能建一台虚拟机（查找与新建必须同锁）。"""
    import threading

    open_gate(minutes=60)
    before = len(server._sessions)
    sids, codes = [], []
    lock = threading.Lock()

    def enter():
        code, body = S("/api/exam/start", {"name": "并发同名"})
        with lock:
            codes.append(code)
            sids.append(body.get("sid"))

    threads = [threading.Thread(target=enter) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    check("T80 concurrent same-name entries all succeed",
          len(codes) == 6 and all(c == 200 for c in codes), codes)
    check("T81 concurrent same-name entries share one machine",
          len(set(sids)) == 1 and len(server._sessions) == before + 1,
          "%d distinct sids, +%d sessions" % (len(set(sids)), len(server._sessions) - before))


def test_session_persistence_size():
    open_gate(minutes=60)
    sid = new_student("bulky")
    session = server._sessions[sid]
    # 走真实判题入口：直接改 session.logs 会绕过 submit_line 的裁剪逻辑
    for i in range(250):
        session.submit_line("echo %d" % i, "x" * 3000)
    server.save_session(session)
    conn = server.db()
    row = conn.execute("SELECT length(payload) AS n FROM sessions WHERE sid=?", (sid,)).fetchone()
    conn.close()
    check("T52 persisted session payload stays bounded", row["n"] < 200000, "%d bytes" % row["n"])
    _, state = S("/api/exam/state?sid=" + sid)
    blob = len(json.dumps(state, ensure_ascii=False))
    check("T53 student state payload stays bounded", blob < 60000, "%d bytes" % blob)
    # 每条命令都会 save_session 一次，写入耗时必须与历史命令数无关地保持很小
    t0 = time.time()
    for _ in range(20):
        server.save_session(session)
    per_save = (time.time() - t0) / 20 * 1000
    check("T66 per-command session save stays fast", per_save < 25, "%.1f ms" % per_save)


def test_admin_polling_cost():
    open_gate(minutes=60)
    sids = [new_student("poll%d" % i) for i in range(8)]
    for sid in sids:
        session = server._sessions[sid]
        for i in range(120):
            session.logs.append({"in": "echo %d" % i, "out": "y" * 1500})
        server.save_session(session)
    conn = server.db()
    conn.execute("DELETE FROM sessions WHERE sid NOT IN (%s)"
                 % ",".join("?" * len(sids)), sids)
    conn.commit()
    conn.close()
    t0 = time.time()
    for _ in range(5):
        T("/api/admin/live")
        T("/api/admin/progress")
        T("/api/admin/logs")
    cost = (time.time() - t0) / 5
    check("T54 teacher 3s poll cycle < 250ms for 8 students", cost < 0.25, "%.0f ms" % (cost * 1000))


def test_delete_and_clear():
    open_gate(minutes=60)
    sid = new_student("deleteme")
    S("/api/exam/finish", {"sid": sid})
    code, body = T("/api/admin/record/delete", {"sid": sid})
    _, subs = T("/api/admin/submissions")
    check("T55 delete record removes submission",
          code == 200 and not any(r["sid"] == sid for r in subs), code)
    code, body = T("/api/admin/record/delete", {"sid": ""})
    check("T56 delete requires sid", code == 400, code)
    sid2 = new_student("clearme")
    code, body = T("/api/admin/records/clear", {})
    _, subs = T("/api/admin/submissions")
    _, live = T("/api/admin/live")
    check("T57 clear removes everything",
          code == 200 and not subs and not live, "%s %s" % (len(subs), len(live)))


def test_lab_pool_release_without_vm():
    import threading
    import lab_pool

    deleted = []
    orig = lab_pool.delete_session_image
    lab_pool.delete_session_image = lambda sid: deleted.append(sid)
    try:
        # __new__ 跳过 __init__：真实构造函数会拉起 _prepare/_janitor 线程
        pool = lab_pool.LabPool.__new__(lab_pool.LabPool)
        pool.cond = threading.Condition(threading.Lock())
        pool.vms = {}
        lab_pool.LabPool.release(pool, "exam:deadbeefdeadbeef", reset=True)
        check("T58 exam release reclaims disk with no live VM",
              deleted == ["deadbeefdeadbeef"], deleted)
        del deleted[:]
        lab_pool.LabPool.release(pool, "exam:deadbeefdeadbeef", reset=False)
        check("T59 exam release without reset keeps the disk", deleted == [], deleted)
    finally:
        lab_pool.delete_session_image = orig


def test_purge_slot_images():
    import lab_qemu

    tmp = tempfile.mkdtemp(prefix="slot-purge-")
    orig_dir, orig_map = lab_qemu.SESSION_IMG_DIR, lab_qemu.SLOT_MAP_PATH
    try:
        lab_qemu.SESSION_IMG_DIR = tmp
        lab_qemu.SLOT_MAP_PATH = os.path.join(tmp, "exam_slots.json")
        with open(lab_qemu.SLOT_MAP_PATH, "w", encoding="utf-8") as fh:
            json.dump({"live-session-sid": 3}, fh)
        for name in ("pw-01.qcow2", "pw-03.qcow2", "pw-07.qcow2",
                     "live-session-sid.qcow2", "golden.qcow2"):
            open(os.path.join(tmp, name), "w", encoding="utf-8").close()
        removed = REAL_PURGE_SLOTS()
        left = sorted(n for n in os.listdir(tmp) if n != "exam_slots.json")
        check("T60 purge deletes unassigned prewarm images", removed == 2, removed)
        check("T61 purge keeps assigned slot and session images",
              left == ["golden.qcow2", "live-session-sid.qcow2", "pw-03.qcow2"], left)
    finally:
        lab_qemu.SESSION_IMG_DIR = orig_dir
        lab_qemu.SLOT_MAP_PATH = orig_map
        shutil.rmtree(tmp, ignore_errors=True)


def test_orphan_session_images():
    import lab_qemu

    tmp = tempfile.mkdtemp(prefix="orphan-img-")
    orig_dir = lab_qemu.SESSION_IMG_DIR
    try:
        lab_qemu.SESSION_IMG_DIR = tmp
        for name in ("aaaa1111aaaa1111.qcow2", "bbbb2222bbbb2222.qcow2",
                     "pw-01.qcow2", "golden.qcow2"):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write("x" * 1024)
        orphans = REAL_ORPHAN_IMAGES({"aaaa1111aaaa1111"})
        check("T69 only session images without a db row are reported",
              [sid for sid, _ in orphans] == ["bbbb2222bbbb2222"], orphans)
        check("T70 orphan report never deletes files",
              sorted(os.listdir(tmp)) == ["aaaa1111aaaa1111.qcow2",
                                          "bbbb2222bbbb2222.qcow2",
                                          "golden.qcow2", "pw-01.qcow2"],
              sorted(os.listdir(tmp)))
    finally:
        lab_qemu.SESSION_IMG_DIR = orig_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_command_mirror():
    import threading
    from term_bridge import TermBridge

    class _Stdin:
        def write(self, data):
            pass

        def flush(self):
            pass

    class _Proc:
        stdin = _Stdin()

        def poll(self):
            return None

    def mirror(payload):
        s = TermBridge.__new__(TermBridge)
        s.proc = _Proc()
        s._lock = threading.RLock()
        s.in_buf = bytearray()
        s.out_buf = bytearray()
        s.pending = None
        s.secret_mode = False
        s._esc = False
        s.last_out = 0.0
        s.stopped = threading.Event()
        TermBridge._feed(s, payload)
        return s.pending[0] if s.pending else None

    check("T62 plain command mirrored", mirror(b"ls -l\r") == "ls -l", mirror(b"ls -l\r"))
    up = mirror(b"\x1b[A\r")
    check("T63 up-arrow recall yields no garbage command", up is None, repr(up))
    check("T64 backspace edits the mirrored line", mirror(b"abc\x7f\r") == "ab",
          mirror(b"abc\x7f\r"))
    check("T65 ctrl-L does not pollute the mirrored line", mirror(b"pwd\x0c\r") == "pwd",
          repr(mirror(b"pwd\x0c\r")))


def main():
    server.init_db()
    server.restore_sessions()
    server.start_exam_janitor()
    student = server.run_server(host="127.0.0.1", port=STUDENT_PORT)
    teacher = server.run_teacher_server(port=TEACHER_PORT)
    time.sleep(0.4)
    print("student http://127.0.0.1:%d  teacher http://127.0.0.1:%d" % (STUDENT_PORT, TEACHER_PORT))
    print("tmpdir " + TMP)
    print("-" * 100)
    tests = [
        test_isolation, test_gate, test_answer_leak, test_force_pass,
        test_partial_force_pass_progress, test_penalty, test_finish_and_export,
        test_janitor, test_questions_admin, test_config_admin, test_resume,
        test_session_persistence_size, test_admin_polling_cost,
        test_delete_and_clear,
        test_name_binding, test_name_binding_concurrent,
        test_lab_pool_release_without_vm, test_purge_slot_images,
        test_orphan_session_images, test_command_mirror,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " (crashed)", False, repr(exc))
    print("-" * 100)
    failed = [r for r in RESULTS if not r[1]]
    print("total %d  passed %d  failed %d" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    for name, _, detail in failed:
        print("  FAILED " + name + " | " + str(detail))
    try:
        student.shutdown()
        teacher.shutdown()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
