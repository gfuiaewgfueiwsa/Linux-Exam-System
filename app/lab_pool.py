# -*- coding: utf-8 -*-
"""lab_pool —— 真实 Linux 虚拟机池（每个会话一台独立 Alpine 虚拟机）。

两类资源分开管理：
* 练习池：从金镜像 snapshot 启动，固定编号 01..N，空闲时复用。
* 考试池：开考前预热若干台“槽位虚拟机”，每台使用独立持久镜像
  （pw-01.qcow2 …），学生进入考试时直接接管一台已启动的虚拟机，
  把启动等待从数十秒降到接近 0。槽位与 sid 的映射写入 exam_slots.json，
  服务器重启后仍能找回学生原来的磁盘状态。
"""

from __future__ import annotations

import json
import os
import threading
import time

from lab_qemu import (GOLDEN, LAB_DIR, QemuVM, assign_slot, delete_session_image,
                      ensure_key, ensure_session_image, ensure_slot_image,
                      find_qemu, provision_golden, purge_unassigned_slot_images,
                      slot_for_session, slot_image_path, slots_in_use)

CONFIG_PATH = os.path.join(LAB_DIR, "lab.json")
DEFAULT = {
    "max_vms": 16,           # 练习虚拟机上限
    "idle_seconds": 1800,
    "memory_mb": 256,
    "prewarm_vms": 2,        # 练习池预热台数
    "exam_prewarm": 14,      # 开考前预热的考试虚拟机台数
    "exam_capacity": 16,     # 考试虚拟机并发上限
    "boot_concurrency": 4,   # 同时启动的虚拟机数量
    "image_concurrency": 2,  # 同时复制磁盘镜像的数量
    "smp": 2,                # 每台虚拟机 vCPU 数
}


def load_conf():
    conf = dict(DEFAULT)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            conf.update({k: v for k, v in json.load(fh).items() if k in DEFAULT})
    except Exception:
        pass
    return conf


