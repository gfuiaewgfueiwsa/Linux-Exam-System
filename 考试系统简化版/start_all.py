# -*- coding: utf-8 -*-
"""start_all —— 一键启动「考试系统简化版」+ Cloudflare Quick Tunnel。

与完整版同一套隧道方案，但没有 QEMU、没有镜像、没有开机等待：命令沙箱跑在本进程里，
启动即可用。

  教师控制台  http://localhost:8101   （只监听本机，不进隧道，免登录）
  学生端本机  http://localhost:8100   （供本机自测）
  学生端公网  3 条备用 https://xxx.trycloudflare.com（任选其一，写入 公网地址.txt）

用法：
    python start_all.py            # 学生端 + 教师端 + 公网隧道
    python start_all.py --local    # 只启动本机，不开隧道
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "app"))
# 完整版主项目里的 cloudflared 是共享工具，简化版优先找自己的 tools，再退回主项目
MAIN_BASE = os.path.dirname(BASE)

VERSION = "1.0"
PORT = int(os.environ.get("EXAM_PORT", "8100"))
TEACHER_PORT = int(os.environ.get("TEACHER_PORT", "8101"))
LOCAL_URL = f"http://localhost:{PORT}"
TEACHER_URL = f"http://localhost:{TEACHER_PORT}"


def _attach_job_kill_on_close():
    """Windows：把当前进程加入 Job Object，关闭窗口（进程被直接结束）时由系统
    一并终止 cloudflared 子进程，避免公网隧道残留占用端口。"""
    if os.name != "nt":
        return
    try:
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_size_t),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 句柄必须按 64 位指针声明，否则默认按 32 位 int 截断，绑定会失败
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return
        # 子进程默认继承父进程 job：父进程退出时句柄被 OS 关闭，触发 KILL_ON_JOB_CLOSE
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            kernel32.CloseHandle(job)
            return
    except Exception:
        pass


_attach_job_kill_on_close()

import server  # noqa: E402
from server import run_server, run_teacher_server, gate_state  # noqa: E402


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_stale_tunnels():
    """端口空闲说明没有在跑的考试系统，残留的 cloudflared 一律清掉，
    免得一次次启动把隧道进程堆到几十个。"""
    if os.name != "nt":
        return
    flags = 0x08000000  # CREATE_NO_WINDOW
    try:
        subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe", "/T"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=flags, check=False)
    except Exception:
        pass


def find_cloudflared():
    for candidate in (os.path.join(BASE, "tools", "cloudflared.exe"),
                      os.path.join(MAIN_BASE, "tools", "cloudflared.exe")):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("cloudflared")


def banner(lines):
    width = 72
    print("+" + "-" * width + "+")
    for line in lines:
        print("| " + line.ljust(width - 2) + " |")
    print("+" + "-" * width + "+")


def write_public_urls(urls):
    path = os.path.join(BASE, "公网地址.txt")
    with open(path, "w", encoding="utf-8") as fh:
        for u in urls:
            fh.write(u + "\n")


def main():
    local_only = "--local" in sys.argv
    if port_in_use(PORT) or port_in_use(TEACHER_PORT):
        print(f"端口 {PORT}/{TEACHER_PORT} 已被占用：可能简化版已经在运行。")
        print(f"教师控制台：{TEACHER_URL}")
        print(f"学生端：{LOCAL_URL}")
        print("若要重启，请先关闭原来的启动窗口。")
        webbrowser.open(TEACHER_URL)
        return

    kill_stale_tunnels()
    student = run_server(host="0.0.0.0", port=PORT)
    teacher = run_teacher_server(port=TEACHER_PORT)
    gate = gate_state()
    banner([
        f"考试系统简化版 v{VERSION} · 命令沙箱（无虚拟机、无镜像）",
        f"教师控制台：{TEACHER_URL}   （仅本机，免登录）",
        f"学生端本机：{LOCAL_URL}",
        f"当前考试状态：{gate['status']}  {gate.get('message', '')}",
        "公网隧道只转发学生端；关闭窗口或按 Ctrl+C 停止",
    ])

    time.sleep(0.4)
    webbrowser.open(TEACHER_URL)

    if local_only:
        print("\n[本地模式] 未启动公网隧道。按 Ctrl+C 退出。\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            teacher.shutdown()
            student.shutdown()
            print("\n服务已停止。")
        return

    cf = find_cloudflared()
    if not cf:
        print("\n[提示] 未找到 cloudflared.exe，本次仅提供本机访问。")
        print(f"把 cloudflared.exe 放到 {os.path.join(BASE, 'tools')} 或主项目 tools 后重启即可开公网。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            teacher.shutdown()
            student.shutdown()
        return

    print("\n正在启动 3 条 Cloudflare Quick Tunnel …（首次连接约 5-15 秒）")
    flags = 0x08000000 if os.name == "nt" else 0
    TUNNEL_COUNT = 3
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    urls = [""] * TUNNEL_COUNT
    procs: list[subprocess.Popen] = []
    lock = threading.Lock()

    def launch_tunnel(i: int):
        p = subprocess.Popen(
            [cf, "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=flags,
        )
        procs.append(p)

        def reader():
            for line in iter(p.stdout.readline, ""):
                line = line.rstrip()
                if not line:
                    continue
                m = pattern.search(line)
                if m:
                    with lock:
                        if not urls[i]:
                            urls[i] = m.group(0)
                    print(f"[cloudflared #{i + 1}] {m.group(0)}")
                elif "ERR" in line or "error" in line.lower():
                    print(f"[cloudflared #{i + 1}] " + line)

        threading.Thread(target=reader, daemon=True).start()

    for i in range(TUNNEL_COUNT):
        launch_tunnel(i)

    deadline = time.time() + 90
    while time.time() < deadline:
        with lock:
            if all(urls):
                break
        time.sleep(0.5)

    with lock:
        got = [u for u in urls if u]
    if got:
        os.environ["TUNNEL_URL"] = got[0]
        os.environ["TUNNEL_URLS"] = ",".join(got)
        write_public_urls(got)
        print("\n" + "=" * 74)
        print("学生公网入口（发给学生，任选其一，打不开就换下一个）：")
        for idx, u in enumerate(got, 1):
            print(f"  {idx}. {u}")
        print("教师控制台（只在本机打开，不要发出去）：")
        print("  " + TEACHER_URL)
        print("=" * 74 + "\n")
    else:
        print("\n[提示] 隧道地址获取超时，可重试或改用 --local 本地模式。\n")

    try:
        while True:
            time.sleep(1)
            if procs and all(p.poll() is not None for p in procs):
                break
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        teacher.shutdown()
        student.shutdown()
        print("\n服务已停止。")


if __name__ == "__main__":
    main()
