# -*- coding: utf-8 -*-
"""server —— 考试系统简化版后端（Python 标准库，无第三方依赖，无虚拟机）。

两个独立服务：
  学生端  0.0.0.0:8100   —— 只暴露考试页与答题接口，由 Cloudflare 隧道转发
  教师端  127.0.0.1:8101 —— 只在本机可访问，免登录，设置考试时间与查看成绩

与完整版的区别只在「命令在哪里执行」：完整版把终端字节流通过 SSH PTY 转到 QEMU
虚拟机，简化版把每条输入喂给进程内的 Linux 沙箱（sandbox.LinuxSim），因此不需要
QEMU、不需要磁盘镜像、不需要等 40 秒开机。姓名依然是绑定键：姓名 → 会话 sid →
这份沙箱状态，沙箱状态随每次回车整体落盘 SQLite，所以断网、刷新、关标签页、换电
脑，只要填同一个姓名回到的是同一个环境（当前身份、目录、已建的用户与文件、已点亮
的题全部保留）。教师端接口与完整版逐字段对齐，沿用同一个 teacher.html。
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import questions
from questions import STEPS, ExamSession

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "web")
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "exam.db")
CONFIG_PATH = os.path.join(DATA, "config.json")

os.makedirs(DATA, exist_ok=True)

_sessions: dict[str, ExamSession] = {}
_lock = threading.RLock()

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sid TEXT UNIQUE,
  name TEXT,
  student_no TEXT,
  score REAL,
  passed INTEGER,
  total INTEGER,
  elapsed INTEGER,
  penalty INTEGER DEFAULT 0,
  finished INTEGER,
  started_at TEXT,
  finished_at TEXT,
  detail TEXT,
  commands TEXT
);
"""

CONFIG: dict = {}


# --------------------------------------------------------------------- 存储
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(CREATE_SQL)
    conn.commit()
    conn.close()


