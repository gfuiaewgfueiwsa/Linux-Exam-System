# -*- coding: utf-8 -*-
"""ui_server —— boot the exam servers on spare ports for manual/browser UI checks.

Uses a throwaway data store so the real data/ directory is untouched, and stubs
the QEMU pool so no virtual machine is ever started.

    python tests/ui_server.py                # gate: entry opens in 3 minutes
    python tests/ui_server.py --before 900   # gate: 15 minutes away (card locked)
    python tests/ui_server.py --open         # gate: open now

Student  http://127.0.0.1:8010
Teacher  http://127.0.0.1:8011
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

TMP = tempfile.mkdtemp(prefix="exam-ui-")

# Must be patched before lab_pool is imported: LabPool() starts its _prepare
# thread in the constructor, and _prepare purges images from the real lab dir.
import lab_qemu  # noqa: E402

lab_qemu.purge_unassigned_slot_images = lambda: 0
lab_qemu.provision_golden = lambda: None

import server  # noqa: E402
import exam_flow  # noqa: E402

# run_server() reports orphan images from the real lab dir; keep the harness quiet.
server.orphan_session_images = lambda known: []

server.DB_PATH = os.path.join(TMP, "exam.db")
server.CONFIG_PATH = os.path.join(TMP, "config.json")
exam_flow.QUESTIONS_PATH = os.path.join(TMP, "questions.json")
server.CONFIG = server.load_config()

from lab_pool import POOL  # noqa: E402

POOL.prewarm_exam = lambda count=None: None
POOL.stop_exam_prewarm = lambda: None
POOL.release = lambda key, reset=False: None
POOL.prewarm = lambda: None


def _no_vm(key, timeout=420):
    raise RuntimeError("UI harness: QEMU disabled")


POOL.acquire = _no_vm


def main():
    argv = sys.argv[1:]
    student_port = int(os.environ.get("UI_STUDENT_PORT", "8010"))
    teacher_port = int(os.environ.get("UI_TEACHER_PORT", "8011"))

    server.init_db()
    server.start_exam_janitor()
    server.run_server(host="127.0.0.1", port=student_port)
    server.run_teacher_server(port=teacher_port)

    if "--open" in argv:
        before = -60          # 已经开始 1 分钟
    elif "--before" in argv:
        before = int(argv[argv.index("--before") + 1])
    else:
        before = 180          # 3 分钟后开考：落在 5 分钟提前进入窗口内
    minutes = 60

    start = time.time() + before
    stamp = time.strftime("%Y-%m-%dT%H:%M", time.localtime(start))
    server.CONFIG["exam_start"] = stamp
    server.CONFIG["exam_minutes"] = minutes
    server.CONFIG["exam_closed"] = False
    server.save_config(server.CONFIG)

    print("student http://127.0.0.1:%d" % student_port)
    print("teacher http://127.0.0.1:%d" % teacher_port)
    print("gate %s" % server.gate_state())
    print("tmpdir %s" % TMP)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
