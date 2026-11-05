# WorkBuddy 跨设备双向同步套件

让两台（或多台）电脑上的 [WorkBuddy](https://www.workbuddy.cn/) 保持任务、对话、记忆、脚本、工作区文件全部一致——以一台 NAS（或任何 SMB 共享）为中转站，**无需任何云服务**，全程局域网内完成。

> 适用于：在家用机和办公机之间切换使用 WorkBuddy、希望两边看到完全相同的任务列表和对话记录、且不想把数据交给第三方云盘的用户。

## 它同步什么

| 数据类型 | 位置 | 同步方式 |
|---|---|---|
| 会话列表（/resume 可见） | `workbuddy.db` 的 `sessions` 等表 | **记录级合并**（按 `id`，`updated_at` 新者胜，不整库覆盖） |
| 对话正文 | `~/.workbuddy/projects/*.jsonl` | 单文件双向，冲突保新 + `.conflict` 备份 |
| 任务清单 | `~/.workbuddy/tasks/` | 目录级加性合并 |
| 项目记忆 / 日志 | 工作区 `.workbuddy/memory/` | 日志按行去重合并；MEMORY 按段落合并 |
| 用户技能 | `~/.workbuddy/skills/` | 按子目录加性合并 |
| 同步脚本自身 | `~/WorkBuddy/scripts/` | 脚本也会同步，两台自动保持同版本 |
| 工作区文件 | `~/WorkBuddy/<会话目录>/` | 加性合并（**只增不删**）+ 墓碑删除传播 |
| 任务报告 | `~/WorkBuddy/TaskReports/` | 每次运行重新生成 HTML |

核心安全机制：

- **加性合并（additive merge）**——两边谁缺补谁，绝不自动删除。
- **墓碑删除传播（tombstone）**——你主动删掉的文件会记入 NAS 上的墓碑表，下次任一台同步时把另一边的副本移入 `.trash/`（可恢复），防止"删了又复活"。忽略清单内的路径不产生墓碑。
- **冲突三保险**——双方都改过的文件保留较新版本，旧方存为 `.conflict-<时间戳>` 备份；`*.conflict-*` 默认在忽略清单中，不会无限增殖。
- **机器守卫**——通过 `COMPUTERNAME` 识别当前机器（绝不靠端口/文件猜测），每台机器的同步状态独立存档（`sync_state.<机器名>.json`），互不踩踏。
- **并发锁**——NAS 上的 `.sync-lock` 防止两台同时跑同步互相破坏（10 分钟未释放视为死锁自动接管）。

## 环境要求

- Windows 10/11，两台（或多台）电脑各装 WorkBuddy 桌面版
- 一台 NAS 或任何可同时被两台电脑访问的 SMB 共享（群晖/威联通/极空间/飞牛/OpenMediaVault/Windows 共享文件夹均可）
- 无需单独安装 Python：脚本优先使用 WorkBuddy 自带的 Python（`%USERPROFILE%\.workbuddy\binaries\python\...`），找不到时自动回退系统 `python`

## 快速开始

### 第 1 步：配置 NAS 路径（两台电脑都做）

```bat
cd %USERPROFILE%\WorkBuddy\scripts
copy sync-config.example.txt sync-config.txt
notepad sync-config.txt
```

把 `NAS_ROOT=` 后面改成你的共享路径，例如：

```
NAS_ROOT=\\192.168.1.100\share\workbuddy
```

> 首次运行会自动在 NAS 上创建整套目录结构（`workspace-files/`、`projects/`、`tasks/`、`scripts/`、`.trash/` 等），无需手工准备。

### 第 2 步：登记两台机器的名字

编辑 `scripts/sync-bidirectional.py` 顶部的 `KNOWN_MACHINES`，在每台电脑上执行 `echo %COMPUTERNAME%` 查到本机名后填入：

```python
KNOWN_MACHINES = {
    "OFFICE-PC": "办公电脑 (office)",
    "HOME-PC": "家里电脑 (home)",
}
```

### 第 3 步：一键同步

双击运行：

```bat
scripts\sync-all.bat
```

它会依次执行三步：

1. **sync-bidirectional.py** —— 双向同步所有文件（带 ignore 清单、墓碑、并发锁）
2. **sessions-merge.py** —— 把会话索引合并进本机 `workbuddy.db`（重启 WorkBuddy 后 /resume 两边的任务都可见）
3. **tasks-report.py** —— 重新生成全局任务报告 HTML

完成后重启 WorkBuddy 即可看到合并结果。

### 日常使用节奏（重要）

```
到一台电脑开始工作前 → 先跑一次 sync-all.bat
离开这台电脑前      → 再跑一次 sync-all.bat
```

串行使用（一次只用一台、切换前先同步）是**最安全**的模式。两台电脑在未同步状态下同时写同一个会话的对话，会触发冲突覆盖（旧内容进 `.conflict` 备份，可手动找回，但不会自动合并）——详见 [docs/SYNC-RISK-ANALYSIS.md](docs/SYNC-RISK-ANALYSIS.md)。

## 各文件说明

```
scripts/
├── sync-all.bat             # 一键入口：同步 + 合并 + 报告
├── sync-bidirectional.py    # 核心：双向文件同步（加性合并/墓碑/锁/机器守卫）
├── sessions-merge.py        # 会话索引合并进 workbuddy.db（记录级，非整库覆盖）
├── tasks-report.py          # 生成全局任务报告 HTML
├── undelete.py              # 从 .trash/ 回收站找回被墓碑删除的文件
├── sync_config.py           # 读取 sync-config.txt 的 NAS 路径
├── sync-config.example.txt  # 配置模板（复制为 sync-config.txt 后编辑）
└── sync-ignore.txt          # 排除清单（类似 .gitignore，支持通配符）
```

`undelete.py` 用法：误删文件被同步删掉后，运行 `python undelete.py list` 查看 `.trash/` 内容，`python undelete.py restore <文件名>` 恢复。

## 排除清单（sync-ignore.txt）

有些文件**不该**跨机同步：每机各自生成的产物、设备级密钥（两台不同，互覆会坏）、虚拟环境、已删任务的残留目录。语法同 `.gitignore`：

```
2026-01-15-10-30-00/my-task/decrypted/    # 整个目录排除
2026-01-15-10-30-00/my-task/key.cache     # 单文件排除
*.conflict-*                              # 通配符
```

**技巧**：清理"已删除任务但工作目录残留"时，先把该目录加进忽略清单再删除——这样另一台的副本既不会被拉回来，也不会被墓碑误伤。

## NAS 端目录结构（自动创建）

```
<NAS_ROOT>/
├── workspace-files/        # 各会话工作区文件镜像
├── projects/               # 对话记录 (.jsonl)
├── tasks/                  # 任务清单
├── scripts/                # 同步脚本自身（保持两台同版本）
├── .trash/                 # 墓碑删除的文件回收站（可用 undelete.py 恢复）
├── .ws_tombstones.json     # 删除墓碑表（NAS 权威副本）
├── sync_state.<机器名>.json # 每台机器的同步状态
└── .sync-lock              # 并发锁
```

## FAQ

**Q：会覆盖 workbuddy.db 导致数据库损坏吗？**
不会。数据库整文件被明确排除同步，只由 `sessions-merge.py` 做**记录级合并**（逐行读两边、按 `id` 和 `updated_at` 合并写回），这是全套件里唯一碰数据库的地方。

**Q：两台电脑同时跑同步会怎样？**
NAS 上的 `.sync-lock` 会让后到的一台等待；锁超过 10 分钟未释放视为死锁自动接管。但仍建议错开时间跑。

**Q：删掉一个任务后，它的文件夹还在？**
WorkBuddy 删除任务是软删除（数据库打标记），工作目录不自动回收。清理方法：把目录加入 `sync-ignore.txt` → 删除本地目录 → 在 NAS 上删对应 `workspace-files/<目录>` → 下次同步不会回拉。

**Q：手机端的数据能同步吗？**
本套件只覆盖 Windows PC 之间的局域网同步。手机端请用 WorkBuddy 官方的「连接电脑」模式或云端资料库。

**Q：支持 Mac/Linux 吗？**
脚本纯 Python + SMB 路径，理论上改路径写法可跑，但未测试。设计目标是两台 Windows PC。

## 已知边界与风险

完整风险矩阵（哪些操作绝对安全、哪些场景会触发冲突覆盖、如何恢复）见 **[docs/SYNC-RISK-ANALYSIS.md](docs/SYNC-RISK-ANALYSIS.md)**。开始使用前建议通读一遍。

## License

MIT — 见 [LICENSE](LICENSE)
