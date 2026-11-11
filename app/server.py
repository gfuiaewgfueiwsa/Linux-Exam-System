# -*- coding: utf-8 -*-
"""server —— 考试系统后端（Python 标准库，无第三方依赖）。

两个独立服务：
  学生端  0.0.0.0:8000   —— 只暴露学生页面与答题接口，由 Cloudflare 隧道转发
  教师端  127.0.0.1:8001 —— 只在本机可访问，免登录，负责设置考试时间与查看成绩
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import exam_flow
from exam_flow import ExamSession, STEPS
from lab_pool import POOL
from lab_qemu import orphan_session_images
from real_sim import RealSim
from term_bridge import TermBridge, close_worker, get_worker
from wsproto import WebSocket

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "web")
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "exam.db")
CONFIG_PATH = os.path.join(DATA, "config.json")

os.makedirs(DATA, exist_ok=True)

_sessions: dict[str, ExamSession] = {}
_practice: dict[str, dict] = {}
# 练习 token 只校验 WebSocket 握手那一刻，长时间挂着的连接不受影响；
# 这里只回收“申请了却始终没连上”的僵尸 token。
PRACTICE_TOKEN_TTL = 6 * 3600
_lock = threading.Lock()

exam_flow.REAL_VM_LOOKUP = POOL.get

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
  hints INTEGER,
  penalty INTEGER DEFAULT 0,
  finished INTEGER,
  started_at TEXT,
  finished_at TEXT,
  detail TEXT,
  commands TEXT
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(CREATE_SQL)
    # 迁移：为旧库的 submissions 表补充 penalty 列（退出全屏扣分）
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(submissions)").fetchall()]
        if "penalty" not in cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN penalty INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    conn.close()


def restore_sessions():
    """服务启动时从 SQLite 恢复未交卷会话到内存，避免重启后教师端丢失在线学生数据。"""
    try:
        conn = db()
        rows = conn.execute("SELECT payload FROM sessions").fetchall()
        conn.close()
    except Exception:
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


def report_orphan_images():
    """启动时报告无主的考试磁盘镜像（只读），提示管理员手动清理。"""
    try:
        conn = db()
        known = {row["sid"] for row in conn.execute("SELECT sid FROM sessions")}
        conn.close()
    except Exception:
        return
    orphans = orphan_session_images(known)
    if not orphans:
        return
    mb = sum(size for _, size in orphans) / 1048576.0
    print(f"[exam] 发现 {len(orphans)} 个无会话记录的考试磁盘镜像（约 {mb:.0f} MB）；"
          "确认没有学生在考后，可手动删除 data/lab/images/sessions/ 下对应的 <sid>.qcow2")


def load_config():
    default = {
        "exam_title": "Linux 基础命令 · 连续实操考核",
        "exam_start": "",
        "exam_minutes": 120,
        "exam_closed": False,
    }
    if not os.path.exists(CONFIG_PATH):
        save_config(default)
        return default
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        default.update({k: v for k, v in cfg.items() if k in default})
    except Exception:
        pass
    return default


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


CONFIG = load_config()


def parse_start(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    # 教师端只填时分（HH:MM / HH:MM:SS），日期默认取今天
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(value, fmt)
            now = datetime.now()
            return now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        except ValueError:
            continue
    return None


def gate_state():
    """返回考试闸门状态：not_set / before / open / closed。"""
    now = datetime.now()
    if CONFIG.get("exam_closed"):
        return {"status": "closed", "message": "考试已结束，等待教师安排下一场。",
                "start": CONFIG.get("exam_start", ""), "minutes": CONFIG.get("exam_minutes", 120),
                "seconds_to_start": 0, "remaining": 0}
    start = parse_start(CONFIG.get("exam_start", ""))
    if start is None:
        return {"status": "open", "message": "",
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


EARLY_SECONDS = 300  # 允许学生提前 5 分钟进入查看题目（不分配虚拟机）


def gate_end_timestamp():
    """返回本场考试截止时间戳（与当前闸门状态无关）；未设置开考时间返回 None。"""
    start = parse_start(CONFIG.get("exam_start", ""))
    if start is None:
        return None
    minutes = max(1, int(CONFIG.get("exam_minutes", 120)))
    return (start + timedelta(minutes=minutes)).timestamp()


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


def name_key(name):
    """姓名绑定键：折叠空白并忽略大小写，让「张 三」「张三」「Zhang San」命中同一台虚拟机。"""
    return " ".join(str(name or "").split()).casefold()


def find_live_session_by_name(name):
    """按姓名找未交卷会话；调用方必须持有 _lock。"""
    key = name_key(name)
    if not key:
        return None
    for session in _sessions.values():
        if not session.finished and name_key(session.name) == key:
            return session
    return None


def acquire_exam_session(name, student_no=""):
    """姓名 → 会话：已有同名未交卷会话就接管，否则新建。调用方必须持有 _lock。

    虚拟机池的键是 exam:<sid>，复用同一 sid 即复用同一台虚拟机与其磁盘，
    学生关标签页、换浏览器、断网重连后回到的是自己原来那台 Linux。
    查找与新建必须在同一把锁内完成，否则两个同名请求会各建一台。
    """
    session = find_live_session_by_name(name)
    if session is not None:
        return session
    gate = gate_state()
    sid = uuid.uuid4().hex[:16]
    deadline = gate_deadline() if gate["status"] == "open" else gate_end_timestamp()
    session = ExamSession(sid, name, student_no, deadline=deadline)
    session.sim = RealSim(None)
    _sessions[sid] = session
    save_session(session)
    return session


def save_submission(session: ExamSession):
    summary = session.summary()
    started = datetime.fromtimestamp(session.started).strftime("%Y-%m-%d %H:%M:%S")
    ended = datetime.fromtimestamp(session.finished_at or time.time()).strftime("%Y-%m-%d %H:%M:%S")
    conn = db()
    conn.execute(
        """INSERT OR REPLACE INTO submissions
           (sid,name,student_no,score,passed,total,elapsed,hints,penalty,finished,started_at,finished_at,detail,commands)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (session.sid, session.name, session.student_no, summary["score"],
         summary["passed"], summary["total"], summary["elapsed"], 0,
         summary["penalty"],
         1 if (summary["submitted"] or summary["finished"]) else 0, started, ended,
         json.dumps(summary["detail"], ensure_ascii=False),
         json.dumps(summary["commands"], ensure_ascii=False)))
    conn.commit()
    conn.close()


