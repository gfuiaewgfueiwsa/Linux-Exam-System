# -*- coding: utf-8 -*-
"""lab_qemu —— 真实 Linux 环境（便携 QEMU + Alpine 云镜像）。

本机无需 Docker / WSL / 管理员权限：
  1. 自动发现 qemu-system-x86_64.exe（项目 tools/qemu/bin、PATH）
  2. 首次运行自动下载 Alpine cloudinit 镜像（约 150MB）
  3. 首次运行自动用 cloud-init 预置“金镜像”：bash/sudo/shadow/vim/less/tar/python3/openssh
  4. 每个学生会话用 snapshot 模式从金镜像启动独立虚拟机，互不影响，关机即还原

对外只暴露：QemuVM（start / ssh_run / open_shell / snapshot / stop）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAB_DIR = os.environ.get("EXAM_LAB_DIR") or os.path.join(BASE, "data", "lab")
QEMU_DIR = os.path.join(LAB_DIR, "qemu")
IMG_DIR = os.path.join(LAB_DIR, "images")
SESSION_IMG_DIR = os.path.join(IMG_DIR, "sessions")
KEY_PATH = os.path.join(LAB_DIR, "id_lab")
GOLDEN = os.path.join(IMG_DIR, "golden.qcow2")
CLOUD_IMG = os.path.join(IMG_DIR, "alpine-cloud.qcow2")

CLOUD_URL = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/cloud/alpine-3.24.2-x86_64-cloudinit-r2.qcow2"
SSH_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
SSH_KEYGEN = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh-keygen.exe")

_GUEST_USER = "student"
_GUEST_PASS = "student"
_ROOT_PASS = "root"

SNAPSHOT_PY = r'''#!/usr/bin/env python3
import json, os, pwd, grp, stat, subprocess, sys

MAX_NODES = 5000
MAX_CONTENT = 262144
ROOTS = [("/home", 7), ("/root", 7), ("/tmp", 5), ("/etc", 1), ("/", 1)]

def name_of_uid(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)

def name_of_gid(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)

nodes = {}

def add(path):
    if len(nodes) > MAX_NODES:
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        kind = "dir"
    elif stat.S_ISLNK(st.st_mode):
        kind = "link"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "other"
    item = {"kind": kind, "mode": stat.S_IMODE(st.st_mode),
            "owner": name_of_uid(st.st_uid), "group": name_of_gid(st.st_gid),
            "size": st.st_size}
    if kind == "link":
        try:
            item["target"] = os.readlink(path)
        except OSError:
            pass
    if kind == "file" and st.st_size <= MAX_CONTENT:
        try:
            with open(path, "r", errors="replace") as fh:
                item["content"] = fh.read()
        except OSError:
            pass
    nodes[path] = item
    return kind

def walk(root, depth):
    kind = add(root)
    if kind != "dir":
        return
    base = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if dirpath.rstrip("/").count("/") - base >= depth:
            dirnames[:] = []
            continue
        for d in list(dirnames):
            p = os.path.join(dirpath, d)
            if os.path.islink(p):
                add(p)
                dirnames.remove(d)
            else:
                add(p)
        for f in filenames:
            if len(nodes) > MAX_NODES:
                return
            add(os.path.join(dirpath, f))

def users():
    shadow = {}
    try:
        with open("/etc/shadow", "r", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(":")
                if len(parts) >= 2:
                    shadow[parts[0]] = parts[1]
    except OSError:
        pass
    groups_of = {}
    try:
        with open("/etc/group", "r", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(":")
                if len(parts) < 4:
                    continue
                members = [m for m in parts[3].split(",") if m]
                for m in members:
                    groups_of.setdefault(m, []).append(parts[0])
    except OSError:
        pass
    out = {}
    try:
        with open("/etc/passwd", "r", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(":")
                if len(parts) < 7:
                    continue
                name, uid, gid, home, shell = parts[0], parts[2], parts[3], parts[5], parts[6]
                try:
                    primary = grp.getgrgid(int(gid)).gr_name
                except (KeyError, ValueError):
                    primary = gid
                glist = [primary] + [g for g in groups_of.get(name, []) if g != primary]
                h = shadow.get(name, "")
                out[name] = {"uid": int(uid), "gid": int(gid), "home": home, "shell": shell,
                             "groups": glist, "passwd_set": bool(h) and h not in ("!", "*", "!!")}
    except OSError:
        pass
    return out

def group_table():
    out = {}
    try:
        with open("/etc/group", "r", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(":")
                if len(parts) < 4:
                    continue
                out[parts[0]] = {"gid": int(parts[2]), "members": [m for m in parts[3].split(",") if m]}
    except OSError:
        pass
    return out

def shell_state(tty):
    """定位前台 shell 的 pid / user / cwd。"""
    if not tty:
        return {}, None
    try:
        res = subprocess.run(["ps", "-e", "-o", "pid=,tty=,user=,args="], capture_output=True, text=True, timeout=10)
    except Exception:
        return {}, None
    best = None
    for line in (res.stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, t, user, args = parts
        if t.replace("/dev/", "") != tty.replace("/dev/", ""):
            continue
        if not re_match_shell(args):
            continue
        try:
            best = (int(pid), user)
        except ValueError:
            continue
    if not best:
        return {}, None
    pid, user = best
    cwd = ""
    try:
        cwd = os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        pass
    return {"user": user, "cwd": cwd, "pid": pid}, tty

def re_match_shell(args):
    for token in ("bash", "/bin/sh", "-sh", "ash", "zsh", "sh"):
        if args == token or args.startswith(token + " ") or ("/" + token) in args:
            return True
    return False

def main():
    tty = sys.argv[1] if len(sys.argv) > 1 else ""
    for root, depth in ROOTS:
        walk(root, depth)
    state, tty2 = shell_state(tty)
    data = {"nodes": nodes, "users": users(), "groups": group_table(),
            "shell": state, "tty": tty2 or tty}
    print(json.dumps(data, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


# --------------------------------------------------------------------- 基础工具
def _mkdirs():
    for d in (LAB_DIR, IMG_DIR):
        os.makedirs(d, exist_ok=True)


def free_port(start=22300, end=22900):
    for port in range(start, end):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("没有可用的本地端口")


def port_open(port, timeout=0.3):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_port(port, timeout=180.0, proc=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        if port_open(port):
            return True
        time.sleep(0.4)
    return False


def download(url, path, label=""):
    _mkdirs()
    tmp = path + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "exam-lab/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = time.time()
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if time.time() - last > 3:
                last = time.time()
                pct = (done / total * 100) if total else 0
                print(f"[lab] 下载{label} {done // 1048576}/{total // 1048576} MB ({pct:.0f}%)")
    os.replace(tmp, path)


# --------------------------------------------------------------------- QEMU 定位
def find_qemu():
    exe = "qemu-system-x86_64.exe"
    candidates = [
        os.path.join(QEMU_DIR, exe),
        os.path.join(QEMU_DIR, "bin", exe),
        os.path.join(BASE, "tools", "qemu", exe),
        os.path.join(BASE, "tools", "qemu", "bin", exe),
        os.path.join(LAB_DIR, "msys64", "mingw64", "bin", exe),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    found = shutil.which(exe) or shutil.which("qemu-system-x86_64")
    return found


def install_qemu(installer_path):
    """NSIS 静默安装到项目内 data\\lab\\qemu（无需管理员）。"""
    _mkdirs()
    if not os.path.isfile(installer_path):
        raise RuntimeError(f"未找到 QEMU 安装包：{installer_path}")
    print("[lab] 正在安装便携 QEMU …")
    subprocess.run([installer_path, "/S", "/D=" + QEMU_DIR], check=False, timeout=900)
    exe = os.path.join(QEMU_DIR, "qemu-system-x86_64.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("QEMU 安装失败，请手动安装后重试")
    return exe


# --------------------------------------------------------------------- SSH
def _fix_key_acl(path):
    """Windows 下收紧私钥 ACL，避免 ssh 报“私钥权限过宽”导致连接失败。"""
    if os.name != "nt":
        return
    try:
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        subprocess.run(["icacls", path, "/inheritance:r"],
                       capture_output=True, timeout=30, check=False)
        subprocess.run(["icacls", path, "/grant:r", f"{user}:F"],
                       capture_output=True, timeout=30, check=False)
    except Exception:
        pass


def ensure_key():
    _mkdirs()
    if os.path.isfile(KEY_PATH):
        _fix_key_acl(KEY_PATH)
        return KEY_PATH
    subprocess.run([SSH_KEYGEN, "-t", "ed25519", "-N", "", "-q", "-f", KEY_PATH],
                   check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _fix_key_acl(KEY_PATH)
    return KEY_PATH


def pub_key():
    with open(KEY_PATH + ".pub", "r", encoding="utf-8") as fh:
        return fh.read().strip()


def ssh_base(port, user=_GUEST_USER):
    return [SSH_EXE, "-p", str(port), "-i", KEY_PATH,
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=6",
            "-o", "PreferredAuthentications=publickey",
            "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=6",
            f"{user}@127.0.0.1"]


def ssh_run(port, command, timeout=30, user=_GUEST_USER, use_sudo=False):
    cmd = command
    if use_sudo:
        cmd = "sudo -n sh -c " + shlex_quote(command)
    try:
        res = subprocess.run(ssh_base(port, user) + [cmd], capture_output=True,
                             timeout=timeout, text=True, encoding="utf-8", errors="replace")
        return res.returncode, (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def shlex_quote(text):
    return "'" + text.replace("'", "'\"'\"'") + "'"


def wait_ssh(port, timeout=300, proc=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        code, _ = ssh_run(port, "true", timeout=8)
        if code == 0:
            return True
        time.sleep(1.0)
    return False


def session_image_path(sid):
    """考试会话的持久磁盘镜像路径。

    预热的考试虚拟机会提前占用一个 slot 镜像（pw-01.qcow2 …），分配给学生时
    把 sid 与 slot 的映射写入 exam_slots.json。这样服务器重启后仍能找到该学生
    原来的磁盘，继续使用同一份文件系统状态。
    """
    slot = slot_for_session(sid)
    if slot:
        return slot_image_path(slot)
    return os.path.join(SESSION_IMG_DIR, sid + ".qcow2")


def slot_image_path(index):
    """预热考试槽位的磁盘镜像路径（与具体学生无关，分配后写入映射）。"""
    return os.path.join(SESSION_IMG_DIR, "pw-%02d.qcow2" % int(index))


SLOT_MAP_PATH = os.path.join(LAB_DIR, "exam_slots.json")
_slot_lock = threading.Lock()


def _load_slot_map():
    try:
        with open(SLOT_MAP_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items() if str(v).isdigit()}
    except Exception:
        pass
    return {}


def _save_slot_map(mapping):
    try:
        _mkdirs()
        tmp = SLOT_MAP_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, SLOT_MAP_PATH)
    except Exception:
        pass


def slot_for_session(sid):
    with _slot_lock:
        return _load_slot_map().get(sid)


def assign_slot(sid, index):
    """把预热槽位分配给考试会话（持久化映射，重启后可恢复）。"""
    with _slot_lock:
        mapping = _load_slot_map()
        mapping[sid] = int(index)
        _save_slot_map(mapping)


def clear_slot(sid):
    with _slot_lock:
        mapping = _load_slot_map()
        if sid in mapping:
            mapping.pop(sid, None)
            _save_slot_map(mapping)


def slots_in_use():
    """返回当前已被学生会话占用的预热槽位编号集合。"""
    with _slot_lock:
        return set(_load_slot_map().values())


def ensure_slot_image(index):
    """准备一个预热槽位的持久镜像：从金镜像复制一次，之后虚拟机直接落盘。"""
    _mkdirs()
    os.makedirs(SESSION_IMG_DIR, exist_ok=True)
    path = slot_image_path(index)
    if os.path.isfile(path):
        return path
    if not os.path.isfile(GOLDEN):
        provision_golden()
    part = path + ".part"
    shutil.copyfile(GOLDEN, part)
    os.replace(part, path)
    print("[lab] 已创建考试预热镜像 " + os.path.basename(path))
    return path


def purge_unassigned_slot_images():
    """删除没有映射到任何考试会话的预热槽位镜像，返回删除的个数。

    预热镜像一旦启动过就被虚拟机写脏，考试结束后若留着，下一场 ensure_slot_image
    会把上一场的磁盘原样交给新学生，同时白占几百 MB 磁盘。已分配给未交卷会话的
    槽位必须保留，否则服务器重启后学生原来的文件系统就找不回来了。
    """
    in_use = slots_in_use()
    removed = 0
    try:
        names = os.listdir(SESSION_IMG_DIR)
    except OSError:
        return 0
    for name in names:
        if not (name.startswith("pw-") and name.endswith(".qcow2")):
            continue
        try:
            index = int(name[3:-6])
        except ValueError:
            continue
        if index in in_use:
            continue
        try:
            os.remove(os.path.join(SESSION_IMG_DIR, name))
            removed += 1
        except OSError:
            pass
    return removed


def orphan_session_images(known_sids):
    """列出没有会话记录的考试磁盘镜像 [(sid, bytes)]，只读不删。

    会话镜像以学生 sid 命名，只由考试分配路径创建。数据库被清空或损坏重建后
    这些镜像就再也找不到主人，但不能按「库里没有」自动删除：那会在一次启动里
    抹掉所有在考学生的文件系统。只报告，由管理员确认后手动清理。
    """
    try:
        names = os.listdir(SESSION_IMG_DIR)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".qcow2") or name.startswith("pw-"):
            continue
        sid = name[:-6]
        if len(sid) != 16 or any(c not in "0123456789abcdef" for c in sid):
            continue
        if sid in known_sids:
            continue
        try:
            out.append((sid, os.path.getsize(os.path.join(SESSION_IMG_DIR, name))))
        except OSError:
            pass
    return out


def ensure_session_image(sid):
    """为考试会话准备持久磁盘镜像（首次从金镜像复制一份独立 qcow2）。

    snapshot 模式下 QEMU 关机即丢弃磁盘改动，导致学生断连或服务器重启后
    已创建的用户/目录/文件全部丢失、无法继续推进命令。改用独立镜像并在
    无 snapshot 模式下启动后，文件系统改动会直接写入该镜像并持久保存。
    """
    _mkdirs()
    os.makedirs(SESSION_IMG_DIR, exist_ok=True)
    path = session_image_path(sid)
    if os.path.isfile(path):
        return path
    if not os.path.isfile(GOLDEN):
        provision_golden()
    part = path + ".part"
    shutil.copyfile(GOLDEN, part)
    os.replace(part, path)
    print(f"[lab] 已为会话 {sid} 创建持久磁盘镜像")
    return path


def delete_session_image(sid):
    """删除考试会话的持久磁盘镜像（交卷/清除记录后回收）。"""
    slot = slot_for_session(sid)
    clear_slot(sid)
    paths = [os.path.join(SESSION_IMG_DIR, sid + ".qcow2")]
    if slot:
        paths.append(slot_image_path(slot))
    for path in paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------- cloud-init
USER_DATA = """#cloud-config
hostname: exam-host
ssh_pwauth: true
disable_root: false
users:
  - name: student
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: student
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - __PUBKEY__
write_files:
  - path: /etc/sudoers.d/00-lab-no-pty
    permissions: '0440'
    content: |
      Defaults !use_pty
  - path: /etc/local.d/lab-sshd.start
    permissions: '0755'
    content: |
      #!/bin/sh
      mkdir -p /run/sshd
      ssh-keygen -A >/dev/null 2>&1
      /usr/sbin/sshd
  - path: /opt/lab_snapshot.py
    permissions: '0755'
    content: |
