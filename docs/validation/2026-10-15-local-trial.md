# 2026-10-15 本地 Skill 试用摘要

本摘要记录一次使用公开 `review-changes` `0.3.0-rc.1` 包的本地工作流试用。Skill 由当前 agent 读取解压目录中的 `SKILL.md` 执行，没有安装或验证宿主自动发现与触发。

## 结果

- 对已知历史分支差异完成初审：提交 [`4a0108e`](https://github.com/lonelydoll42/CodeReview-Agent/commit/4a0108e4409af9b8ba65800a106bea69ebcca41c) 相对其父提交 [`801e9b4`](https://github.com/lonelydoll42/CodeReview-Agent/commit/801e9b4c3cbc9537e584cca3fdfad2a174b66fff) 新增 `_runtime.py` 导入，但批次 preseal 只封存 `project_tool_runs.py`；复制该单文件到缺少 `_runtime.py` 的目录执行，复现 `ModuleNotFoundError`。初审最终报告为 1 条 confirmed MEDIUM（P2）`packaging_error`。
- 负向对照新建未跟踪的 `Unrelated Review Control.md`，通过 worktree scope 和 `include_untracked` 纳入快照；保留全部 prior evidence 后 recheck 为 `persisting`，没有新 finding。
- 在隔离副本中让 projector 自包含 UTF-8 guard，并将两条批次提示改用 `python3 -X utf8 -S`。按实际静态扫描 JSON 输入，在只复制 projector、没有 `_runtime.py` 的目录中运行，退出码为 0 并产生 1 条投影结果；同包修复后 recheck 为 `resolved`，无新 confirmed finding。证据涉及 `project_tool_runs.py`、`ab-batch-neutral.md`、`common-output.md` 和仍只封存单个 projector 的 `run_ab_batch.py`。

## 包与来源

- 公开 release：[`review-changes-v0.3.0-rc.1`](https://github.com/lonelydoll42/CodeReview-Agent/releases/tag/review-changes-v0.3.0-rc.1)，source revision [`956439e`](https://github.com/lonelydoll42/CodeReview-Agent/commit/956439ea9c08afaffa4084473a19cc7c98cc521c)，27 项资源。
- ZIP SHA-256：`cc17d9cc9270613fe84bd86a876f09a0c7a92918f928d08f8d0786edcaf97bed`；manifest SHA-256：`4ea7eca2cc18e63a0906d95b331464b7cf8b9b4d76a42bc9d58d12779070d87f`。ZIP/manifest 清单哈希匹配，`unzip -t` 通过。
- 原始最终 attempts 为初审、负向对照和修复复查各自的 `0002`；它们保留在本地 session 中，未作为仓库内的公开复验材料。

## 命令摩擦

- 首次 sidecar 请求超过一分钟无数据；设置连接与总时限后重试成功。sidecar 是裸 SHA-256，不能直接交给 `sha256sum -c`；本次改为比较 sidecar 内容与本地 manifest SHA。试用指南提供的 Python 校验步骤不受此格式差异影响。
- 环境没有通用 `python` 和 `jq` 命令，改用 `python3 -X utf8 -S` 与 Python 标准库解析 JSON。初始候选短哈希 `fe64ad9` 在当时 checkout 只落到任务表文档改动；核对历史后将样本头定位到 `4a0108e`。
- 初轮报告将 Semgrep 状态修正为工具原文 `Semgrep executable was not found on PATH`。主控协作消息共 5 条，包含边界、进度和措辞沟通，不等于 5 次人工介入；各次报告与复查 attempt 均保留，没有覆盖旧结果。

## 限制

本次使用已知历史缺陷，不是盲测、严格 A/B、独立外部用户试用或宿主取证；不产生准确率、召回率、质量提升或 Skill 增益结论。记录的 finding 属于提交 `4a0108e` 所代表的历史样本，不表示当前主分支存在该缺陷；修复仅在隔离副本中验证。Semgrep 不可用，未声称静态扫描结果；未运行 pytest、CI 或完整 smoke。Host、model、token 和 cost 均 unknown；WorkBuddy 导入与宿主自动发现未验证。严格 A/B 仍暂停，0 个有效配对；真实外部试用反馈尚未收集。