def release_exam(sid, wait=False):
    """交卷/清理后回收虚拟机并删除学生的持久磁盘镜像。

    默认在后台回收：关机和删除镜像最多需要十几秒，不能阻塞交卷响应或教师端
    “结束考试”请求。多个会话同时回收时各自独立线程并行执行。
    """
    close_worker("exam:" + sid)

    def _cleanup():
        POOL.release("exam:" + sid, reset=True)

    if wait:
        _cleanup()
    else:
        threading.Thread(target=_cleanup, daemon=True).start()


_janitor_started = False


def start_exam_janitor():
    """启动考试到期看护线程：到截止时间自动交卷并回收虚拟机。"""
    global _janitor_started
    if _janitor_started:
        return
    _janitor_started = True
    threading.Thread(target=_exam_janitor, daemon=True).start()


def _exam_janitor():
    while True:
        time.sleep(2)
        due = []
        with _lock:
            for sid, s in list(_sessions.items()):
                # 用 remaining() 而不是 deadline：教师未设置统一开考时间时
                # deadline 为 None，会话只能靠学生自己敲下一条命令才会发现超时，
                # 学生直接关掉浏览器的话这台虚拟机就永远占着槽位。
                if not s.finished and s.remaining() <= 0:
                    due.append(sid)
            now = time.time()
            for token in [t for t, v in _practice.items()
                          if now - v.get("created", now) > PRACTICE_TOKEN_TTL]:
                _practice.pop(token, None)
        for sid in due:
            try:
                with _lock:
                    s = _sessions.get(sid)
                    if s is None or s.finished:
                        continue
                    s.finish()
                    save_submission(s)
                    save_session(s)
                release_exam(sid)
            except Exception:
                pass


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _int(qs, name, default):
    try:
        return int((qs.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default


def progress_series(records, total, penalty=0):
    """把按完成顺序排列的答题记录转成“已完成命令数 -> 分数”的折线点。

    起点 (0, 0) 表示尚未答对任何命令；每答对一个命令，折线向右前进一步，
    分数按各题 points 加权累计后折算成 100 分制，与 ExamSession.score 一致。
    """
    pts = [{"x": 0, "y": 0}]
    if total <= 0:
        return pts
    point_map = {st["key"]: st.get("points", 2) for st in STEPS}
    total_points = sum(point_map.values())
    earned = 0
    for i, r in enumerate(records or []):
        done = i + 1
        earned += point_map.get(r.get("key"), 2)
        y = earned / total_points * 100 if total_points else 0
        pts.append({"x": done, "y": round(max(0.0, y - penalty), 1)})
    return pts


class Handler(BaseHTTPRequestHandler):
    server_version = "LinuxExam/2.0"
    role = "student"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------------ 工具
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json_bytes(body)
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

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2 * 1024 * 1024:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
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
        self.wfile.write(body)

    def _serve_static(self, rel, ctype):
        path = os.path.normpath(os.path.join(WEB, rel))
        if not path.startswith(WEB) or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ 终端 WebSocket
    def _serve_term(self):
        if self.role == "teacher":
            return self._send(404, {"error": "not found"})
        qs = self._query()
        mode = (qs.get("mode") or ["practice"])[0]
        ws = WebSocket.accept(self)
        if ws is None:
            return
        key = ""
        token = ""
        sid = ""
        vm = None
        try:
            if mode == "exam":
                sid = (qs.get("sid") or [""])[0]
                session = get_session(sid)
                if session is None:
                    ws.send_text(json.dumps({"type": "error", "message": "会话不存在"}, ensure_ascii=False))
                    return ws.close()
                if session.finished:
                    ws.send_text(json.dumps({"type": "error", "message": "本次考核已交卷"}, ensure_ascii=False))
                    return ws.close()
                # 提前进入阶段不分配虚拟机：未到开考时间直接拒绝连接，由前端倒计时结束后重连
                gate = gate_state()
                if gate["status"] == "before":
                    ws.send_text(json.dumps({"type": "error", "message": "尚未开考，请等待倒计时结束后自动连接"},
                                            ensure_ascii=False))
                    return ws.close()
                key = "exam:" + sid
                ws.send_text(json.dumps({"type": "status", "message": "正在启动 Linux 环境…"},
                                        ensure_ascii=False))
                # 常驻终端：虚拟机启动/重连都在 Worker 线程中完成，当前连接立即
                # 进入收发循环；启动期间 Worker 持续推送状态，前端不会误判超时。
                worker = get_worker(key, session=session,
                                    on_update=self._after_command,
                                    on_activity=lambda: POOL.touch(key))
                worker.attach(ws, rows=_int(qs, "rows", 32), cols=_int(qs, "cols", 110))
                worker.serve_ws(ws)
                return
            else:
                token = (qs.get("token") or [""])[0]
                if token not in _practice:
                    ws.send_text(json.dumps({"type": "error", "message": "练习会话已失效"}, ensure_ascii=False))
                    return ws.close()
                key = "practice:" + token
                ws.send_text(json.dumps({"type": "status", "message": "正在启动 Linux 环境…"},
                                        ensure_ascii=False))
                vm = POOL.acquire(key, timeout=420)
                ws.send_text(json.dumps({"type": "status", "message": ""}, ensure_ascii=False))
                ws.send_text(json.dumps({"type": "machine", "id": getattr(vm, "label", "")},
                                        ensure_ascii=False))
                bridge = TermBridge(ws, vm, None, rows=_int(qs, "rows", 32),
                                    cols=_int(qs, "cols", 110),
                                    on_activity=lambda: POOL.touch(key))
            bridge.run()
        except Exception as exc:          # noqa: BLE001
            try:
                ws.send_text(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False))
            except Exception:
                pass
        finally:
            # 考试断线不再销毁 shell：常驻 Worker 会保留会话，重连直接接管。
            # 练习断开立即回收虚拟机。
            if mode == "practice" and key:
                POOL.release(key)
                _practice.pop(token, None)
            ws.close()

    def _after_command(self, session, result):
        with _lock:
            if result.get("finished"):
                save_submission(session)
            save_session(session)
        # 考试完成（含完成最后一条命令）后稍等片刻再回收：
        # 先让 Worker 把 finished 状态推给浏览器，前端才能正确弹出成绩页。
        if result.get("finished"):
            threading.Timer(1.2, release_exam, args=(session.sid,)).start()

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if urlparse(self.path).path == "/ws/term":
            return self._serve_term()
        if self.role == "teacher":
            return self._teacher_get()
        return self._student_get()

    def _student_get(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path.startswith("/vendor/"):
            name = path[len("/vendor/"):]
            ctype = "application/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
            return self._serve_static(os.path.join("vendor", name), ctype)
        if path == "/api/lab/status":
            return self._send(200, POOL.state())
        if path == "/api/health":
            return self._send(200, {
                "ok": True, "service": "linux-exam", "role": "student",
                "local": f"http://localhost:{self.server.server_port}",
                "tunnel": os.environ.get("TUNNEL_URL", ""),
                "steps": len(STEPS), "active": len(_sessions),
                "gate": gate_state(),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        if path == "/api/exam/gate":
            return self._send(200, gate_state())
        if path == "/api/exam/state":
            sid = (self._query().get("sid") or [""])[0]
            session = get_session(sid)
            if not session:
                return self._send(404, {"error": "会话不存在"})
            return self._send(200, self._state_payload(session))
        if path == "/api/result":
            sid = (self._query().get("sid") or [""])[0]
            session = get_session(sid)
            if session:
                return self._send(200, session.summary())
            conn = db()
            row = conn.execute("SELECT * FROM submissions WHERE sid=?", (sid,)).fetchone()
            conn.close()
            if not row:
                return self._send(404, {"error": "成绩不存在"})
            return self._send(200, {
                "name": row["name"], "student_no": row["student_no"], "sid": row["sid"],
                "score": row["score"], "passed": row["passed"], "total": row["total"],
                "elapsed": row["elapsed"],
                "finished": bool(row["finished"]),
                "detail": json.loads(row["detail"] or "[]"),
                "commands": json.loads(row["commands"] or "{}"),
            })
        return self._send(404, {"error": "not found"})

    def _teacher_get(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/teacher", "/teacher.html"):
            return self._serve_file("teacher.html", "text/html; charset=utf-8")
        if path == "/api/health":
            return self._send(200, {"ok": True, "role": "teacher", "steps": len(STEPS),
                                    "live": len([s for s in _sessions.values() if not s.finished])})
        if path == "/api/admin/config":
            return self._send(200, {"config": CONFIG, "gate": gate_state()})
        if path == "/api/admin/questions":
            return self._send(200, exam_flow.all_questions())
        if path == "/api/admin/submissions":
            conn = db()
            rows = conn.execute("SELECT * FROM submissions ORDER BY id DESC").fetchall()
            conn.close()
            return self._send(200, [{
                "sid": r["sid"], "name": r["name"], "student_no": r["student_no"],
                "score": r["score"], "passed": r["passed"], "total": r["total"],
                "elapsed": r["elapsed"], "finished": bool(r["finished"]),
                "started_at": r["started_at"], "finished_at": r["finished_at"],
                "penalty": r["penalty"] if "penalty" in r.keys() else 0,
            } for r in rows])
        if path == "/api/admin/stats":
            conn = db()
            rows = conn.execute("SELECT * FROM submissions").fetchall()
            conn.close()
            scores = [r["score"] for r in rows]
            live = [s for s in _sessions.values() if not s.finished]
            return self._send(200, {
                "count": len(rows), "finished": len([r for r in rows if r["finished"]]),
                "avg": round(sum(scores) / len(scores), 1) if scores else 0,
                "max": max(scores) if scores else 0,
                "min": min(scores) if scores else 0,
                "pass_rate": round(len([s for s in scores if s >= 60]) / len(scores) * 100, 1) if scores else 0,
                "online": len(live),
                "total_students": len(_sessions),
                "lab": POOL.state(),
            })
        if path == "/api/admin/live":
            items = []
            for s in _sessions.values():
                if s.finished:
                    continue
                items.append({
                    "sid": s.sid, "name": s.name, "student_no": s.student_no,
                    "done": len(s.records), "total": len(STEPS), "score": s.score(),
                    "remaining": s.remaining(), "finished": s.finished,
                    "last_active": datetime.fromtimestamp(s.last_active).strftime("%H:%M:%S"),
                    "penalty": getattr(s, "penalty", 0),
                    "tasks": s.public_tasks(include_expect=True),
                })
            items.sort(key=lambda x: (x["finished"], -x["done"]))
            return self._send(200, items)
        if path == "/api/admin/logs":
            # 每台机器（会话）学生实际输入的命令日志，供教师端折叠查看
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
                for l in logs:
                    if isinstance(l, dict):
                        line = (l.get("in") or "").strip()
                    else:
                        line = str(l).strip()
                    if line:
                        commands.append(line)
                items.append({"sid": row["sid"], "name": name, "commands": commands})
            items.sort(key=lambda x: x["name"])
            return self._send(200, items)
        if path == "/api/admin/progress":
            # 每位学生的“已完成命令数 -> 分数”进度折线，供教师端动态曲线展示
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
                run = live is not None and not live.finished
                pen = live.penalty if live is not None else int(data.get("penalty") or 0)
                series = progress_series(records, len(STEPS), pen)
                score = live.score() if live is not None else (series[-1]["y"] if series else 0)
                items.append({
                    "sid": row["sid"],
                    "name": (live.name if live is not None else data.get("name", "")),
                    "student_no": (live.student_no if live is not None else data.get("student_no", "")),
                    "finished": finished, "run": run,
                    "done": len(records), "total": len(STEPS),
                    "score": score, "series": series,
                })
            items.sort(key=lambda x: (not x["run"], -(x["done"] or 0), x["name"]))
            return self._send(200, items)
        if path == "/api/admin/export_matrix.csv":
            # 成绩矩阵：第一列姓名、第一行 41 道命令，每名学生行末为总分
            conn = db()
            rows = conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()
            conn.close()
            buf = io.StringIO()
            writer = csv.writer(buf)
            header = ["姓名", "学号/工号"] + [st["expect"] for st in STEPS] + ["总分"]
            writer.writerow(header)
            for r in rows:
                detail = []
                try:
                    detail = json.loads(r["detail"] or "[]")
                except Exception:
                    detail = []
                passed = {d.get("no") for d in detail if d.get("status") == "pass"}
                cells = []
                for i, st in enumerate(STEPS):
                    cells.append(st.get("points", 2) if (i + 1) in passed else "")
                writer.writerow([r["name"], r["student_no"]] + cells + [r["score"]])
            data = "\ufeff" + buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=linux-exam-matrix.csv")
            body = data.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/admin/export.csv":
            conn = db()
            rows = conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()
            conn.close()
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["学号/工号", "姓名", "得分", "已完成任务", "任务总数", "用时(秒)",
                             "是否交卷", "开始时间", "交卷时间", "会话ID"])
            for r in rows:
                writer.writerow([r["student_no"], r["name"], r["score"], r["passed"],
                                 r["total"], r["elapsed"],
                                 "是" if r["finished"] else "否", r["started_at"],
                                 r["finished_at"], r["sid"]])
            data = "\ufeff" + buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=linux-exam-scores.csv")
            body = data.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send(404, {"error": "not found"})

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        if self.role == "teacher":
            return self._teacher_post()
        return self._student_post()

    def _student_post(self):
        path = urlparse(self.path).path
        body = self._json_body()
        if path == "/api/exam/start":
            gate = gate_state()
            early = gate["status"] == "before" and 0 < gate["seconds_to_start"] <= EARLY_SECONDS
            if gate["status"] != "open" and not early:
                return self._send(403, {"error": gate["message"], "gate": gate})
            name = str(body.get("name", "")).strip()[:32]
            student_no = str(body.get("student_no", "")).strip()[:32]
            if not name:
                return self._send(400, {"error": "请填写姓名"})
            with _lock:
                session = acquire_exam_session(name, student_no)
            return self._send(200, self._state_payload(session))
        if path == "/api/exam/resume":
            # 只找回、不新建：先认浏览器里的 sid，再认姓名。姓名是虚拟机的绑定键，
            # 关标签页后 sessionStorage 里的 sid 没了，也能凭姓名回到自己那台 Linux。
            sid = str(body.get("sid", "")).strip()[:32]
            name = str(body.get("name", "")).strip()[:32]
            if not sid and not name:
                return self._send(400, {"error": "缺少 sid 或姓名"})
            with _lock:
                session = get_session(sid) if sid else None
                if session is None:
                    session = find_live_session_by_name(name)
            if session is None:
                return self._send(404, {"error": "没有可恢复的考试会话"})
            return self._send(200, self._state_payload(session))
        if path == "/api/exam/finish":
            sid = str(body.get("sid", ""))
            with _lock:
                session = get_session(sid)
                if not session:
                    return self._send(404, {"error": "会话不存在"})
                session.finish()
                save_submission(session)
                save_session(session)
                summary = session.summary()
            release_exam(sid)
            return self._send(200, summary)
        if path == "/api/exam/fullscreen":
            # 学生端报告考试中退出全屏：记录违规并自动扣 1 分
            sid = str(body.get("sid", ""))
            with _lock:
                session = get_session(sid)
                if not session:
                    return self._send(404, {"error": "会话不存在"})
                if session.finished:
                    return self._send(200, {"score": session.score(),
                                            "penalty": getattr(session, "penalty", 0)})
                session.add_penalty()
                save_session(session)
                penalty = session.penalty
                score = session.score()
            return self._send(200, {"score": score, "penalty": penalty})
        if path == "/api/practice/start":
            token = uuid.uuid4().hex[:12]
            _practice[token] = {"created": time.time()}
            return self._send(200, {
                "token": token,
                "ws": f"/ws/term?mode=practice&token={token}",
                "lab": POOL.state(),
            })
        if path == "/api/practice/close":
            token = str(body.get("token", ""))
            _practice.pop(token, None)
            POOL.release("practice:" + token)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def _teacher_post(self):
        path = urlparse(self.path).path
        body = self._json_body()
        if path == "/api/admin/config":
            action = str(body.get("action", "save"))
            if action == "close":
                CONFIG["exam_closed"] = True
                save_config(CONFIG)
                with _lock:
                    sids = list(_sessions.keys())
                    for s in _sessions.values():
                        if not s.finished:
                            s.finish()
                            save_submission(s)
                            save_session(s)
                for sid in sids:
                    release_exam(sid)
                POOL.stop_exam_prewarm()
                return self._send(200, {"config": CONFIG, "gate": gate_state()})
            if action in ("now", "save"):
                if action == "now":
                    start = datetime.now()
                else:
                    start = parse_start(str(body.get("start", "")))
                    if start is None:
                        return self._send(400, {"error": "时间格式应为 2026-11-10T14:00"})
                minutes = int(body.get("minutes", CONFIG.get("exam_minutes", 120)))
                if minutes < 1 or minutes > 600:
                    return self._send(400, {"error": "考试时长应在 1–600 分钟之间"})
                CONFIG["exam_start"] = start.strftime("%Y-%m-%dT%H:%M")
                CONFIG["exam_minutes"] = minutes
                CONFIG["exam_closed"] = False
                save_config(CONFIG)
                gate = gate_state()
                if gate["status"] == "open" or (
                        gate["status"] == "before"
                        and gate.get("seconds_to_start", 0) <= 1800):
                    POOL.prewarm_exam()
                return self._send(200, {"config": CONFIG, "gate": gate})
            return self._send(400, {"error": "未知操作"})
        if path == "/api/admin/questions":
            action = str(body.get("action", "save"))
            if action == "reset":
                count = exam_flow.reset_questions()
                return self._send(200, {"ok": True, "count": count})
            questions = body.get("questions")
            if not isinstance(questions, list):
                return self._send(400, {"error": "缺少考题列表"})
            count = exam_flow.save_questions(questions)
            return self._send(200, {"ok": True, "count": count})
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
            release_exam(sid)
            return self._send(200, {"ok": True})
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
                finished = bool(result.get("finished"))
                if finished:
                    save_submission(session)
                save_session(session)
            # 教师把最后一题手动判过之后会话即结束，必须和正常交卷一样回收
            # 虚拟机，否则这台机器会一直占着槽位直到进程退出。
            if finished:
                release_exam(sid)
            return self._send(200, result)
        if path == "/api/admin/records/clear":
            conn = db()
            conn.execute("DELETE FROM submissions")
            conn.execute("DELETE FROM sessions")
            conn.commit()
            conn.close()
            with _lock:
                sids = list(_sessions.keys())
                _sessions.clear()
            for sid in sids:
                release_exam(sid)
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    # ------------------------------------------------------------------ 状态
    def _state_payload(self, session: ExamSession):
        return {
            "sid": session.sid, "name": session.name, "student_no": session.student_no,
            "prompt": session.sim.prompt(), "tasks": session.public_tasks(),
            "selected": session.public_task(session.selected) if session.selected else None,
            "progress": session.progress(), "score": session.score(),
            "badge": session.sim.mode_badge(),
            "remaining": session.remaining(), "elapsed": session.elapsed(),
            "finished": session.finished, "total": len(STEPS),
            "history": session.logs[-30:],
            "gate": gate_state(),
            "real": True,
        }


class TeacherHandler(Handler):
    role = "teacher"


def run_server(host="0.0.0.0", port=8000):
    """学生端服务。"""
    init_db()
    restore_sessions()
    report_orphan_images()
    start_exam_janitor()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def run_teacher_server(port=8001):
    """教师端服务，只监听本机环回地址。"""
    init_db()
    restore_sessions()
    start_exam_janitor()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), TeacherHandler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    httpd = run_server(port=p)
    print(f"考试系统已启动：http://localhost:{p}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        httpd.shutdown()
