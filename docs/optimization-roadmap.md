# 优化路线图

[返回仓库首页](../README.md) · [使用指南](guide.md) · [开发指南](../CONTRIBUTING.md)

这份路线图记录当前可靠性改动的边界，以及接下来可以独立验收的工作。每个阶段都应先有可重复的测试和观测数据，再调整提示词、模型或 Agent 数量。

## 当前方向：Skill 优先

默认产品入口改为 `review-changes`：使用宿主模型完成变更范围固定、上下文阅读、可选静态检查、语义审查、证据核实和报告生成。共享底座不依赖模型 SDK 或服务组件，现有服务继续提供可选自动化部署。第一轮实现与安装验收见 [Skill-first 说明](skill-first.md)。

本地工作区、暂存区、分支审查和修改后复查已经形成第一版闭环；PR2 的 Codex 新会话恢复验收也已通过。PR1 本地 fixture 和包 smoke 通过；远端提交 `e8c719b635067ac33cca7300ffe8a4584ded3961` 的 [Standalone Skill run](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38198341019) 中 Linux/Windows × Python 3.10/3.11 四项全过。2026-10-09 的 11-case/23-run A/B pilot 仍缺人工标签、完整 host/model/Skill-load 元数据和独立覆盖审计，不能得出严格 A/B 质量比较；[ab-pilot integrity run](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38198341075) 仅验证公开工件完整性。WorkBuddy 可连接后另做真实宿主验收。脚本与隔离测试不计作宿主通过。

## 已完成：可靠性基础

当前主流程已经具备以下行为：

- Agent 使用异步 Anthropic 客户端；同步的 AST/Semgrep 分析移到线程执行，避免阻塞事件循环。
- `MAX_PARALLEL_AGENTS` 作为同一服务事件循环内跨任务共享的上限；任务在等待配额时不消耗 Agent 执行槽位。
- Agent/文件执行记录区分成功、超时、异常和畸形模型输出。
- 任务终态区分 `completed`、`partial`、`uncovered` 和 `failed`。明确返回 `findings=[]` 的调用属于成功的清洁结果。
- 只有完整且已持久化的结果写入重复审查缓存；部分、未覆盖和失败结果不能污染缓存。
- 报告和 API/UI 展示文件覆盖、失败原因和终态，使“没有发现问题”和“没有完成审查”可区分。

这批改动仍然不保证停止已经进入底层线程的同步分析，也不提供多事件循环间的进程级额度协调或进程重启后的任务恢复。模型 API、GitHub 和数据库的实际延迟、限流与可用性需要在部署环境中单独测量。

## 已完成：修改后复查

`review-changes` 可以把上一份已校验报告与新的本地快照关联起来。复查先保留全部稳定 `finding_id` 匹配，再对剩余问题使用确定性回退：`category`、`root_cause`、`trigger`、`impact` 经大小写及空白归一化后必须精确相等；捕获的证据路径可用于消歧。每条当前问题最多关联一条旧问题，无法消歧时保持未确认。这是规范化后的精确文本匹配，不是模糊语义相似度。

每条旧问题输出 `resolved`、`persisting` 或 `unverified`。标记已解决要求当前快照中有有效的修复证据，其中至少包含指向旧问题证据路径的 `after` 引用，且该引用显示出相对旧快照可检测的源码变化（脚本比较捕获源码内容时保留空白差异）；还要说明触发路径如何改变。所有仍可访问的旧证据路径必须以当前 `after` 证据引用；删除路径需引用当前 `before` 源码，并引用旧证据中已有的当前调用方。所需旧上下文路径也须以当前 `after` 证据引用；所有相关变更文件均须完成语义检查且无上下文缺口。同 ID 当前 `confirmed` 或 `needs_confirmation` 问题与已解决决策冲突时不能判定已解决。宿主负责语义判断，脚本检查捕获证据及决策/范围约束，不证明宿主解释本身正确。无关文件覆盖不完整时，单条问题仍可在相关覆盖完整的前提下判为已解决，但整体复查状态保持 `partial`。复查同时列出新确认问题和覆盖缺口；报告消失本身不会被当作修复。

## 可选服务后续：持久化执行

目标是把 API 进程内的 `asyncio.create_task` 替换成可恢复的 worker：

1. 选择 Redis-backed queue 或独立消息系统，定义任务租约、可见性超时和有限重试。
2. 为任务增加 attempt、worker、开始/结束时间、失败原因和规则版本等持久字段。
3. 为审查版本建立幂等约束，避免 webhook 重复投递造成重复模型调用或重复评论。
4. 增加 shutdown drain、任务取消和过期租约恢复测试。

