# 部署与调试手册

> 本文件汇总本项目**完整版**与**考试系统简化版**的全部安装、启动、配置、测试与调试命令。
> 系统功能说明见 [README.md](./README.md)，简化版说明见 [考试系统简化版/README.md](./考试系统简化版/README.md)。

---

## 0. 仓库内容说明（必读）

本仓库是**公开仓库**，只收录源码、前端页面与文档。以下内容出于隐私、体积或许可原因**不随仓库分发**，克隆后需要自行准备：

| 未随仓库分发 | 原因 | 获取 / 生成方式 |
| --- | --- | --- |
| `.workbuddy/` | AI 助手私有记忆 | 不需要，本地自动生成 |
| `data/`、`考试系统简化版/data/` | 成绩数据库、考生姓名、考场配置、虚拟机镜像 | **首次启动自动创建**，`config.json` 默认值见 §5 |
| `tools/qemu/`、`tools/cloudflared.exe` | 第三方二进制发行版，约 800 MB | 见 §2，自行下载后放入 `tools/` |
| 答案类文档（题目答案、答案速查、课件文本） | 公开会泄露考题 | 教师本地自行保管 |
| `backup/` | 旧版本源码备份 | 不需要 |
| `公网地址.txt` | 运行时临时隧道域名 | 启动后自动生成（一次 3 条） |
| `__pycache__/`、`*.pyc`、`*.log` | 编译缓存与日志 | 运行自动生成 |

> 仓库尚未附带 `LICENSE`。若要允许他人二次分发或商用，请自行补充许可证文件。

---

## 1. 环境要求

| 项目 | 完整版 | 简化版 |
| --- | --- | --- |
| Python | 3.10+（仅标准库） | 3.10+（仅标准库） |
| 操作系统 | Windows 10/11 x64 | Windows / macOS / Linux 均可 |
| 管理员权限 | 不需要 | 不需要 |
| 外部依赖 | QEMU + Alpine 云镜像；公网访问另需 `cloudflared.exe` | 公网访问另需 `cloudflared.exe` |
| Docker / WSL / Hyper-V | 不需要 | 不需要 |
| 磁盘 | 基础约 1.1 GB，按 14 台预热建议预留 5 GB | 约 2 MB |
| 端口 | 8000 / 8001 / 22300–22900（SSH 转发，仅本机） | 8100 / 8101 |

两个版本都**不需要 `pip install`**，全部依赖 Python 标准库。

---

## 2. 第三方依赖准备（仅完整版 / 公网访问需要）

### 2.1 简化版：零依赖即可本机跑

```powershell
cd "考试系统简化版"
python start_all.py --local
```

不需要 QEMU，不需要 `cloudflared`，直接打开 `http://localhost:8100`。

### 2.2 完整版：QEMU

完整版的学生终端跑在真实 Linux 虚拟机里，需要 QEMU for Windows：

```powershell
# 1) 从 https://qemu.weilnetz.de/w64/ 下载 64 位安装包
# 2) 安装后把整个安装目录（含 bin\qemu-system-x86_64.exe 及 dll）
#    放到项目 tools/qemu/ 下，例如：
#       tools/qemu/bin/qemu-system-x86_64.exe
# 程序会自动搜索 tools/qemu/ 与 tools/qemu/bin/
```

首次启动还会自动下载 Alpine 云镜像（约 150 MB）并预置金镜像（约 2 分钟），
生成 `data/lab/images/golden.qcow2`；之后每台考试机从金镜像复制，约 40 秒启动。

### 2.3 公网访问：cloudflared（可选）

```powershell
# 从 https://github.com/cloudflare/cloudflared/releases/latest
# 下载 cloudflared-windows-amd64.exe，改名为 cloudflared.exe，放到 tools/
#   项目根 tools/cloudflared.exe        （完整版）
#   也可由简化版回退复用主项目 tools/  （见简化版 README）
```