__SNAPSHOT__
runcmd:
  - apk update
  - apk add --no-cache bash sudo shadow vim less tar gzip coreutils findutils procps python3 openssh util-linux
  - echo 'root:root' | chpasswd
  - echo 'student:student' | chpasswd
  - usermod -s /bin/bash root
  - sh -c "grep -q '^CREATE_HOME' /etc/login.defs || echo 'CREATE_HOME yes' >> /etc/login.defs"
  - sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
  - sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
  - sh -c "grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config"
  - rc-update add local default || true
  - rc-update add sshd default || true
  - ssh-keygen -A || true
  - rc-service sshd restart || true
  - rc-update del cloud-init default || true
  - rc-update del cloud-init-local default || true
  - rc-update del cloud-config default || true
  - rc-update del cloud-final default || true
  - touch /opt/lab-ready
  - sync
"""

META_DATA = """instance-id: exam-lab-golden-2
local-hostname: exam-host
"""


class _SeedServer:
    """给 cloud-init 提供 NoCloud 种子（QEMU 用户网络里 10.0.2.2 指向本机）。"""

    def __init__(self, user_data):
        self.user_data = user_data
        server = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                body = {"/user-data": server.user_data, "/meta-data": META_DATA,
                        "/vendor-data": ""}.get(path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass


# --------------------------------------------------------------------- 金镜像预置
def _snapshot_indented():
    lines = []
    for line in SNAPSHOT_PY.strip().splitlines():
        lines.append("      " + line if line.strip() else "")
    return "\n".join(lines)


def provision_golden(force=False, log_path=None):
    """首次运行：用 cloud-init 把 Alpine 云镜像预置成金镜像。"""
    _mkdirs()
    if os.path.isfile(GOLDEN) and not force:
        return GOLDEN
    ensure_key()
    if not os.path.isfile(CLOUD_IMG):
        print("[lab] 首次运行，正在下载 Alpine 云镜像（约 150MB）…")
        download(CLOUD_URL, CLOUD_IMG, "云镜像")
    shutil.copyfile(CLOUD_IMG, GOLDEN + ".tmp.qcow2")

    user_data = (USER_DATA.replace("__PUBKEY__", pub_key())
                 .replace("__SNAPSHOT__", _snapshot_indented()))
    seed = _SeedServer(user_data)
    port = free_port()
    log_file = log_path or os.path.join(LAB_DIR, "provision.log")
    cmd = qemu_cmd(GOLDEN + ".tmp.qcow2", port, memory=640, snapshot=False,
                   smbios=f"type=1,serial=ds=nocloud-net;s=http://10.0.2.2:{seed.port}/",
                   serial_log=log_file)
    print(f"[lab] 正在预置金镜像（首次约 3–8 分钟）… 串口日志：{log_file}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=0x08000000)
    try:
        ok = wait_ssh(port, timeout=900, proc=proc)
        if not ok:
            raise RuntimeError("金镜像预置失败：SSH 未就绪，请查看 " + log_file)
        deadline = time.time() + 1500
        while time.time() < deadline:
            code, out = ssh_run(port, "test -f /opt/lab-ready && echo READY || echo WAIT", timeout=15)
            if "READY" in out:
                break
            if proc.poll() is not None:
                raise RuntimeError("金镜像预置过程中虚拟机关机，请查看 " + log_file)
            time.sleep(3)
        else:
            raise RuntimeError("金镜像预置超时，请查看 " + log_file)
        ssh_run(port, "sudo -n poweroff", timeout=10)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.terminate()
    finally:
        seed.stop()
        if proc.poll() is None:
            proc.terminate()
    os.replace(GOLDEN + ".tmp.qcow2", GOLDEN)
    print("[lab] 金镜像已就绪：" + GOLDEN)
    return GOLDEN


# --------------------------------------------------------------------- QEMU 进程
_ACCEL_LOCK = threading.Lock()
_ACCEL = None


def detect_accel():
    """探测可用的硬件加速器；不可用时回退到多线程 TCG。

    Windows 需要启用“Windows 虚拟机监控程序平台”后 WHPX 才可用。未启用时
    QEMU 只能走 TCG 软件模拟，Linux 命令会有明显延迟。这里在首次启动时用
    一个最小的暂停实例探测，成功则使用 WHPX，否则使用 tcg,thread=multi。
    """
    global _ACCEL
    with _ACCEL_LOCK:
        if _ACCEL is not None:
            return _ACCEL
        exe = find_qemu()
        _ACCEL = "tcg,thread=multi"
        if exe and os.name == "nt":
            proc = None
            try:
                proc = subprocess.Popen(
                    [exe, "-accel", "whpx", "-machine", "pc", "-m", "64",
                     "-display", "none", "-monitor", "none", "-nodefaults", "-S"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000)
                time.sleep(1.8)
                if proc.poll() is None:
                    _ACCEL = "whpx"
                    print("[lab] 已启用 WHPX 硬件加速")
                else:
                    print("[lab] WHPX 不可用，使用多线程 TCG（命令可能有模拟延迟）")
            except Exception:
                pass
            finally:
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        return _ACCEL


def qemu_cmd(image, port, memory=256, snapshot=True, smbios=None, serial_log=None,
             smp=2):
    exe = find_qemu()
    if not exe:
        raise RuntimeError("未找到 QEMU，请先把 qemu-w64-setup-*.exe 放到 tools/qemu 目录")
    cmd = [exe, "-machine", "pc", "-m", str(memory), "-smp", str(max(1, int(smp))),
           "-accel", detect_accel(),
           "-display", "none", "-monitor", "none",
           "-drive", f"file={image},if=virtio,format=qcow2" + (",snapshot=on" if snapshot else ""),
           "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:22",
           "-device", "virtio-net-pci,netdev=n0",
           "-no-user-config"]
    if smbios:
        cmd += ["-smbios", smbios]
    if serial_log:
        cmd += ["-serial", "file:" + serial_log]
    else:
        cmd += ["-serial", "none"]
    return cmd


class QemuVM:
    """一台真实 Alpine Linux 虚拟机（会话级）。"""

    def __init__(self, sid, memory=256, image=None, smp=2, label=""):
        self.sid = sid
        self.label = label      # 预加载编号（练习 01-07 / 考试 E01-…），普通会话为空
        self.port = free_port()
        self.proc = None
        self.memory = memory
        self.smp = smp
        self.image = image       # 持久会话镜像路径；None 表示用金镜像 + snapshot 模式
        self.created = time.time()
        self.last_used = time.time()
        self.shell = None
        self.tty = ""
        self.lock = threading.Lock()
        self.start_event = threading.Event()   # 启动完成（成功或失败）时置位
        self.start_error = ""
        self.starting = False
        self.ready = False

    # -- 生命周期 ------------------------------------------------------
    def start(self, timeout=240):
        self.starting = True
        self.start_event.clear()
        self.start_error = ""
        try:
            if self.image:
                # 持久会话镜像：无 snapshot 启动，文件系统改动直接落盘，重连/重启不丢
                if not os.path.isfile(self.image):
                    raise RuntimeError("会话磁盘镜像不存在")
                cmd = qemu_cmd(self.image, self.port, memory=self.memory, snapshot=False,
                               smp=self.smp)
            else:
                if not os.path.isfile(GOLDEN):
                    provision_golden()
                cmd = qemu_cmd(GOLDEN, self.port, memory=self.memory, snapshot=True,
                               smp=self.smp)
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         creationflags=0x08000000)
            if not wait_port(self.port, timeout=timeout, proc=self.proc):
                self.stop()
                raise RuntimeError("Linux 虚拟机启动超时")
            if not wait_ssh(self.port, timeout=120, proc=self.proc):
                self.stop()
                raise RuntimeError("Linux 虚拟机 SSH 未就绪")
            self.ready = True
            return self
        except Exception as exc:
            self.start_error = str(exc)
            raise
        finally:
            self.starting = False
            self.start_event.set()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def detach(self):
        """断开交互 shell 连接，但不关闭虚拟机（保持运行）。"""
        try:
            if self.shell is not None:
                self.shell.kill()
        except Exception:
            pass
        self.shell = None

    def stop(self):
        self.detach()
        if self.proc is not None and self.proc.poll() is None:
            try:
                ssh_run(self.port, "sudo -n poweroff", timeout=5)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # -- 能力 ----------------------------------------------------------
    def exec_root(self, script, timeout=45):
        return ssh_run(self.port, script, timeout=timeout, use_sudo=True)

    def snapshot(self, timeout=45):
        code, out = self.exec_root("python3 /opt/lab_snapshot.py " + shlex_quote(self.tty), timeout=timeout)
        if code != 0 or not out.strip():
            return {}
        line = out.strip().splitlines()[-1]
        try:
            return json.loads(line)
        except Exception:
            return {}

    def open_shell(self, rows=32, cols=110):
        """打开学生的交互式 bash（SSH 分配真实 PTY）。"""
        if self.shell is not None and self.shell.poll() is None:
            return self.shell
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env.setdefault("LANG", "C.UTF-8")
        remote = (f"stty rows {rows} cols {cols} 2>/dev/null; "
                  "echo LAB_TTY=$(tty); "
                  "export HISTFILE=\"$HOME/.bash_history\"; "
                  "export PROMPT_COMMAND='history -a'; "
                  "printf '\\033[2J\\033[H'; exec bash -l")
        cmd = [SSH_EXE, "-p", str(self.port), "-i", KEY_PATH,
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
               "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=8",
               "-o", "PreferredAuthentications=publickey",
               "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=6",
               "-tt", "student@127.0.0.1", remote]
        self.shell = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, bufsize=0,
                                      creationflags=0x08000000, env=env)
        return self.shell

    def set_winsize(self, rows, cols, timeout=8):
        """在不打扰交互 shell 的前提下修改它的 PTY 尺寸（另开 SSH 执行通道）。"""
        if not self.tty:
            return False
        cmd = f"stty -F {shlex_quote(self.tty)} rows {int(rows)} cols {int(cols)}"
        code, _ = ssh_run(self.port, cmd, timeout=timeout)
        return code == 0

    def read_tty(self, timeout=8.0):
        """从 shell 输出的第一行解析 pts 编号。"""
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline and b"\n" not in buf:
            chunk = self.shell.stdout.read(1)
            if not chunk:
                break
            buf += chunk
        text = buf.decode("utf-8", "replace")
        match = re.search(r"LAB_TTY=(/\S+)", text)
        if match:
            self.tty = match.group(1)
        return text


POOL_INFO = {"provider": "qemu", "qemu": None, "golden": GOLDEN}
