# 任务A：考试端断连重连后状态/历史丢失的解决方案

> 版本：v3.0.30 配套文档
> 状态：已实现，待实测验证

## 1. 问题现象

考试学生端长时间不操作后断连，重新连接后「没有之前系统的历史记录」，无法继续推进命令。

## 2. 根因分析

本系统考试环境用 QEMU 启动 Alpine Linux 真实虚拟机，原本以 `snapshot` 模式运行：

```text
snapshot=True  →  QEMU 关机即丢弃磁盘 overlay，所有文件系统改动全部丢失
```

由此产生两类状态丢失：

| 丢失对象 | 触发场景 | 原处理方式 | 问题 |
|---|---|---|---|
| 命令历史 `~/.bash_history` | 浏览器断连重连 | `QemuVM.export_history()` / `import_history()` | 依赖 VM 仍存活；服务器重启或 VM 崩溃即失效 |
| 用户/目录/文件等磁盘改动 | 服务器重启、VM 崩溃 | 无 | snapshot 模式磁盘 overlay 被丢弃，无法恢复 |

**核心结论**：断连本身不销毁 VM（`_serve_term` 的 `finally` 中考试模式只 `export_history()`、不 `release()`），真正的状态丢失发生在**服务器重启或 VM 崩溃**时——snapshot 模式导致磁盘改动被丢弃，仅靠 `~/.bash_history` 文本恢复不够完整。

## 3. 解决方案：考试会话改用持久磁盘镜像

核心思路：每个考试会话不再共用 `snapshot=True` 的金镜像，而是在首次进入考试时**从金镜像整盘复制一份独立 qcow2**，以 `snapshot=False` 启动，使文件系统改动真正落盘。

### 3.1 约束条件

- 环境只有 `qemu-system-x86_64.exe`，**无 `qemu-img`**，因此无法用 `qemu-img create -b` 创建 backing overlay 快照链。
- 退而求其次：用 `shutil.copyfile` 整盘复制金镜像。

### 3.2 关键代码改动

#### lab_qemu.py

```python
SESSION_IMG_DIR = os.path.join(IMG_DIR, "sessions")

def session_image_path(sid):
    return os.path.join(SESSION_IMG_DIR, f"{sid}.qcow2")

def ensure_session_image(sid):
    # 首次进入考试：从金镜像整盘复制独立 qcow2
    os.makedirs(SESSION_IMG_DIR, exist_ok=True)
    part = session_image_path(sid)
    if not os.path.exists(part):
        shutil.copyfile(GOLDEN, part)   # 整盘复制，与金镜像解耦
    return part

def delete_session_image(sid):
    # 交卷/释放时删除独立镜像
    p = session_image_path(sid)
    if os.path.exists(p):
        os.remove(p)
```

`QemuVM.__init__` 新增 `image` 参数，`start()` 据此分支：

```python
def start(self):
    if self.image:
        qemu_cmd(self.image, ..., snapshot=False)   # 持久镜像，改动落盘
    else:
        qemu_cmd(GOLDEN, ..., snapshot=True)        # 练习仍用快照，不落盘
```

`qemu_cmd` 已支持按 `snapshot` 参数拼接 `-drive` 的 `snapshot=on/off`。

#### lab_pool.py

```python
# acquire() 的 exam 分支：锁外执行复制，双重检查避免并发重复创建
if key.startswith("exam:"):
    sid = key[len("exam:"):]
    image = ensure_session_image(sid)
    vm = QemuVM(key, memory=self.memory, image=image)

# release() 在释放 exam 会话时删除持久镜像
if reset and key.startswith("exam:"):
    delete_session_image(key[len("exam:"):])
```

### 3.3 效果

- 断连重连：VM 存活时沿用原有 `export/import_history` 恢复 `~/.bash_history`。
- 服务器重启/VM 崩溃后重连：磁盘改动已在持久镜像中落盘，重连后按 sid 找回独立镜像继续，不再「没有历史记录」。

## 4. 验证清单（需实测）

1. **整盘复制耗时**：金镜像体积决定首次进入考试的等待时长。若过大，需评估改用预创建 sparse 镜像或其他压缩方案。
2. **正常重启落盘**：服务器正常重启走 poweroff 路径，`cache=writeback` 下最后几次写操作应完整落盘。
3. **强杀进程风险**：服务器被强制 kill（非 poweroff）时，`cache=writeback` 可能丢最后几次写操作。必要时可改用 `cache=writethrough` 换取更强的落盘一致性（代价是 I/O 变慢）。
4. **磁盘占用**：每名学生一份独立镜像，交卷后需确认 `delete_session_image` 及时回收磁盘空间。

## 5. 风险与后续优化方向

- **磁盘膨胀**：整盘复制方案按学生数量线性增长磁盘占用。优化方向：接入 `qemu-img` 后改回 `qcow2 backing file` 方案（只存差异 overlay）。
- **复制阻塞**：`ensure_session_image` 是同步 I/O，首次进入考试会阻塞请求。若并发高，可改为后台预复制 + 前端加载态。
- **落盘一致性**：如出现交卷后改动丢失，优先排查 `cache` 模式，改 `writethrough` 验证。