不开公网时一律加 `--local`，不会启动隧道。Cloudflare Quick Tunnel 每次重启都会换域名，
3 条备用地址会打印在启动窗口并写入 `公网地址.txt`。

---

## 3. 启动命令

### 3.1 完整版（项目根目录）

```powershell
cd "D:\KSXT"
双击「启动考试系统.bat」        # 等价 python start_all.py
python start_all.py             # 学生端 + 教师端 + 3 条公网隧道
python start_all.py --local     # 只开本机，不开隧道（推荐先这样自测）
```

### 3.2 简化版（子目录）

```powershell
cd "D:\KSXT\考试系统简化版"
双击「启动简化版.bat」
python start_all.py             # 学生端 + 教师端 + 3 条公网隧道
python start_all.py --local     # 只开本机，不开隧道
python app/server.py            # 只起学生端后端，默认 8100
python app/server.py 8100       # 指定学生端端口
```

### 3.3 停止服务

直接关闭启动窗口即可。`start_all.py` 用 Windows Job Object 托管子进程，
主进程被结束时会一并终止 `cloudflared` / QEMU / SSH，不会残留进程占端口。

### 3.4 克隆仓库

```powershell
gh repo clone gfuiaewgfueiwsa/Linux实训考核系统
# 或
git clone https://github.com/gfuiaewgfueiwsa/Linux实训考核系统.git
```

---

## 4. 端口与访问地址

| 版本 | 入口 | 地址 | 说明 |
| --- | --- | --- | --- |
| 完整版 | 学生端（本机） | `http://localhost:8000` | 自测用 |
| 完整版 | 教师控制台 | `http://localhost:8001` | **仅监听 127.0.0.1**，免登录，公网不可访问 |
| 完整版 | 学生端（公网） | 启动后 `公网地址.txt` | 3 条 `https://xxx.trycloudflare.com` |
| 简化版 | 学生端（本机） | `http://localhost:8100` | `/teacher` 明确返回 404 |
| 简化版 | 教师控制台 | `http://localhost:8101` | 仅本机，免登录 |
| 简化版 | 学生端（公网） | 启动后 `公网地址.txt` | 同上，只转发学生端 |

**教师端绝不进公网隧道**，学生端不实现任何 `/api/admin/*` 路由（404）。

---

## 5. 配置文件

首次启动自动创建 `data/` 与 `config.json`，可直接改文件，也可用 §6 的接口改。

### 5.1 考场配置 `data/config.json`（两个版本同结构）

```json
{
  "exam_title": "Linux 基础命令 · 简化版实操考核",
  "exam_start": "2026-11-11T10:41",
  "exam_minutes": 45,
  "exam_closed": false
}
```

| 字段 | 含义 |
| --- | --- |
| `exam_title` | 页面标题 |
| `exam_start` | 开考时间，`YYYY-MM-DDTHH:MM`（本地时间） |
| `exam_minutes` | 考试时长 1–600 分钟 |
| `exam_closed` | `true` = 强制关闭闸门（不论时间） |

闸门状态：`not_set`（未排期）/ `before`（未开考）/ `open`（进行中）/ `ended`（已结束）/ `closed`（教师强制关闭）。
只有 `open` 才能入场和答题。

### 5.2 虚拟机池 `data/lab/lab.json`（仅完整版）

```json
{
  "max_vms": 16, "idle_seconds": 1800, "memory_mb": 256,
  "prewarm_vms": 2, "exam_prewarm": 14, "exam_capacity": 16,
  "boot_concurrency": 4, "image_concurrency": 2, "smp": 2
}
```

教室机器性能不足时优先调小 `exam_prewarm` / `exam_capacity`。
`data/lab/exam_slots.json` 是姓名 → 虚拟机槽位的运行时映射，由程序维护，不要手改。

---

## 6. 教师端常用操作（curl）

下列命令在 **Git Bash / macOS / Linux** 下可直接执行。
在 **PowerShell** 中请用 `curl.exe`（`curl` 是 `Invoke-WebRequest` 别名），
JSON 里的双引号要写成 `""`，例如 `--data-raw "{""action"":""now"",""minutes"":45}"`。