验收重点是 API/worker 重启后 `pending` 与失去租约的 `running` 任务可以继续，重复事件不会产生重复结果；在这之前不要宣称系统支持断点恢复。

## 安全与运维

下一阶段补充 API Key/JWT、Webhook 强制验签、仓库 allowlist、请求体限制和限流。健康检查应区分进程存活与 PostgreSQL、Redis、模型依赖可用性，并提供 readiness 语义。外部评论和通知继续保持显式配置开关。

## 质量与成本

宿主中立评测的 pilot、输入契约和发布任务见[后续发布任务](release-task-plan.md)、[评测输入说明](../eval/README.md)及[2026-10-09 报告](../eval/results/2026-10-09-ab-pilot.md)。三个 seed 仍是合成隔离夹具；旧 11-case/23-run pilot 尚无人工标签或经认证的盲测，WorkBuddy 仍为 `not_run`。host/model 身份、Skill loading 与独立 coverage audit 不完整，因此 agent-only 记录不能说明人类质量或 Skill 增益；人工质量指标仍为 null。v2 失败及 [v2 失败报告](../eval/results/2026-10-12-ab-workflow-v2.md)保持历史记录。v3 两个独立 GO 后正式启动并以 `closed_incomplete` 结束：12 slots 中 1 个启动并失败、11 个暂停无 retry，host/evidence 为 0、pairs 为 0/6，strict build 为 0 records，validate/score/summarize 为 NA。Codex 拒绝 `properties.tool_runs.items` 开放 object，wrapper exit 1 且无有效 JSON；trace 11198 的 `pread64` 返回裸 `?`、结果不可得，trace incomplete。partial seal `accepted` 只覆盖 98 个一致引用，11 个 slot result 不可用。v3 closure 的 Limited GO 仅适用于已封存的零记录工件；schema review、相关测试和版本化导出历史见[评测输入说明](../eval/README.md)。这些检查不改变 v3 的 `closed_incomplete` 状态，也不构成质量或 Skill 增益结论。

workflow-validation-v4 (`2026-10-12-ab-workflow-validation-v4`) 已在提交 `25d35c6` 固定公开 summary/evidence/check，执行仍为 `closed_incomplete`：12 个 planned slots 中 1 个 launched、11 个 unstarted；evidence-valid slots 为 0、eligible pairs 为 0，有 1 条 run record 和 1 个机械 score 文件。严格 build → validate → score → validate → summarize 均通过，export check 的 5 项绑定/投影检查均 `verified`；它们只验证流程与公开工件一致性，不代表有效证据或质量通过。strict summary 的 3 条 paired-case 行及 5 个缺失 `(case_id, condition)` 组合是 summary-level 记录，不等于 11 个 unstarted slots。该批复用既有语料、仅验证 workflow reproducibility，不是 fresh blind test；人工 labels pending、人工质量指标 null，host/model/provider 与成本 unknown。

v4 grading index 在首个 slot 启动后才 seal（`pre_first_model=false`）；原始 `blocked_label_reference_present` 是保守的路径引用启发式，不证明标签内容被读取。v4 固定 source commit `c80ab00`、runtime normalizer/parser v3；parser v6 的独立 GO 仅面向未来新工件，旧 v4/v3/pilot 工件不重写。复现旧 trust/sealer/scoring 需使用 `c80ab00` 基线；closure export check 对保存的哈希绑定投影验真，不要求当前主分支源码不变。三份公开文件的路径和 SHA 见[评测输入说明](../eval/README.md)。

当前改动位置也有区分：v3 strict schema/preflight 与 trace 源修补留在共享工作区；v3/v4 closure summary/evidence/check 是固定公开工件。零记录 v3 Limited GO 仅覆盖 v3 工件；v4 的完整状态和五项 check 口径见[评测输入说明](../eval/README.md)。后续 workflow retry 应使用单独固定的 parser/source 版本和执行记录；质量评估仍需人工标注、宿主/模型/Skill-load 元数据与独立 coverage audit。PR4 的 C 组仍等待 context-omission 证据，PR5 继续排队。20–30 个真实案例仍是后续积累目标；成本和 token 用量不可见时记为 `null`，失败和未覆盖必须保留，不能当作空结果。

## 扩展边界

在执行队列、认证和评测稳定后，再抽象 Model Provider、Agent registry、语言能力矩阵和代码托管平台适配。新增 Agent 必须提供结构化输出契约、超时/取消测试、空结果测试和错误状态测试；新增模型不得绕过统一运行时。

## 当前不应承诺的能力

- 进程重启后自动恢复审查。
- 统一 API 认证或公开部署安全。
- 自动 Merge Gate、跨仓库策略和通用模型 Provider API。
- 已经测量的误报率、漏报率、延迟或成本下降。
