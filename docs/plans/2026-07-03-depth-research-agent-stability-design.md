# Depth Research Agent 稳定性改造设计

**日期**: 2026-07-03
**状态**: 已审核，待实现
**涉及文件**: `core/state.py`, `core/tools/delegate.py`, `core/git_utils.py`, `core/tools/apply_patch.py`, `cli/bridge.py`, `web/components/chat.py`, `core/agent.py`, `core/planner.py`, `core/context.py`

---

## 背景

Depth Research Agent 采用 Planner Agent 拆解任务 + 多个 Actor Agent 并发执行的架构。在 Python 3.12 环境下，存在以下三类稳定性风险：

1. **并发状态竞争**: 多个 Actor 同时写入 `GlobalState`，缺少同步原语
2. **DAG 失败穿透**: 依赖图中某任务失败后，子任务仍被调度执行
3. **Git Diff 可靠性**: 手动拼接 diff 对换行符和特殊字符敏感，`apply_patch` 易失败
4. **UI 中英混杂**: 系统级提示语言不一致，大块 Patch 渲染卡顿

---

## 阶段一：Actor 并发与状态管理加固

### 1.1 引入异步锁保护全局状态

**文件**: `core/state.py`

| 改动 | 说明 |
|---|---|
| `__init__` 新增 `self._lock = asyncio.Lock()` | 每个 GlobalState 实例持有一把异步锁 |
| `add_task` → `async def` | 操作 `task_tree` 和 `change_log` 时加锁 |
| `update_task` → `async def` | 同上 |
| `add_summary` → `async def` | 同上 |
| `consume_changes` → `async def` | 同上 |
| **`snapshot` → `async def`** | **新增**：保护读一致性，防止读到中间态 |
| **`get()` 增加 `threading.Lock`** | **新增**：防止 `WebBridge` 多线程并发首次调用时创建多个实例 |

**调用点更新**:
- `core/tools/update_state.py`: 所有 `state.xxx()` → `await state.xxx()`
- `core/tools/delegate.py`: `run_one` 内部所有 `state.update_task()` / `state.add_summary()` → `await`
- `core/planner.py`: `self.state.snapshot()` → `await self.state.snapshot()`

### 1.2 阻断 DAG 依赖图失败穿透

**文件**: `core/tools/delegate.py`

| 改动 | 说明 |
|---|---|
| `TaskNode.status` 类型扩展 | 新增 `"blocked"` 到 Literal 类型 |
| 失败时不加入 `completed` | 当前代码将失败任务也加入 `completed`，导致依赖它的子任务被错误解锁 |
| 级联标记 blocked | 遍历 `remaining` 中所有依赖该失败任务 ID 的子任务，标记为 `blocked` 并从 `remaining` 移除 |
| `asyncio.gather` 加 `return_exceptions=True` | 防止单个 Actor 的未捕获异常拖垮整批并发 |

**算法伪代码**:
```
for each batch_result:
    if status != "done":
        for each remaining_task:
            if failed_task_id in remaining_task.dependencies:
                await state.update_task(remaining_task_id, status="blocked")
                remove from remaining
        // 不加入 completed 集合
    else:
        add to completed
```

---

## 阶段二：Git Worktree 合并机制重构

### 2.1 重构 Diff 提取

**文件**: `core/git_utils.py`

| 改动 | 说明 |
|---|---|
| 废弃 `_diff_untracked_file` | 不再手动拼接 diff header |
| 废弃 `_list_untracked` | 不再需要列举未追踪文件 |
| 重写 `extract_diff` | 三步走：`git reset HEAD` → `git add -A` → `git diff --cached --binary` → `git reset HEAD` |
| `extract_diff` 改为 `async def` | 使用 `asyncio.create_subprocess_exec` 避免阻塞事件循环 |

**调用点更新**:
- `core/tools/delegate.py:179`: `extract_diff(wt_path)` → `await extract_diff(wt_path)`

### 2.2 自动清理 Fuzz 模式残留

**文件**: `core/tools/apply_patch.py`

| 改动 | 说明 |
|---|---|
| 新增 `_cleanup_rej_files(base_dir)` | 扫描 `*.rej` 文件，读取内容，删除文件 |
| 在 `execute` 的 `finally` 块调用 | 无论 dry-run 还是实际 apply，都清理 |
| 内容嵌入 `ToolResult` | Planner 能感知被拒绝的 hunk 内容 |

---

## 阶段三：UI 一致性与稳定性增强

### 3.1 统一反馈为中文

| 文件 | 当前文本 | 改为 |
|---|---|---|
| `cli/bridge.py:75` | `[System: Context compressed]` | `[系统：上下文已自动压缩]` |
| `core/context.py:183` | `[System: {n} chars omitted to prevent context overflow]` | `[系统：为防止上下文溢出，已省略 {n} 字符]` |
| `core/agent.py:265` | `Safety limit: Agent reached max steps. Please retry with a simpler request.` | `安全熔断：Agent 已达到最大步数限制。请尝试简化请求后重试。` |
| `core/planner.py:55` | `Safety limit: Planner reached max steps. Please retry with a simpler request.` | `安全熔断：Planner 已达到最大步数限制。请尝试简化请求后重试。` |
| `core/planner.py:148` | 同上 | 同上 |

### 3.2 防范大块 Patch 导致前端卡顿

**文件**: `web/components/chat.py` + `cli/bridge.py`

| 改动 | 说明 |
|---|---|
| `_render_tool_output` 增加截断 | 最多 200 行或 8000 字符（取先到达者） |
| CLI bridge 同样截断 | `cli/bridge.py` 的 tool_result 渲染逻辑增加相同限制 |
| 截断提示 | `... [完整差异已合并至主工作区，此处仅展示前 200 行]` |

---

## 实施顺序

三个阶段在同一分支上按顺序实施（阶段二依赖阶段一的 async 改动）：

1. 阶段一 → `core/state.py`, `core/tools/delegate.py`, `core/tools/update_state.py`, `core/planner.py`
2. 阶段二 → `core/git_utils.py`, `core/tools/apply_patch.py`
3. 阶段三 → `cli/bridge.py`, `web/components/chat.py`, `core/agent.py`, `core/planner.py`, `core/context.py`

---

## 测试验证要点

- [ ] 并发场景：模拟 4 个 Actor 同时 `update_task` + `add_summary`，验证无脏数据
- [ ] DAG 失败穿透：构造 A→B→C 依赖链，中间 B 失败，验证 C 被标记 blocked
- [ ] Git diff：创建含特殊字符、无末尾换行符的新文件，验证 `apply_patch` 成功
- [ ] .rej 清理：用 `--reject` 应用冲突 patch，验证 `.rej` 被清理且内容被报告
- [ ] 中文 UI：触发 compaction / max steps / error 事件，验证终端和 Web 端都显示中文
- [ ] 大 diff 截断：返回 500 行的 diff，验证 UI 截断至 200 行