```bash
# 查看当前配置与闸门
curl -s http://127.0.0.1:8101/api/admin/config

# 立即开考，时长 45 分钟（完整版把端口换成 8001）
curl -s -X POST http://127.0.0.1:8101/api/admin/config \
  -H "Content-Type: application/json" \
  --data-raw '{"action":"now","minutes":45}'

# 指定开考时间
curl -s -X POST http://127.0.0.1:8101/api/admin/config \
  -H "Content-Type: application/json" \
  --data-raw '{"action":"save","start":"2026-11-11T14:00","minutes":40}'

# 强制关闭闸门（会同时把在线考生全部交卷）
curl -s -X POST http://127.0.0.1:8101/api/admin/config \
  -H "Content-Type: application/json" --data-raw '{"action":"close"}'

# 统计 / 进行中 / 成绩动态 / 日志
curl -s http://127.0.0.1:8101/api/admin/stats
curl -s http://127.0.0.1:8101/api/admin/live
curl -s http://127.0.0.1:8101/api/admin/progress
curl -s http://127.0.0.1:8101/api/admin/logs
curl -s http://127.0.0.1:8101/api/admin/submissions

# 导出成绩（CSV，UTF-8 BOM，Excel 可直接打开）
curl -s -o 成绩.csv        http://127.0.0.1:8101/api/admin/export.csv
curl -s -o 成绩表.csv      http://127.0.0.1:8101/api/admin/export_matrix.csv

# 删除单条记录 / 清空全部记录（谨慎，不可恢复）
curl -s -X POST http://127.0.0.1:8101/api/admin/record/delete \
  -H "Content-Type: application/json" --data-raw '{"sid":"<sid>"}'
curl -s -X POST http://127.0.0.1:8101/api/admin/records/clear \
  -H "Content-Type: application/json" --data-raw '{}'
```

学生端常用只读接口：

```bash
curl -s http://127.0.0.1:8100/api/health          # 版本、题数、闸门
curl -s http://127.0.0.1:8100/api/exam/gate       # 闸门详情
curl -s "http://127.0.0.1:8100/api/result?sid=<sid>"   # 成绩单
```

---

## 7. 测试命令

```powershell
# ---------- 简化版 ----------
cd "D:\KSXT\考试系统简化版"
python tests/selfcheck.py      # 进程内判分自检，不开端口（259 项断言）
python tests/e2e_http.py       # 真起 HTTP 端到端（282 项断言）

# 测试端口被占用时改端口
$env:E2E_STUDENT_PORT="18199"; $env:TEACHER_PORT="18101"; python tests/e2e_http.py

# ---------- 完整版 ----------
cd "D:\KSXT"
$env:PYTHONUTF8="1"
python tests/api_smoke.py      # 83 项：接口隔离 / 闸门 / 判分 / 恢复 / 磁盘回收
python tests/ui_server.py --before 900   # 开考前 15 分钟，验证卡片禁用与自动解锁
python tests/ui_server.py --open         # 闸门已开，验证入场 / 刷新恢复 / 交卷
```

测试隔离说明：

- `tests/api_smoke.py` 默认在 8010 / 8011 起服务，用 `tempfile.mkdtemp()`，
  虚拟机池与金镜像全部替换为桩，**不启动 QEMU、不改动真实 `data/`**。
  可用 `SMOKE_STUDENT_PORT` / `SMOKE_TEACHER_PORT` 改端口。
- `tests/e2e_http.py` 默认在 8199 / 8101 起服务，用独立目录 `data/e2e_tmp/`，
  **不污染 `data/exam.db`**。跑真实考试前先关掉测试进程，避免 8101 冲突。
- 退出码 `0` 表示全部断言通过。

---

## 8. 调试命令

