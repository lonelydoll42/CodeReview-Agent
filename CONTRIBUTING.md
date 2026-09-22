# 开发与 Git 工作流

使用 Python 3.10 或 3.11。首次开发：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
```

普通测试不需要 GitHub、LLM、Redis 或数据库凭据。设置指向专用测试数据库的
`TEST_DATABASE_URL=postgresql+asyncpg://...` 后，还会运行真实 PostgreSQL 测试；
该测试创建随机命名的 schema，结束时只删除自己创建的 schema。
CI 在 PostgreSQL 15 上执行该测试，并覆盖 Python 3.10/3.11。

## 分支与提交

- 从最新 `main` 创建短期主题分支，如 `fix/review-snapshot` 或 `perf/dashboard-cache`。
- 多个并行任务使用 `git worktree add -b <branch> <new-directory> origin/main`。
- 一个提交处理一个可解释、可回退的主题；换行规范化与业务改动分别提交。
- 提交消息使用 `fix(scope): ...`、`perf(scope): ...`、`test(scope): ...` 等格式。
- 合并前检查 `git log --graph --oneline --all` 与 `git diff origin/main...HEAD`。
- 对有继承关系的功能分支，先检查共同祖先，避免重复 cherry-pick 同一批提交。
- 用 `git revert <commit>` 回退已共享的普通提交；合并提交需确认主线后使用
  `git revert -m 1 <merge-commit>`。不要重写已共享的历史。

PR 需要描述触发场景、行为变化和验证结果。仓库管理员可将 CI 的两个 Python
检查设为 `main` 合并前的必需检查，并要求通过 PR 合并。工作流文件本身不会启用
GitHub 分支保护。完成发布验证后再建立带注释的版本标签，部署记录完整提交 SHA。

## 审查代码的版本约定

审查报告记录 `base_sha`、`head_sha` 和 `merge_base_sha`。完整源码通过 GitHub
Contents API 按固定 SHA 读取，不使用可变分支名；无需为每个 PR 克隆整个仓库。
拉取前后 PR 的 base/head 必须一致，否则任务失败并提示重试。缺失或不完整的
patch 使用 merge-base 与 head 的完整文本重建；报告明确列出未分析的文件。

AST/复杂度分析使用完整文件；Semgrep 提示仅保留新增行上的问题。未改动代码可
提供上下文，但行内评论必须位于审查 diff 的新增行，并绑定被分析的 head SHA。
发布前发现 PR 已更新时，跳过旧版本行内评论。

缓存键包含比较双方、共同祖先和分析代码指纹。指纹覆盖 Agent 的模型常量、
提示词、静态规则和依赖声明。外部分析配置改变时递增 `.env` 中的
`REVIEW_RULESET_VERSION` 并重启服务。数据库保存成功且所有 Agent 调用返回后
才写缓存；命中时为新任务复制可查询的报告及 Agent 结果。

当前边界：仅支持 UTF-8 文本且单文件不超过 1 MiB；删除文件、二进制和不支持的
语言会显示为未分析。GitHub 文件列表不完整时任务失败，不把部分审查当作完整结果。
Agent 只返回空结果的内部降级行为仍需后续引入显式错误状态，以区分“无问题”与
“模型调用失败”。完整源码分析会增加 GitHub API 请求量，当前缓存主要节省模型调用。
