# WorkBuddy 跨设备双向同步 — 覆盖 / 丢失风险全面分析

> 分析对象：`%USERPROFILE%\WorkBuddy\scripts\` 下的 `sync-bidirectional.py`、`sessions-merge.py`、`tasks-report.py`、`sync-all.bat`
> 目标：确认两台电脑（办公机与家用机）双向同步时，任务 / 会话 / 其他信息是否可能被相互覆盖导致丢失；同步目的是让两台电脑看到一致的信息，且在一台上的任务与对话能在另一台继续操作。
> 结论速览：**串行使用下安全且能达成目标；并发 / 独立修改同一文件（尤其是同一会话的对话）时会覆盖丢失一方内容（多数留 `.conflict` 备份可恢复）。`workbuddy.db` 已排除整文件同步，不再有全库覆盖风险。**

---

## 一、各数据类型的同步机制与风险矩阵

| 数据类型 | 同步机制 | 何时安全 | 何时会丢 | 严重度 |
|---|---|---|---|---|
| **sessions 表**（会话元数据 / /resume 列表） | `sessions-merge.py` 按 `id` 记录级合并，newer `updated_at` wins | 总是 | 不丢 | 无 |
| **projects/`<sessionId>`.jsonl**（对话正文） | `sync_dir_bidirectional` → 单文件双向；冲突时保留较新一方 + 存 `.conflict` 备份 | 串行追加（先拉后写） | **两台同时 / 未先同步就各自追加同一会话 → 一方对话片段被覆盖丢失**（有备份） | **高** |
| **tasks/`<session>`/`<taskId>`.json**（任务清单） | 目录双向；不同文件互补保留；同名冲突保留较新 + 备份 | 不同任务互补 | 同一 `taskId` 两边同时改 status → 一方修改丢失（概率极低，taskId 为随机 UUID） | 低 |
| **daily log**（`YYYY-MM-DD.md`） | 单文件双向，但 daily log 走 `merge_daily_log` **按行去重自动合并** | 总是 | 不丢（自动合并） | 无 |
| **MEMORY.md**（项目/用户级记忆） | **段落级合并（B1）**：按 H1-H3 标题并集全部段落，仅真正分歧的段落保留较新版 + 备份 | 单边修改 / 任一侧追加新段落 | 同一段落两边独立改写 → 该段落旧版丢失（有 `.merged` 备份，其余段落不丢） | 低 |
| **identity**（SOUL/IDENTITY/USER） | **段落级合并（B1）**：同 MEMORY.md，按标题并集段落 | 单边修改 / 任一侧追加新段落 | 同一段落两边独立改写 → 该段落旧版丢失（有 `.merged` 备份） | 低 |
| **settings.json / mcp.json** | 单文件双向；冲突保留较新 + 备份 | 配置一致 | 两边不同配置 → 一方配置丢失（有备份） | 中 |
| **workbuddy.db** | **已排除文件同步**，仅 `sessions-merge.py` 合并 `sessions` 表 | 总是 | 不再整文件覆盖 | 已解决 |
| **tasks-report.html** | 单文件，每次重建 | 总是 | 被对方版本覆盖（仅报告，无害） | 无 |

---

## 二、最高危场景还原：对话被覆盖

`sync_single_file` 的 "BOTH changed" 分支（第 218-258 行）对 **非 daily-log 文件** 的策略是：`shutil.copy2(较新一方, 对方)` + 把被覆盖方存为 `*.conflict-<时间戳>` 备份。即**永远只保留一方，另一方靠备份手动恢复**。

`projects/<sessionId>.jsonl` 正是这种"非 daily-log 单文件"。具体触发过程：

1. 办公电脑在会话 `S` 追加对话 `A` → 本机 `S.jsonl = 原 + A`。
2. 用户**没跑 sync-all** 就关掉办公电脑，去家里电脑。
3. 家里电脑打开同一会话 `S`（已通过 sessions 合并出现），追加对话 `B` → 家里 `S.jsonl = 原 + B`（家里是从 NAS 拉的"原"版本，没拿到办公的 `A`）。
4. 两台各跑一次 sync-all：
   - 家里先跑：本地 `原+B` 较 NAS `原` 变了 → push → NAS = `原+B`。
   - 办公后跑：本地 `原+A`、NAS `原+B`、双方相对上次 state 的"原"都变了 → **BOTH changed 冲突** → 按 mtime 保留较新一方，**另一方被覆盖**。
   - 结果：要么办公的 `A` 丢（家里 mtime 较新），要么家里的 `B` 丢（办公 mtime 较新）。被丢的一方在 NAS 上有 `.conflict` 备份，可手动找回，但**不会自动合并**。

> 这就是"在一台电脑的对话，在另一台看不到且无法继续"的根因——不是同步没生效，而是**并发写同一会话触发了整文件冲突覆盖**。

---

## 三、其他风险点

1. **`sync_state.json` 是 NAS 上共享的单文件**（`STATE_FILE = os.path.join(NAS_ROOT, "sync_state.json")`，第 21 行）。两台电脑同时跑 `sync-all.bat` 会交错读写这个状态文件，导致 hash 判断错乱 → 误判 changed → 不必要的 push/pull → 潜在覆盖。**不要在两台电脑几乎同时跑同步。**
2. **`workspaces` 表不跨设备**（automations 已解决，见 C1）：`workbuddy.db` 已排除文件同步，`sessions-merge.py` 现合并 `sessions`、`automations`、`automation_runtime_state` 三张表（C1 已实施），所以**自动化任务可在两台电脑互相可见 / 运行**。仅 `workspaces` 元数据与 `automation_runs` 执行历史仍不跨设备（`automation_runs` 日志量大、价值低，刻意不同步）。
3. **空 workspace 目录**：曾经的"工作目录可能已被重命名或删除"报错，已用 `ensure_cwd_dirs()`（只 mkdir）修复。
4. **NAS 连接抖动**：SMB 会话空闲会断，导致事后 `isdir` 报不可达；已有秒级 TCP 探活避免脚本挂死，重连或重跑即可。

---

## 四、当前已有的保护（正面）

- ✅ `workbuddy.db` 排除整文件同步，彻底消除全库覆盖风险
- ✅ daily log 自动按行去重合并（不丢日志）
- ✅ sessions 表按记录合并（newer wins，不丢会话）
- ✅ 冲突文件保留 `.conflict-<时间戳>` 备份（可手动恢复）
- ✅ cwd 目录自动创建（修复"工作目录不存在"报警）
- ✅ legacy NAS `workbuddy.db` 移出同步路径（不直接删，可恢复）
- ✅ NAS 不可达时秒级探活报错，不挂死

---

## 五、加固建议（分级）

### 必做（消除高危）
- **A1. `projects/*.jsonl` 改为"按行去重追加合并"**：复用 `merge_daily_log` 的思路，对 jsonl 按每条记录的 id/时间戳去重合并，而非整文件冲突覆盖。这样办公的 `A` 和家里的 `B` 都能保留，**彻底解决对话丢失**。这是达成"跨设备继续操作"目标最关键的一处。
- **A2. `sync_state.json` 加并发锁**（或改为每台电脑本地 state、NAS 仅存 canonical）：避免两台同时跑导致状态错乱。最低成本做法是文档规定"不要两台同时跑"。

### 建议
- **B1. ✅ 已完成 — MEMORY.md / identity 段落级合并**：实现 `merge_sections` / `merge_markdown`，按 H1-H3 标题并集段落，仅分歧段落保留较新版并留 `.merged-<时间戳>` 备份。已接入 `sync_single_file` 的 cold-start 与 BOTH-changed 两个冲突分支（见 `sync-bidirectional.py`）。
- **B2. 固化"使用纪律"**：操作前先 `sync-all` 拉最新；结束/切电脑前 `sync-all` 推送；避免两台同时写同一会话。

### 可选
- **C1. ✅ 已完成 — automations 跨设备合并**：`sessions-merge.py` 泛化出 `get_rows` / `write_rows_table`，并对 `sessions`、`automations`、`automation_runtime_state` 三表做 NAS canonical 库并集合并（`updated_at` / `last_run_at` 较新者胜）。两台电脑的自动化任务现已可互相同步；运行 `sync-all.bat` 后重启 WorkBuddy 即可在任一端看到另一端创建的自动化。

---

## 六、当前推荐的安全使用纪律（达成目标的最小约束）

1. 开始工作前，先跑 `sync-all.bat` 拉取对方最新状态。
2. 结束工作 / 切换电脑前，跑 `sync-all.bat` 推送。
3. **不要两台电脑同时写同一个会话**（实施 A1 后可放宽）。
4. 若发现某文件被 `.conflict` 覆盖，NAS 对应目录下有 `.conflict-<时间戳>` 备份，可手动找回。
