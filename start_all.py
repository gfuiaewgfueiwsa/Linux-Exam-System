# -*- coding: utf-8 -*-
"""start_all —— 一键启动考试系统 + Cloudflare Quick Tunnel。

启动后：
  教师控制台  http://localhost:8001   （只监听本机，不进隧道，免登录）
  学生端本地  http://localhost:8000   （供本机自测）
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


def _attach_job_kill_on_close():
    """Windows：把当前进程加入 Job Object，进程退出（含直接关闭 cmd 窗口）时
    由系统自动终止所有子进程（cloudflared / qemu / ssh），避免残留占用内存。"""
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
                ("ActiveProcessLimit", ctypes.c_uint32),
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
        # 显式声明 HANDLE 为 64 位指针与返回类型，避免 ctypes 默认按 32 位 int
        # 截断句柄，导致 Job Object 分配/绑定失败、关闭窗口时子进程无法被清理。
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
        # 子进程默认继承父进程 job；父进程退出时 job 句柄被 OS 关闭，触发
        # KILL_ON_JOB_CLOSE，所有子进程一并终止。保留句柄，不手动关闭。
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            kernel32.CloseHandle(job)
            return
    except Exception:
        pass


_attach_job_kill_on_close()

from server import run_server, run_teacher_server, CONFIG, gate_state  # noqa: E402
from lab_pool import POOL  # noqa: E402

VERSION = "3.0.33"

PORT = int(os.environ.get("EXAM_PORT", "8000"))
TEACHER_PORT = int(os.environ.get("TEACHER_PORT", "8001"))
LOCAL_URL = f"http://localhost:{PORT}"
TEACHER_URL = f"http://localhost:{TEACHER_PORT}"
CLOUDFLARED = os.path.join(BASE, "tools", "cloudflared.exe")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_stale_processes():
    """Windows：启动前强制结束上次残留的 QEMU 进程。

    关闭 cmd 窗口时若 Job Object 清理未生效，上一次运行的 qemu 进程会残留，
    导致重新启动后新老虚拟机叠加累积（7+7=14 台无限累计）。此处兜底清理。
    仅在端口空闲（无运行中的考试系统）时调用，避免误杀正在使用的虚拟机。
    """
    if os.name != "nt":
        return
    flags = 0x08000000  # CREATE_NO_WINDOW，避免弹出黑色命令窗口
    for name in ("qemu-system-x86_64.exe", "cloudflared.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", name, "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                check=False,
            )
        except Exception:
            pass


def find_cloudflared():
    if os.path.isfile(CLOUDFLARED):
        return CLOUDFLARED
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


def _report_lab():
    """真实 Linux 环境准备进度上报。"""
    for _ in range(240):
        st = POOL.state()
        if st["ready"]:
            print(f"[lab] 真实 Linux 环境就绪：最多 {st['max_vms']} 台并发虚拟机，"
                  f"镜像目录 {st['lab_dir']}")
            return
        if st["error"]:
            print("[lab] 环境未就绪：" + st["error"])
            return
        time.sleep(1)
    print("[lab] 环境准备仍在进行（首次启动需要下载镜像并预置系统，请稍候）")


def _exam_prewarm_scheduler():
    """开考前 30 分钟自动预热 14 台考试虚拟机；考试结束后回收。"""
    while True:
        try:
            gate = gate_state()
            status = gate.get("status")
            if status == "before" and gate.get("seconds_to_start", 0) <= 1800:
                POOL.prewarm_exam()
            elif status == "open":
                # 兼容教师直接点“立即开考”的情况；已占用的槽位会被跳过
                POOL.prewarm_exam()
            elif status == "closed":
                POOL.stop_exam_prewarm()
        except Exception:
            pass
        time.sleep(30)


def main():
    local_only = "--local" in sys.argv
    if port_in_use(PORT) or port_in_use(TEACHER_PORT):
        print(f"端口 {PORT}/{TEACHER_PORT} 已被占用：可能考试系统已经在运行。")
        print(f"教师控制台：{TEACHER_URL}")
        print(f"学生端：{LOCAL_URL}")
        print("若要重启，请先关闭原来的启动窗口。")
        webbrowser.open(TEACHER_URL)
        return

    # 端口空闲说明没有运行中的考试系统，此时残留的 QEMU 均为上次关闭窗口
    # 未被清理的孤儿进程，强制清理避免重新启动后虚拟机无限累计。
    kill_stale_processes()

    student = run_server(host="0.0.0.0", port=PORT)
    teacher = run_teacher_server(port=TEACHER_PORT)
    gate = gate_state()
    banner([
        f"Linux 实训考核系统 v{VERSION} · 真实 Linux 环境（QEMU/Alpine）",
        f"教师控制台：{TEACHER_URL}   （仅本机，免登录）",
        f"学生端本机：{LOCAL_URL}",
        f"当前考试状态：{gate['status']}  {gate.get('message', '')}",
        "公网隧道只转发学生端；关闭窗口或按 Ctrl+C 停止",
    ])
    threading.Thread(target=_report_lab, daemon=True).start()
    threading.Thread(target=_exam_prewarm_scheduler, daemon=True).start()

    time.sleep(0.6)
    webbrowser.open(TEACHER_URL)

    if local_only:
        POOL.prewarm()
        print("\n[本地模式] 未启动公网隧道。按 Ctrl+C 退出。\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            teacher.shutdown()
            student.shutdown()
            POOL.stop_all()
            print("\n服务已停止。")
        return

    cf = find_cloudflared()
    if not cf:
        print("\n[提示] 未找到 cloudflared.exe，本次仅提供本机访问。")
        print("把 cloudflared.exe 放到项目 tools 目录后重新启动即可开公网。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            teacher.shutdown()
            student.shutdown()
            POOL.stop_all()
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

    # 等待三条隧道地址全部出现（最长 90 秒，超时则以已获取的为准）
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

    # 隧道就绪后再预加载虚拟机（先隧道、后虚拟机），避免两者并发抢占 CPU/内存
    # 导致部分虚拟机 SSH 超时启动失败。
    POOL.prewarm()

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
        POOL.stop_all()
        print("\n服务已停止。")


if __name__ == "__main__":
    main()