def load_config():
    default = {
        "exam_title": "Linux 基础命令 · 简化版实操考核",
        "exam_start": "",
        "exam_minutes": 120,
        "exam_closed": False,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                default.update(saved)
        except Exception:
            pass
    CONFIG.clear()
    CONFIG.update(default)
    save_config(CONFIG)


def save_config(config):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def restore_sessions():
    """进程重启后把未交卷会话读回内存，教师端的在线列表不会因重启而清空。"""
    try:
        conn = db()
        rows = conn.execute("SELECT payload FROM sessions").fetchall()
        conn.close()
    except sqlite3.Error:
        return
    restored = 0
    for row in rows:
        try:
            session = ExamSession.restore(json.loads(row["payload"]))
            if not session.finished:
                _sessions[session.sid] = session
                restored += 1
        except Exception:
            continue
    if restored:
        print(f"[exam] 已恢复 {restored} 个进行中的会话")


def save_session(session: ExamSession):
    payload = json.dumps(session.dump(), ensure_ascii=False)
    conn = db()
    conn.execute("INSERT OR REPLACE INTO sessions(sid, payload, updated) VALUES (?,?,?)",
                 (session.sid, payload, time.time()))
    conn.commit()
    conn.close()


def get_session(sid):
    if sid in _sessions:
        return _sessions[sid]
    conn = db()
    row = conn.execute("SELECT payload FROM sessions WHERE sid=?", (sid,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        session = ExamSession.restore(json.loads(row["payload"]))
    except Exception:
        return None
    _sessions[sid] = session
    return session


def save_submission(session: ExamSession):
    summary = session.summary()
    started = datetime.fromtimestamp(session.started).strftime("%Y-%m-%d %H:%M:%S")
    ended = datetime.fromtimestamp(session.finished_at or time.time()).strftime("%Y-%m-%d %H:%M:%S")
    conn = db()
    conn.execute(
        """INSERT OR REPLACE INTO submissions
           (sid,name,student_no,score,passed,total,elapsed,penalty,finished,
            started_at,finished_at,detail,commands)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (session.sid, session.name, session.student_no, summary["score"],
         summary["passed"], summary["total"], summary["elapsed"], session.penalty,
         1 if (summary["submitted"] or summary["finished"]) else 0, started, ended,
         json.dumps(summary["detail"], ensure_ascii=False),
         json.dumps(summary["commands"], ensure_ascii=False)))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------- 姓名绑定
def name_key(name):
    """绑定键：折叠空白并忽略大小写，「张 三」「张三」命中同一个环境。"""
    return " ".join(str(name or "").split()).casefold()


def find_live_session_by_name(name):
    key = name_key(name)
    if not key:
        return None
    for session in _sessions.values():
        if not session.finished and name_key(session.name) == key:
            return session
    return None


def acquire_exam_session(name, student_no=""):
    """姓名 → 会话：有同名未交卷会话就接管，否则新建。调用方必须持有 _lock。

    一个 sid 对应一份沙箱状态，复用 sid 即复用同一个环境；查找与新建在同一把锁
    内完成，否则两个同名请求会各建一份。交卷之后同名不再复用旧会话。
    """
    session = find_live_session_by_name(name)
    if session is not None:
        return session
    sid = os.urandom(8).hex()
    gate = gate_state()
    deadline = gate_deadline() if gate["status"] == "open" else gate_end_timestamp()
    session = ExamSession(sid, name, student_no, deadline=deadline)
    _sessions[sid] = session
    save_session(session)
    return session


# --------------------------------------------------------------------- 闸门
def parse_start(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 教师端只填时分（HH:MM），日期按今天解释，与完整版一致
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            at = datetime.strptime(text, fmt)
        except ValueError:
            continue
        now = datetime.now()
        return now.replace(hour=at.hour, minute=at.minute, second=at.second, microsecond=0)
    return None


def gate_state():
    """考试闸门状态：not_set / before / open / closed。"""
    now = datetime.now()
    if CONFIG.get("exam_closed"):
        return {"status": "closed", "message": "考试已结束，等待教师安排下一场。",
                "start": CONFIG.get("exam_start", ""), "minutes": CONFIG.get("exam_minutes", 120),
                "seconds_to_start": 0, "remaining": 0}
    start = parse_start(CONFIG.get("exam_start", ""))
    if start is None:
        # 没排开考时间就等于没安排这场考试：老师不点「开始」，学生端必须锁在门外。
        # 早先是放行的，结果控制台还没设定时间，学生已经能进考场把题做完了。
        return {"status": "not_set", "message": "教师尚未设置开考时间，考试还没有开放。",
                "start": "", "minutes": CONFIG.get("exam_minutes", 120),
                "seconds_to_start": 0, "remaining": 0}
    minutes = max(1, int(CONFIG.get("exam_minutes", 120)))
    end = start + timedelta(minutes=minutes)
    if now < start:
        return {"status": "before", "message": "考试尚未开始，请等待倒计时结束。",
                "start": start.strftime("%Y-%m-%dT%H:%M"),
                "end": end.strftime("%Y-%m-%dT%H:%M"), "minutes": minutes,
                "seconds_to_start": int((start - now).total_seconds()),
                "remaining": int((end - now).total_seconds())}
    if now < end:
        return {"status": "open", "message": "考试进行中。",
                "start": start.strftime("%Y-%m-%dT%H:%M"),
                "end": end.strftime("%Y-%m-%dT%H:%M"), "minutes": minutes,
                "seconds_to_start": 0, "remaining": int((end - now).total_seconds())}
    return {"status": "closed", "message": "本场考试已到结束时间。",
            "start": start.strftime("%Y-%m-%dT%H:%M"),
            "end": end.strftime("%Y-%m-%dT%H:%M"), "minutes": minutes,
            "seconds_to_start": 0, "remaining": 0}


def gate_deadline():
    gate = gate_state()
    if gate["status"] != "open":
        return None
    start = parse_start(gate["start"])
    if start is None:
        return None
    return (start + timedelta(minutes=gate["minutes"])).timestamp()


def gate_end_timestamp():
    start = parse_start(CONFIG.get("exam_start", ""))
    if start is None:
        return None
    return (start + timedelta(minutes=max(1, int(CONFIG.get("exam_minutes", 120))))).timestamp()


EARLY_SECONDS = 300  # 允许提前 5 分钟进入看题，但不能执行命令


# --------------------------------------------------------------------- 到期看护
def start_exam_janitor():
    global _janitor_started
    if _janitor_started:
        return
    _janitor_started = True
    threading.Thread(target=_exam_janitor, daemon=True).start()


_janitor_started = False


def _exam_janitor():
    while True:
        time.sleep(2)
        due = []
        with _lock:
            for sid, session in list(_sessions.items()):
                if not session.finished and session.remaining() <= 0:
                    session.finish()
                    try:
                        save_submission(session)
                        save_session(session)
                    except sqlite3.Error:
                        pass
                    due.append(sid)
            for sid in due:
                _sessions.pop(sid, None)


def lab_state():
    """教师端「运行状态」里的环境节点：简化版没有虚拟机，节点就是每份沙箱。"""
    with _lock:
        nodes = [{"id": (s.name or s.sid[:6]), "status": "busy"}
                 for s in _sessions.values() if not s.finished]
    nodes.sort(key=lambda n: n["id"])
    return {"nodes": nodes, "total": len(nodes), "mode": "sandbox"}


def score_int(value):
    """把任意来源的分数（含历史库里的小数成绩）统一收敛成整数分。"""
    try:
        return max(0, int(round(float(value or 0))))
    except (TypeError, ValueError):
        return 0


def progress_series(records, total, penalty=0):
    pts = [{"x": 0, "y": 0}]
    if total <= 0:
        return pts
    point_map = {st["key"]: st.get("points", 2) for st in STEPS}
    total_points = sum(point_map.values())
    earned = 0
    for i, r in enumerate(records or []):
        earned += point_map.get(r.get("key"), 2)
        y = earned / total_points * 100 if total_points else 0
        pts.append({"x": i + 1, "y": score_int(y - penalty)})
    return pts


# --------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "LinuxExamLite/1.0"
    role = "student"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------------ 工具
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _send_csv(self, buf, filename):
        body = ("\ufeff" + buf.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _serve_file(self, name, ctype):
        path = os.path.join(WEB, name)
        if not os.path.isfile(path):
            return self._send(404, {"error": "页面不存在"})
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _state_payload(self, session: ExamSession):
        return {
            "sid": session.sid, "name": session.name, "student_no": session.student_no,
            "prompt": session.sim.prompt(), "badge": session.sim.mode_badge(),
            "tasks": session.public_tasks(),
            "progress": session.progress(), "score": session.score(),
            "remaining": session.remaining(), "elapsed": session.elapsed(),
            "finished": session.finished, "total": len(STEPS),
            "history": session.logs[-30:], "gate": gate_state(), "sandbox": True,
        }

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._send(200, {
                "ok": True, "service": "linux-exam-lite", "role": self.role,
                "version": "1.0",
                "local": f"http://localhost:{self.server.server_port}",
                "tunnel": os.environ.get("TUNNEL_URL", ""),
                "steps": len(STEPS),
                "active": len([s for s in _sessions.values() if not s.finished]),
                "gate": gate_state(), "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        if self.role == "teacher":
            return self._teacher_get(path)
        return self._student_get(path)

    def _student_get(self, path):
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path in ("/teacher", "/teacher.html"):
            return self._send(404, {"error": "教师控制台仅在本机 8101 端口开放"})
        if path == "/api/exam/gate":
            return self._send(200, gate_state())
        if path == "/api/lab/status":
            return self._send(200, lab_state())
        if path == "/api/exam/state":
            session = get_session((self._query().get("sid") or [""])[0])
            if not session:
                return self._send(404, {"error": "会话不存在"})
            return self._send(200, self._state_payload(session))
        if path == "/api/result":
            session = get_session((self._query().get("sid") or [""])[0])
            if session:
                return self._send(200, session.summary())
            conn = db()
            row = conn.execute("SELECT * FROM submissions WHERE sid=?",
                               ((self._query().get("sid") or [""])[0],)).fetchone()
            conn.close()
            if not row:
                return self._send(404, {"error": "成绩不存在"})
            return self._send(200, {
                "name": row["name"], "student_no": row["student_no"], "sid": row["sid"],
                "score": score_int(row["score"]), "passed": row["passed"], "total": row["total"],
                "elapsed": row["elapsed"], "finished": bool(row["finished"]),
                "penalty": row["penalty"] or 0,
                "detail": json.loads(row["detail"] or "[]"),
                "commands": json.loads(row["commands"] or "{}"),
            })
        return self._send(404, {"error": "not found"})

    def _teacher_get(self, path):
        if path in ("/", "/index.html", "/teacher", "/teacher.html"):
            return self._serve_file("teacher.html", "text/html; charset=utf-8")
        if path == "/api/admin/config":
            return self._send(200, {"config": CONFIG, "gate": gate_state()})
        if path == "/api/admin/questions":
            return self._send(200, questions.all_questions())
        if path == "/api/admin/submissions":
            conn = db()
            rows = conn.execute("SELECT * FROM submissions ORDER BY id DESC").fetchall()
            conn.close()
            return self._send(200, [{
                "sid": r["sid"], "name": r["name"], "student_no": r["student_no"],
                "score": score_int(r["score"]), "passed": r["passed"], "total": r["total"],
                "elapsed": r["elapsed"], "finished": bool(r["finished"]),
                "started_at": r["started_at"], "finished_at": r["finished_at"],
                "penalty": r["penalty"] or 0,
            } for r in rows])
        if path == "/api/admin/stats":
            conn = db()
            rows = conn.execute("SELECT * FROM submissions").fetchall()
            conn.close()
            scores = [score_int(r["score"]) for r in rows]
            live = [s for s in _sessions.values() if not s.finished]
            return self._send(200, {
                "count": len(rows), "finished": len([r for r in rows if r["finished"]]),
                "avg": int(round(sum(scores) / len(scores))) if scores else 0,
                "max": max(scores) if scores else 0,
                "min": min(scores) if scores else 0,
                "pass_rate": round(len([s for s in scores if s >= 60]) / len(scores) * 100, 1) if scores else 0,
                "online": len(live), "total_students": len(_sessions),
                "lab": lab_state(),
            })
        if path == "/api/admin/live":
            items = []
            with _lock:
                for s in _sessions.values():
                    if s.finished:
                        continue
                    items.append({
                        "sid": s.sid, "name": s.name, "student_no": s.student_no,
                        "done": len(s.records), "total": len(STEPS), "score": s.score(),
                        "remaining": s.remaining(), "finished": s.finished,
                        "last_active": datetime.fromtimestamp(s.last_active).strftime("%H:%M:%S"),
                        "penalty": s.penalty,
                        "tasks": s.public_tasks(include_expect=True),
                    })
            items.sort(key=lambda x: (x["finished"], -x["done"]))
            return self._send(200, items)
        if path == "/api/admin/logs":
            conn = db()
            rows = conn.execute("SELECT sid, payload FROM sessions").fetchall()
            conn.close()
            items = []
            for row in rows:
                try:
                    data = json.loads(row["payload"])
                except Exception:
                    continue
                live = _sessions.get(row["sid"])
                name = live.name if live is not None else data.get("name", "")
                logs = live.logs if live is not None else data.get("logs", [])
                commands = []
                for entry in logs:
                    line = entry.get("in", "").strip() if isinstance(entry, dict) else str(entry).strip()
                    if line:
                        commands.append(line)
                items.append({"sid": row["sid"], "name": name, "commands": commands})
            items.sort(key=lambda x: x["name"] or "")
            return self._send(200, items)
        if path == "/api/admin/progress":
            conn = db()
            rows = conn.execute("SELECT sid, payload FROM sessions").fetchall()
            conn.close()
            items = []
            for row in rows:
                try:
                    data = json.loads(row["payload"])
                except Exception:
                    continue
                records = data.get("records", [])
                live = _sessions.get(row["sid"])
                finished = bool(data.get("submitted") or data.get("finished_at")
                                or (live is not None and live.finished))
                pen = live.penalty if live is not None else int(data.get("penalty") or 0)
                series = progress_series(records, len(STEPS), pen)
                items.append({
                    "sid": row["sid"],
                    "name": live.name if live is not None else data.get("name", ""),
                    "student_no": live.student_no if live is not None else data.get("student_no", ""),
                    "finished": finished, "run": live is not None and not live.finished,
                    "done": len(records), "total": len(STEPS),
                    "score": score_int(live.score() if live is not None else (series[-1]["y"] if series else 0)),
                    "series": series,
                })
            items.sort(key=lambda x: (not x["run"], -(x["done"] or 0), x["name"] or ""))
            return self._send(200, items)
        if path == "/api/admin/export_matrix.csv":
            conn = db()
            rows = conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()
            conn.close()
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["姓名", "学号/工号"] + [st["expect"] for st in STEPS] + ["总分"])
            for r in rows:
                try:
                    detail = json.loads(r["detail"] or "[]")
                except Exception:
                    detail = []
                passed = {d.get("no") for d in detail if d.get("status") == "pass"}
                cells = [st.get("points", 2) if (i + 1) in passed else ""
                         for i, st in enumerate(STEPS)]
                writer.writerow([r["name"], r["student_no"]] + cells + [score_int(r["score"])])
            return self._send_csv(buf, "linux-exam-lite-matrix.csv")
        if path == "/api/admin/export.csv":
            conn = db()
            rows = conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()
            conn.close()
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["学号/工号", "姓名", "得分", "已完成任务", "任务总数",
                             "用时(秒)", "退出全屏扣分", "是否交卷", "开始时间",
                             "交卷时间", "会话ID"])
            for r in rows:
                writer.writerow([r["student_no"], r["name"], score_int(r["score"]), r["passed"],
                                 r["total"], r["elapsed"], r["penalty"] or 0,
                                 "是" if r["finished"] else "否", r["started_at"],
                                 r["finished_at"], r["sid"]])
            return self._send_csv(buf, "linux-exam-lite-scores.csv")
        return self._send(404, {"error": "not found"})

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        path = urlparse(self.path).path
        if self.role == "teacher":
            return self._teacher_post(path)
        return self._student_post(path)

    def _student_post(self, path):
        body = self._json_body()
        if path == "/api/exam/start":
            gate = gate_state()
            early = gate["status"] == "before" and 0 < gate["seconds_to_start"] <= EARLY_SECONDS
            if gate["status"] != "open" and not early:
                # 未开考只允许提前看题；已结束/未开放则直接挡在门外
                return self._send(403, {"error": gate["message"] or "考试尚未开放", "gate": gate})
            name = str(body.get("name", "")).strip()[:32]
            student_no = str(body.get("student_no", "")).strip()[:32]
            if not name:
                return self._send(400, {"error": "请填写姓名"})
            with _lock:
                session = acquire_exam_session(name, student_no)
                return self._send(200, self._state_payload(session))
        if path == "/api/exam/resume":
            # 只找回、不新建：先认浏览器里的 sid，再认姓名。
            sid = str(body.get("sid", "")).strip()[:32]
            name = str(body.get("name", "")).strip()[:32]
            if not sid and not name:
                return self._send(400, {"error": "缺少 sid 或姓名"})
            with _lock:
                session = get_session(sid) if sid else None
                # sid 只是浏览器里存着的一串字符，不能只凭它就放行：换人坐到这台上
                # 填自己的姓名却拿到上一个人的环境，成绩就张冠李戴了。姓名对不上即作废。
                if session is not None and name and name_key(session.name) != name_key(name):
                    session = None
                if session is None or session.finished:
                    session = find_live_session_by_name(name)
            if session is None:
                return self._send(404, {"error": "没有可恢复的考试会话"})
            return self._send(200, self._state_payload(session))
        if path == "/api/exam/run":
            sid = str(body.get("sid", ""))
            line = str(body.get("line", ""))
            gate = gate_state()
            if gate["status"] == "before":
                return self._send(403, {"error": "尚未开考，请等待倒计时结束后再作答。",
                                        "gate": gate})
            if gate["status"] == "not_set":
                return self._send(403, {"error": gate["message"], "gate": gate})
            if gate["status"] == "closed":
                return self._send(403, {"error": gate["message"], "gate": gate})
            with _lock:
                session = get_session(sid)
                if session is None:
                    return self._send(404, {"error": "会话不存在，请重新填写姓名进入"})
                if session.finished:
                    return self._send(409, {"error": "本次考核已交卷",
                                            "summary": session.summary()})
                result = session.run(line)
                save_session(session)
                if result["finished"]:
                    save_submission(session)
                    _sessions.pop(session.sid, None)
                return self._send(200, result)
        if path == "/api/exam/finish":
            sid = str(body.get("sid", ""))
            with _lock:
                session = get_session(sid)
                if not session:
                    return self._send(404, {"error": "会话不存在"})
                session.finish()
                save_submission(session)
                save_session(session)
                _sessions.pop(sid, None)
                return self._send(200, session.summary())
        if path == "/api/exam/fullscreen":
            sid = str(body.get("sid", ""))
            with _lock:
                session = get_session(sid)
                if not session:
                    return self._send(404, {"error": "会话不存在"})
                if session.finished:
                    return self._send(200, {"score": session.score(), "penalty": session.penalty})
                session.add_penalty()
                save_session(session)
                return self._send(200, {"score": session.score(), "penalty": session.penalty})
        return self._send(404, {"error": "not found"})

    def _teacher_post(self, path):
        body = self._json_body()
        if path == "/api/admin/config":
            action = str(body.get("action", "save"))
            if action == "close":
                CONFIG["exam_closed"] = True
                save_config(CONFIG)
                with _lock:
                    for sid, session in list(_sessions.items()):
                        if not session.finished:
                            session.finish()
                            save_submission(session)
                            save_session(session)
                    _sessions.clear()
                return self._send(200, {"config": CONFIG, "gate": gate_state()})
            if action in ("now", "save"):
                if action == "now":
                    start = datetime.now()
                else:
                    start = parse_start(str(body.get("start", "")))
                    if start is None:
                        return self._send(400, {"error": "时间格式应为 2026-11-10T14:00"})
                try:
                    minutes = int(body.get("minutes", CONFIG.get("exam_minutes", 120)))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "考试时长应为整数分钟"})
                if minutes < 1 or minutes > 600:
                    return self._send(400, {"error": "考试时长应在 1–600 分钟之间"})
                CONFIG["exam_start"] = start.strftime("%Y-%m-%dT%H:%M")
                CONFIG["exam_minutes"] = minutes
                CONFIG["exam_closed"] = False
                save_config(CONFIG)
                with _lock:
                    # 统一截止时间变了，进行中的会话要跟着改绑
                    deadline = gate_end_timestamp()
                    for session in _sessions.values():
                        session.deadline = deadline
                return self._send(200, {"config": CONFIG, "gate": gate_state()})
            return self._send(400, {"error": "未知操作"})
        if path == "/api/admin/questions":
            action = str(body.get("action", "save"))
            if action == "reset":
                return self._send(200, {"ok": True, "count": questions.reset_questions()})
            items = body.get("questions")
            if not isinstance(items, list):
                return self._send(400, {"error": "缺少考题列表"})
            return self._send(200, {"ok": True, "count": questions.save_questions(items)})
        if path == "/api/admin/force_pass":
            sid = str(body.get("sid", ""))
            key = str(body.get("key", ""))
            if not sid or not key:
                return self._send(400, {"error": "缺少会话ID或题目key"})
            with _lock:
                session = get_session(sid)
                if session is None:
                    return self._send(404, {"error": "会话不存在"})
                result = session.force_pass(key)
                if result.get("ok"):
                    if session.finished:
                        save_submission(session)
                    save_session(session)
                    if session.finished:
                        _sessions.pop(sid, None)
                return self._send(200, result)
        if path == "/api/admin/record/delete":
            sid = str(body.get("sid", ""))
            if not sid:
                return self._send(400, {"error": "缺少会话ID"})
            conn = db()
            conn.execute("DELETE FROM submissions WHERE sid=?", (sid,))
            conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            conn.commit()
            conn.close()
            with _lock:
                _sessions.pop(sid, None)
            return self._send(200, {"ok": True})
        if path == "/api/admin/records/clear":
            conn = db()
            conn.execute("DELETE FROM submissions")
            conn.execute("DELETE FROM sessions")
            conn.commit()
            conn.close()
            with _lock:
                _sessions.clear()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


class TeacherHandler(Handler):
    role = "teacher"


def run_server(host="0.0.0.0", port=8100):
    init_db()
    load_config()
    restore_sessions()
    start_exam_janitor()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def run_teacher_server(port=8101):
    init_db()
    load_config()
    restore_sessions()
    start_exam_janitor()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), TeacherHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    import sys
    load_config()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    httpd = run_server(port=port)
    print(f"简化版考试系统已启动：http://localhost:{port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        httpd.shutdown()
