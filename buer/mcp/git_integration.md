# BUER Git 集成说明

BUER 通过 post_bash hook 截取 git 命令，自动维护 `(root, branch)` 级 project_id
和 append-only 快照，无需手动操作。

## 行为概览

| 事件 | 触发方式 | 结果 |
|------|---------|------|
| `git commit` | post_bash Feature 3 | 建快照 `reason='commit'`，绑定 HEAD commit |
| 回滚（reset/revert/rebase/merge/pull/stash pop/restore/clean/checkout --） | post_bash Feature 4 | 全量 reconcile + 快照 `reason='rollback'` |
| 切分支（checkout/switch） | post_bash Feature 4 | 按新分支 get_or_create project_id + 全量 reconcile + 快照 `reason='branch_switch'` |
| Session 启动/resume | session_start 兜底 | 若当前 HEAD 无快照（漏掉的 git 事件）→ 全量 reconcile + 快照 `reason='session_start'` |
| 首次见 (root, branch) | session_start | 建 project + 全量 reconcile + 快照 `reason='initial'` |
| 非 git 项目 | — | 行为不变，无 branch、无快照 |

## (root, branch) 唯一 project_id

每个 `(root_path, branch)` 对应一个唯一 project_id。

- 切换到新分支 → 新建 project_id（若不存在）
- 切回已有分支 → 复用原 project_id（UNIQUE 约束保证）
- 两个分支共享同一目录，但 determinations 独立 append，互不覆盖

## 快照机制（append-only）

```sql
-- snapshots 表：标记式，不复制数据
-- snapshot_at_seq = 快照时 determinations 的 max(seq)
-- 查"commit X 时的图" = seq ≤ snapshot_at_seq 的最新 determinations
```

同一 `(project_id, commit_hash)` 可有多条快照记录（append-only，不覆盖）。
`latest_snapshot_for_commit` 取最新一条。

## Session 兜底（接住漏掉的 git 事件）

post_bash 只能截取经 Claude Code Bash 工具执行的命令。以下情况 post_bash 不会触发：

- GUI 客户端（Tower、GitKraken、VS Code Source Control）的 commit
- 另一台机器或另一个 shell 执行的 git 操作
- 直接调用 git 而未经过 Claude Code Bash

session_start 在会话启动/resume 时检查当前 HEAD 是否已有对应快照：

- **有快照** → 正常，不重复触发（幂等）
- **无快照** → 触发全量 reconcile + `reason='session_start'` 快照，补齐漏掉的事件

## .gitignore 建议

强烈建议将 `.buer/` 加入项目 `.gitignore`，避免 buer 数据库被 git reset/checkout 等操作影响：

```
# 在项目根的 .gitignore 中添加：
.buer/
```

buer 会在 session 启动时温和提示（若未配置）。

## 可选：post-commit hook（更完整捕获 GUI commit）

session_start 兜底已能接住大部分漏掉的 git 事件。若需要更即时的捕获，
可在项目的 `.git/hooks/post-commit` 添加 hook：

```bash
#!/bin/sh
# Optional: notify buer immediately after git commit
# session_start fallback already covers most missed events;
# this hook provides faster response for GUI commits.
curl -s -X POST http://127.0.0.1:7777/buer/post-bash \
  -H 'Content-Type: application/json' \
  -d "{\"cwd\":\"$(pwd)\",\"tool_input\":{\"command\":\"git commit\"},\"tool_response\":{\"stdout\":\"ok\",\"stderr\":\"\"}}" \
  > /dev/null 2>&1 || true
```

**注意**：若项目已有 `post-commit` hook，需手动 chain（不要覆盖），
buer 不会自动修改 `.git/hooks/`。

使 hook 可执行：

```bash
chmod +x .git/hooks/post-commit
```