```powershell
# 统一 UTF-8，避免中文题面/姓名乱码（两个 .bat 已内置）
$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"

# 语法检查（不启动服务）
python -m py_compile app/*.py start_all.py
python -m py_compile 考试系统简化版/app/*.py 考试系统简化版/tests/*.py

# 检查端口占用
netstat -ano -p tcp | findstr ":8100 :8101 :8000 :8001"
Get-NetTCPConnection -LocalPort 8100 -ErrorAction SilentlyContinue   # PowerShell

# 找出并结束残留进程
tasklist | findstr /i "python cloudflared qemu"
taskkill /PID <PID> /F

# 查看健康与闸门
curl.exe -s http://127.0.0.1:8100/api/health
curl.exe -s http://127.0.0.1:8101/api/admin/config

# 手动走一遍学生流程（简化版，Git Bash 下）
curl -s -X POST http://127.0.0.1:8100/api/exam/start \
  -H "Content-Type: application/json" --data-raw '{"name":"调试考生","student_no":""}'
curl -s -X POST http://127.0.0.1:8100/api/exam/run \
  -H "Content-Type: application/json" \
  --data-raw '{"sid":"<上一步返回的 sid>","line":"whoami"}'

# 直接查 SQLite（无需额外工具）
python -c "import sqlite3;c=sqlite3.connect('考试系统简化版/data/exam.db');print(c.execute('select name,score,passed,finished from submissions').fetchall())"
```

前端调试：两个页面都是单文件 HTML（内联 CSS/JS），响应带 `no-store`，
**改完 HTML 直接刷新浏览器即可**，不必重启服务；改后端 `.py` 必须重启。

---

## 9. 数据重置与备份

```powershell
# 只清成绩，保留配置（先关服务）
Remove-Item "考试系统简化版\data\exam.db"        # 下次启动自动重建空库
Remove-Item "data\exam.db"

# 完整版：重建学生机金镜像（约 2 分钟，先关服务）
Remove-Item "data\lab\images\golden.qcow2"

# 清空所有考试虚拟机会话快照（每台约 200 MB，可从金镜像重新分配）
Remove-Item "data\lab\images\sessions\*.qcow2"

# 备份：直接复制整个 data/ 目录即可，无需停库特殊处理
Copy-Item -Recurse "data" "data_backup_20261111"
```

> 操作前务必确认服务已完全退出，否则 Windows 下文件被占用会删除失败或产生损坏镜像。

---

## 10. 常见问题

| 现象 | 处理 |
| --- | --- |
| 进不去 / 提示「考试未开放」 | `curl .../api/admin/config` 看闸门；用 §6 的 `action=now` 重开一场 |
| 第一次启动很慢 | 完整版首次要下载 Alpine 并预置金镜像（约 2 分钟），之后每台约 40 秒 |
| 提示「找不到 QEMU」 | 确认 `tools/qemu/bin/qemu-system-x86_64.exe` 存在，见 §2.2 |
| 公网地址打不开 | Quick Tunnel 是临时域名，重启即换；`公网地址.txt` 有 3 条，逐条试 |
| 中文乱码 | 设 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`，或用 `.bat` 启动 |
| 8101 端口被占用 / 测试报错 | 上一场 `e2e_http.py` 未退出；`taskkill` 掉残留 `python.exe` 或改端口 |
| 教师端从公网打不开 | 设计如此，教师端只监听 `127.0.0.1`，请在服务器本机操作 |
| 改了 HTML 没生效 | 硬刷新 `Ctrl+F5`；改了 `.py` 请重启服务 |

---

## 11. 安全提示

1. **教师控制台免登录**，只监听 `127.0.0.1`。不要为了远程管理把它改成 `0.0.0.0` 或塞进公网隧道。
2. **考试答案文件不要放进公开仓库**（已在 `.gitignore` 排除）；学生一旦搜到，考核即失效。
3. `data/exam.db` 含考生姓名与成绩，属于个人信息，不要提交或公开。
4. 公网隧道只转发学生端端口；开考前请核对 `公网地址.txt` 与闸门时间。