class LabPool:
    def __init__(self):
        self.conf = load_conf()
        self.max = max(1, int(self.conf["max_vms"]))
        self.idle_seconds = max(300, int(self.conf["idle_seconds"]))
        self.memory = max(192, int(self.conf["memory_mb"]))
        self.prewarm_n = max(0, min(self.max, int(self.conf["prewarm_vms"])))
        self.exam_prewarm_n = max(0, int(self.conf["exam_prewarm"]))
        self.exam_capacity = max(1, int(self.conf["exam_capacity"]))
        self.smp = max(1, int(self.conf["smp"]))
        self.boot_sem = threading.BoundedSemaphore(max(1, int(self.conf["boot_concurrency"])))
        self.image_sem = threading.BoundedSemaphore(max(1, int(self.conf["image_concurrency"])))

        self.vms = {}                            # key -> QemuVM（练习/考试共用索引）
        self.idle = []                           # 练习：空闲已就绪虚拟机
        self.starting = set()                    # 练习：正在启动中的编号
        self._refilling = False
        self._shutdown = False
        self._prewarm_started = False

        # 考试预热池
        self.exam_warm = []                      # 已启动、未分配的考试虚拟机
        self.exam_starting = set()               # 正在启动的槽位编号
        self.exam_booting = set()                # 正在按 sid 冷启动的 key
        self._exam_prewarm_running = False
        self._exam_prewarm_wanted = False
        self._exam_gen = 0                       # 代次：取消预热时递增

        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.ready = False
        self.error = ""
        threading.Thread(target=self._prepare, daemon=True).start()
        threading.Thread(target=self._janitor, daemon=True).start()

    # ------------------------------------------------------------ 准备
    def _prepare(self):
        try:
            ensure_key()
            if not find_qemu():
                self.error = ("未安装 QEMU。请把 qemu-w64-setup-*.exe 放到 "
                              "tools/qemu/ 后重启，或在教师机执行 "
                              "app/lab_qemu.py 的 install_qemu()。")
                return
            os.makedirs(LAB_DIR, exist_ok=True)
            # 上一次运行（尤其是直接关闭窗口）留下的、没有映射到任何在考会话的
            # 预热镜像都是脏盘，先清掉，否则下一场考试会把它交给新学生。
            n = purge_unassigned_slot_images()
            if n:
                print(f"[lab] 已清理 {n} 个无主的考试预热镜像")
            provision_golden()
            self.ready = True
            print("[lab] 真实 Linux 环境已就绪（QEMU/Alpine）")
        except Exception as exc:      # noqa: BLE001
            self.error = str(exc)
            print("[lab] 环境准备失败：" + self.error)

    def state(self):
        with self.lock:
            busy = {getattr(v, "label", "") for v in self.vms.values() if getattr(v, "label", "")}
            idle = {getattr(v, "label", "") for v in self.idle if v.alive()}
            nodes = []
            for i in range(1, self.prewarm_n + 1):
                label = f"{i:02d}"
                if label in busy:
                    st = "busy"
                elif label in idle:
                    st = "idle"
                elif label in self.starting:
                    st = "starting"
                else:
                    st = "pending"
                nodes.append({"id": label, "status": st})
            exam_warm = len([v for v in self.exam_warm if v.alive()])
            exam_active = len([k for k, v in self.vms.items()
                               if k.startswith("exam:") and v.alive()])
            return {
                "ready": self.ready, "error": self.error,
                "max_vms": self.max, "vms": len(self.vms),
                "idle": len(self.idle), "prewarm": self.prewarm_n,
                "nodes": nodes,
                "busy_labels": sorted(busy), "idle_labels": sorted(idle),
                "provider": "qemu", "lab_dir": LAB_DIR,
                "exam": {
                    "prewarm": self.exam_prewarm_n,
                    "warm": exam_warm,
                    "active": exam_active,
                    "starting": len(self.exam_starting),
                    "capacity": self.exam_capacity,
                    "accel": "whpx" if self._accel_hint() == "whpx" else "tcg",
                },
            }

    @staticmethod
    def _accel_hint():
        try:
            from lab_qemu import detect_accel
            return detect_accel()
        except Exception:
            return "tcg"

    # ------------------------------------------------------------ 练习预加载
    def prewarm(self):
        """隧道就绪后由启动脚本显式触发练习池预加载（先隧道、后虚拟机）。"""
        with self.lock:
            if self._prewarm_started:
                return
            self._prewarm_started = True
        threading.Thread(target=self._prewarm_when_ready, daemon=True).start()

    def _prewarm_when_ready(self):
        deadline = time.time() + 900
        while not self.ready and time.time() < deadline:
            if self.error:
                print("[lab] 预加载跳过：" + self.error)
                return
            time.sleep(1)
        if self.ready:
            self._prewarm()

    def _prewarm(self):
        for i in range(1, self.prewarm_n + 1):
            self._start_idle_vm(f"{i:02d}")
        print(f"[lab] 练习池预加载完成：{len(self.idle)}/{self.prewarm_n} 台就绪")

    def _start_idle_vm(self, label, attempts=3):
        if self._shutdown:
            return
        with self.cond:
            if label in self.starting:
                return
            self.starting.add(label)
            self.cond.notify_all()
        for attempt in range(1, attempts + 1):
            if self._shutdown:
                break
            vm = QemuVM("lab-" + label, memory=self.memory, smp=self.smp)
            vm.label = label
            try:
                with self.boot_sem:
                    vm.start()
                with self.cond:
                    self.idle.append(vm)
                    self.starting.discard(label)
                    self.cond.notify_all()
                print(f"[lab] 练习虚拟机 {label} 就绪（端口 {vm.port}）")
                return
            except Exception as exc:      # noqa: BLE001
                try:
                    vm.stop()
                except Exception:
                    pass
                if attempt < attempts:
                    print(f"[lab] 练习虚拟机 {label} 启动失败（第 {attempt}/{attempts} 次）：{exc}，{2 * attempt} 秒后重试")
                    time.sleep(2 * attempt)
                else:
                    print(f"[lab] 练习虚拟机 {label} 启动失败（已重试 {attempts} 次）：{exc}")
        with self.cond:
            self.starting.discard(label)
            self.cond.notify_all()

    def _refill(self):
        with self.cond:
            if self._shutdown or self._refilling or not self.ready or self.error:
                return
            self._refilling = True
        try:
            while True:
                with self.cond:
                    if self._shutdown or len(self.idle) >= self.prewarm_n:
                        break
                    busy = {getattr(v, "label", "") for v in self.vms.values()}
                    idle = {getattr(v, "label", "") for v in self.idle if v.alive()}
                    label = next((f"{i:02d}" for i in range(1, self.prewarm_n + 1)
                                  if f"{i:02d}" not in busy and f"{i:02d}" not in idle
                                  and f"{i:02d}" not in self.starting), None)
                if label is None:
                    break
                self._start_idle_vm(label)
        finally:
            with self.cond:
                self._refilling = False

    # ------------------------------------------------------------ 考试预热
    def prewarm_exam(self, count=None):
        """预热考试虚拟机（幂等）。教师设置/开始考试、启动脚本均可调用。"""
        with self.lock:
            if self._shutdown or not self.ready:
                self._exam_prewarm_wanted = True
                return
            if self._exam_prewarm_running:
                return
            self._exam_prewarm_running = True
            self._exam_prewarm_wanted = False
            gen = self._exam_gen
        target = min(self.exam_prewarm_n if count is None else int(count),
                     self.exam_capacity)
        threading.Thread(target=self._prewarm_exam_work, args=(gen, target),
                         daemon=True).start()

    def _prewarm_exam_work(self, gen, target):
        try:
            deadline = time.time() + 900
            while not self.ready and time.time() < deadline:
                if self.error or self._shutdown:
                    return
                time.sleep(1)
            if not self.ready:
                return
            print(f"[lab] 开始预热 {target} 台考试虚拟机…")
            # 先准备磁盘镜像（并发受限），避免 14 台同时复制造成磁盘风暴
            def make_image(i):
                with self.image_sem:
                    return ensure_slot_image(i)
            threads = []
            for i in range(1, target + 1):
                t = threading.Thread(target=make_image, args=(i,), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=300)
            if gen != self._exam_gen or self._shutdown:
                return
            # 再启动虚拟机：一次抛出全部启动任务，由 boot_sem 限制并发
            boot_threads = []
            for i in range(1, target + 1):
                t = threading.Thread(target=self._boot_exam_slot, args=(i, gen), daemon=True)
                t.start()
                boot_threads.append(t)
            for t in boot_threads:
                t.join(timeout=600)
        finally:
            with self.cond:
                self._exam_prewarm_running = False
                self.cond.notify_all()
            if not self._shutdown and self._exam_prewarm_wanted:
                self.prewarm_exam()

    def _boot_exam_slot(self, index, gen):
        if self._shutdown or gen != self._exam_gen:
            return
        if index in slots_in_use():
            # 该槽位已属于某个学生（服务器重启后重新接管），绝不能再启动一台
            # 共用同一磁盘镜像，否则两个 QEMU 进程会写坏文件系统。
            return
        with self.cond:
            if any(getattr(v, "slot", 0) == index for v in self.exam_warm):
                return
            if index in self.exam_starting:
                return
            self.exam_starting.add(index)
            self.cond.notify_all()
        vm = None
        try:
            for attempt in range(1, 4):
                if gen != self._exam_gen or self._shutdown:
                    return
                vm = None
                try:
                    with self.boot_sem:
                        if gen != self._exam_gen or self._shutdown:
                            return
                        vm = QemuVM(f"exam-slot-{index}", memory=self.memory,
                                    image=slot_image_path(index), smp=self.smp,
                                    label="E%02d" % index)
                        vm.slot = index
                        vm.start()
                    with self.cond:
                        if gen != self._exam_gen or self._shutdown:
                            need_stop = True
                        else:
                            need_stop = False
                            self.exam_warm.append(vm)
                            self.cond.notify_all()
                    if need_stop:
                        try:
                            vm.stop()
                        except Exception:
                            pass
                        return
                    print(f"[lab] 考试虚拟机 E{index:02d} 预热就绪（端口 {vm.port}）")
                    return
                except Exception as exc:      # noqa: BLE001
                    print(f"[lab] 考试虚拟机 E{index:02d} 预热失败"
                          f"（第 {attempt}/3 次）：{exc}")
                    if vm is not None:
                        try:
                            vm.stop()
                        except Exception:
                            pass
                    if attempt < 3:
                        time.sleep(2 * attempt)
        finally:
            with self.cond:
                self.exam_starting.discard(index)
                self.cond.notify_all()

    def stop_exam_prewarm(self):
        """停止尚未被分配的考试预热虚拟机，并取消正在进行的预热。

        关机放到后台并行执行：每台 QEMU 关机最多约 13 秒，14 台串行会让教师端
        “结束考试”请求等待两三分钟。这里立即返回，关机线程并行处理。
        """
        with self.cond:
            self._exam_gen += 1
            warm = list(self.exam_warm)
            self.exam_warm.clear()
            self.cond.notify_all()

        def _stop_all(vms):
            threads = []
            for vm in vms:
                def _one(target=vm):
                    try:
                        target.stop()
                    except Exception:
                        pass
                t = threading.Thread(target=_one, daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=20)
            if vms:
                print(f"[lab] 已停止 {len(vms)} 台空闲考试预热虚拟机")
            # 必须等 QEMU 真正退出后再删镜像，否则会删掉正在使用的磁盘文件。
            try:
                n = purge_unassigned_slot_images()
            except Exception:
                n = 0
            if n:
                print(f"[lab] 已清理 {n} 个考试预热镜像，下一场重新从金镜像复制")

        threading.Thread(target=_stop_all, args=(warm,), daemon=True).start()

    # ------------------------------------------------------------ 生命周期
    def acquire(self, key, timeout=420):
        with self.cond:
            vm = self.vms.get(key)
            if vm is not None and vm.alive():
                vm.last_used = time.time()
                return vm
        if not self.ready:
            deadline = time.time() + timeout
            while not self.ready and time.time() < deadline:
                if self.error:
                    raise RuntimeError(self.error)
                time.sleep(0.5)
            if not self.ready:
                raise RuntimeError(self.error or "Linux 环境尚未就绪")

        if key.startswith("exam:"):
            return self._acquire_exam(key, timeout)

        with self.cond:
            vm = self.vms.get(key)
            if vm is not None and vm.alive():
                return vm
            while self.idle:
                vm = self.idle.pop(0)
                if vm.alive():
                    vm.sid = key
                    vm.last_used = time.time()
                    self.vms[key] = vm
                    self.cond.notify_all()
                    return vm
            deadline = time.time() + timeout
            while len([v for k, v in self.vms.items()
                       if not k.startswith("exam:") and v.alive()]) >= self.max:
                remain = deadline - time.time()
                if remain <= 0:
                    raise RuntimeError("Linux 虚拟机已满，请稍后重试")
                self.cond.wait(min(2.0, remain))
            vm = QemuVM(key, memory=self.memory, smp=self.smp)
            vm.last_used = time.time()
            self.vms[key] = vm
        try:
            with self.boot_sem:
                vm.start()
        except Exception:
            with self.cond:
                self.vms.pop(key, None)
                self.cond.notify_all()
            raise
        with self.cond:
            self.cond.notify_all()
        return vm

    def _acquire_exam(self, key, timeout):
        """考试会话分配：优先接管预热虚拟机，其次按上限冷启动。"""
        sid = key[len("exam:"):]
        deadline = time.time() + timeout
        while True:
            with self.cond:
                vm = self.vms.get(key)
                if vm is not None and vm.alive():
                    vm.last_used = time.time()
                    self.cond.notify_all()
                    return vm
                # 学生已有持久镜像（服务器重启/重连）：必须沿用原磁盘，不能换新槽位
                slot = slot_for_session(sid)
                if slot and not os.path.isfile(slot_image_path(slot)):
                    slot = None
                if not slot:
                    while self.exam_warm:
                        warm = self.exam_warm.pop(0)
                        if not warm.alive():
                            continue
                        warm.sid = key
                        warm.last_used = time.time()
                        self.vms[key] = warm
                        assign_slot(sid, warm.slot)
                        self.cond.notify_all()
                        print(f"[lab] 会话 {sid} 接管预热虚拟机 {warm.label}")
                        return warm
                active = len([k for k, v in self.vms.items()
                              if k.startswith("exam:") and v.alive()])
                if (active + len(self.exam_booting)
                        + (len(self.exam_starting) if not slot else 0)) >= self.exam_capacity:
                    remain = deadline - time.time()
                    if remain <= 0:
                        raise RuntimeError("考试虚拟机已满，请稍后重试")
                    self.cond.wait(min(2.0, remain))
                    continue
                self.exam_booting.add(key)
            # 锁外准备镜像并冷启动（仍受启动并发限制）
            vm = None
            try:
                with self.image_sem:
                    image = ensure_session_image(sid)
                vm = QemuVM(key, memory=self.memory, image=image, smp=self.smp)
                vm.last_used = time.time()
                with self.cond:
                    existing = self.vms.get(key)
                    if existing is not None and existing.alive():
                        return existing
                    self.vms[key] = vm
                    self.cond.notify_all()
                with self.boot_sem:
                    vm.start()
                with self.cond:
                    self.cond.notify_all()
                return vm
            except Exception:
                if vm is not None:
                    with self.cond:
                        if self.vms.get(key) is vm:
                            self.vms.pop(key, None)
                        self.cond.notify_all()
                raise
            finally:
                with self.cond:
                    self.exam_booting.discard(key)
                    self.cond.notify_all()

    def get(self, key):
        with self.lock:
            return self.vms.get(key)

    def touch(self, key):
        vm = self.get(key)
        if vm is not None:
            vm.last_used = time.time()

    def release(self, key, reset=False):
        with self.cond:
            vm = self.vms.pop(key, None)
            self.cond.notify_all()
        if key.startswith("exam:"):
            # 考试会话：交卷/清除记录时关闭虚拟机并回收持久磁盘。
            # vm 为 None 也必须回收磁盘：虚拟机可能已被看护线程清理或压根没
            # 启动成功，此前直接 return 会把学生镜像和 exam_slots.json 里的
            # 槽位映射永久留在那里，那个槽位从此再也不会被预热使用。
            sid = key[len("exam:"):]
            if vm is not None:
                vm.detach()      # 仅断开 SSH 连接
                try:
                    vm.stop()
                except Exception:
                    pass
            if reset:
                delete_session_image(sid)
            return
        if vm is None:
            return
        vm.detach()          # 仅断开 SSH 连接，不关闭虚拟机
        if not vm.alive():
            threading.Thread(target=self._refill, daemon=True).start()
            return
        if vm.label:
            if reset:
                label = vm.label
                vm.stop()
                threading.Thread(target=self._start_idle_vm, args=(label,), daemon=True).start()
            else:
                with self.cond:
                    self.idle.append(vm)
                    self.cond.notify_all()
        else:
            vm.stop()
            threading.Thread(target=self._refill, daemon=True).start()

    # ------------------------------------------------------------ 回收
    def _janitor(self):
        while True:
            time.sleep(60)
            dead = []
            need_refill = False
            with self.lock:
                for key, vm in list(self.vms.items()):
                    if not vm.alive():
                        dead.append(key)
                if any(not vm.alive() for vm in self.idle):
                    self.idle = [v for v in self.idle if v.alive()]
                    need_refill = True
            for key in dead:
                print(f"[lab] 清理已崩溃虚拟机 {key}")
                self.release(key)
                if not key.startswith("exam:"):
                    need_refill = True
            with self.cond:
                self.exam_warm = [v for v in self.exam_warm if v.alive()]
            if need_refill and not self._shutdown:
                threading.Thread(target=self._refill, daemon=True).start()

    def stop_all(self):
        self._shutdown = True
        with self.cond:
            self._exam_gen += 1
            idle = list(self.idle)
            self.idle.clear()
            warm = list(self.exam_warm)
            self.exam_warm.clear()
            vms = list(self.vms.values())
            self.vms.clear()
            self.cond.notify_all()
        for vm in idle + warm + vms:
            try:
                vm.stop()
            except Exception:
                pass


POOL = LabPool()


def status_snapshot():
    return POOL.state()
