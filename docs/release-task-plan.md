# 后续发布任务

本文把第三方项目的设计建议转成当前仓库可验收的任务。参考项目只提供设计线索，不代表本仓库已经具备对应能力。基线为 `3866829`；本表中的完成状态须经主控检查实现和测试后再更新。

## 当前基线

- `review-changes` 0.2.1 已提供本地变更收集、证据校验、报告导出和修改后复查；复查脚本仍只做证据与状态约束校验，不代替宿主的语义判断。
- PR1 的本地 hermetic fixture、包外 smoke 和三 scope 验收已通过；相关测试为 225 passed、4 skipped。代码提交 `116e47e3ff62ef7213f48e81849392a37dfed72b` 的 [Standalone Skill CI run](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329) 已完成，Linux/Windows × Python 3.10/3.11 四个 job 全部成功。
- PR2 核心、Skill、统一入口、固定历史资源和 Codex 新会话恢复验收已通过；Skill 资源 `latest` 链接漂移 P1 已修复并独立复验，核心定向测试为 39 passed。首次恢复命令因 `python` 不存在返回 127，改用 `/usr/bin/python3 -S` 后通过。
- PR3 的宿主中立输入契约、run-record schema 和 seed 索引已整理并校验；尚无 A/B 对照数据或质量分数，因此 PR3 整体未完成。A/B 可先行，C 组待上下文选择器实现后追加。
- 当前三个 seed 是验收准备器生成的合成隔离夹具，不是真实项目盲测。真实 WorkBuddy 验收仍为 `not_run`，PR 输入接入和评论发布尚未开展。
- 2026-10-09 的独立 Codex 前向运行在合成 case-01 上生成 1 条 `confirmed` finding，首轮 finalize 成功；这只证明一次本地流程前向运行，不是 WorkBuddy 验收、盲测或 Skill 增益比较。记录见[前向运行日志](</root/project/Session Skill 前向运行 20261009/session/run-log.md>)。
- 现有 `eval/metrics.py` 与 `eval/run_eval.py` 面向服务端 `Finding`/`pr_url` 数据；没有可复用的宿主运行集，也不覆盖覆盖率、失败状态、复查判定或成本。保留它用于原数据格式，不能据此声称 Skill 质量已被评测。

## 发布任务

| 任务 | 状态 | Owner | 依赖 |
|---|---|---|---|
| PR1 自动验收基础 | 本地 fixture、包 smoke 和定向测试通过（225 passed、4 skipped）；代码 SHA `116e47e3ff62ef7213f48e81849392a37dfed72b` 的 Linux/Windows × Python 3.10/3.11 远端 CI 四项通过 | `skill_ci` | 当前打包器和 Skill 测试 |
| PR2 统一入口与会话记录 | 本地定向测试、固定历史资源和 Codex 新会话恢复通过；WorkBuddy 未实测 | `review_session` | 当前 `review_core` 输入、报告和复查契约 |
| PR3 宿主中立评测基线 | 契约、schema、seed 索引已校验；对照数据集与质量分数未开始 | `eval_tasks` | 现有 seed 准备器；后续真实宿主原始产物和人工标注 |
| PR4 上下文候选选择器 | 排队中（queued），未分派 | 未分派 | PR3 可比较的基线；稳定的快照输入 |
| PR5 历史报告与发布规划 | 后置排队（queued），未分派 | 未分派 | PR2 会话关联；稳定的校验结果与复查契约 |

### PR1：自动验收基础

- **范围：** 将 Skill/package 验收作为独立 CI job；从仓库外运行打包产物，覆盖 Linux 与 Windows 的包内脚本和路径行为。
- **验收：** job 不依赖 API 服务启动；安装/构建产物在独立项目中可导入和执行；工作区、暂存区、差异及中文/空格路径用例结果可追溯；常规测试和打包检查通过。
- **当前进度：** 本地 hermetic fixture、包外 worktree/staged/branch smoke 与 start/resume 验收通过，相关测试为 225 passed、4 skipped。代码 SHA `116e47e3ff62ef7213f48e81849392a37dfed72b` 的 [远端 run](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329) 于 2026-10-09 完成，Ubuntu/Windows × Python 3.10/3.11 四个 job 均成功：[Ubuntu 3.10](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329/job/113425907617)、[Ubuntu 3.11](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329/job/113425908062)、[Windows 3.10](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329/job/113425908039)、[Windows 3.11](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329/job/113425907970)。
- **未覆盖边界：** CI 不能证明 WorkBuddy 能导入 Skill、运行工具或完成跨会话复查；这些仍需真实宿主记录。

### PR2：统一入口与会话记录

- **范围：** 让一个 Skill 入口编排范围固定、上下文收集、工具状态、语义审查、结果校验和导出；增加轻量 session 记录，以仓库身份和内容指纹关联输入、结果草稿、校验结果及后续复查。
- **验收：** 中断后可定位已有证据；恢复时核验仓库、范围和快照指纹，内容改变即形成新快照；不同仓库/worktree 的会话不串用；流程完成状态与 `completed/partial/uncovered/failed` 审查状态分开；语义未完成的草稿不会伪装成已覆盖结论。
- **当前进度：** 核心、Skill、统一入口和固定历史资源本地验收通过；latest 漂移 P1 已修复并独立复验，39 项定向测试通过。Codex 在独立 case-01 工作目录完成 review/finalize，并在新会话中只凭已保存 session 与包恢复成功，未应用准备补丁。首次恢复调用使用 `python -S`，因命令不存在返回 127；改用 `/usr/bin/python3 -S` 后通过。真实 WorkBuddy 安装与运行仍未执行。
- **未覆盖边界：** 不在此任务中引入队列、数据库、服务端任务恢复或模型调用层；会话记录不能替宿主证明 finding 正确。

### PR3：宿主中立 A/B 评测基线

- **范围：** 使用 [评测输入契约](../eval/README.md)、[seed 索引](../eval/data/seed-cases.json) 和 [run-record schema](../eval/schemas/run-record.schema.json)，分开保存宿主原始输出、oracle/人工标注及脚本判分。先在同一宿主和固定案例上比较直接审查 `same_host_direct` 与当前 Skill `current_skill`；上下文选择器条件 `context_selector_skill` 在 PR4 实现后追加，不作为启动 A/B 的前置条件。首批目标为 10–12 个有独立标注的真实案例，再逐步扩展。
- **验收：** 每个运行有唯一条件、宿主/环境、确切快照指纹、原始转录和导出路径；标注不进入宿主输入；失败、未覆盖、工具缺失和人工介入单独保留；脚本分数可由保存的输出和标注重算；未知模型、token、成本写 `null`，不能写成零。
- **当前进度：** 输入契约、schema、三个合成 seed 索引已校验。Codex case-01 流程前向运行已留存草稿、semantic JSON、normalized JSON、报告、session 和 run log；Semgrep 为 missing，模型/用量/成本为未知。它不是三组对照之一，不产生质量分数；目前没有可比较数据集。
- **未覆盖边界：** 现有三例仅为合成流程夹具，不能证明真实代码库上的发现率。20–30 个真实案例是后续积累目标，不是当前数据或启动 A/B 的门槛。宿主未执行时不得产生模型评分；WorkBuddy 暂不可用时，可用实际可用的同一宿主开展 A/B，但必须如实记录，不能称为 WorkBuddy 验收。此任务不加入 Promptfoo 运行时、模型 API 接入或大规模评测框架。

### PR4：上下文候选选择器

- **范围：** 在给定工作区/暂存区/分支快照内，为接口变更、授权迁移、删除/重命名、数据结构变化及异步/资源生命周期变化生成带理由的候选调用方、契约、测试和配置；预算不足、检索失败及未读取候选须显式保留。
- **验收：** 对暂存审查只基于所选暂存快照；跨文件契约和合法鉴权迁移案例列出关键候选；遗漏不能计为覆盖；选择器实现后，在 PR3 固定的同宿主案例中增加 C 组，与 B 组比较发现、误报、覆盖和人工干预变化，并保留 A 组直接审查基线。
- **未覆盖边界：** 候选不是完整调用图，也不能证明候选已被宿主读取；首版不承诺所有语言的 AST/符号解析，不按候选列表推断语义结论。

### PR5：历史报告与发布规划

- **范围：** 为 finding 记录跨轮次事件、快照及证据引用；将已校验结果转换成可发布位置计划，区分可落在当前 diff 的行内 finding 和必须进入顶层报告的 finding。
- **验收：** 部分复查不能清除旧 finding；修复、持续、无法确认和再次出现均能追溯到当轮结果及证据；发布位置失败、权限不足或旧 SHA 不能使 finding 丢失；重复生成计划具有幂等键或可识别更新目标。
- **未覆盖边界：** 本任务先做历史和平台中立的位置规划；GitHub PR 获取、评论写入、权限配置和重试由后续 PR 接入任务另行验收。不能把删除行或非 diff 行 finding 从完整报告过滤掉。

## 推进顺序与门槛

PR1 与 PR2 可并行；PR2 本地验收和 Codex 新会话恢复已通过，WorkBuddy 验收仍待执行。PR1 本地验收与远端 Linux/Windows CI 矩阵均已通过；这不代表 WorkBuddy 宿主验收完成。PR3 可先用 10–12 个真实案例启动 A/B，不等待 C 组或 WorkBuddy 连接；后续同快照数据集逐步扩充。PR4 实现后再对同一宿主、同一案例追加 C，与 B 比较上下文选择器的增益。PR5 依赖稳定 session 与结果契约，外部 PR 获取和评论发布继续单独排期。

当前可发布的结论是：PR2 本地定向验证与 Codex case-01 新会话恢复通过；PR1 本地 fixture、包 smoke 和测试通过，代码 SHA `116e47e3ff62ef7213f48e81849392a37dfed72b` 的远端 Linux/Windows × Python 3.10/3.11 CI 四项均成功。PR3 仅完成输入基线，不存在质量分数。Codex 运行不是 WorkBuddy 实测、盲测或增益比较；WorkBuddy 安装/运行仍为 `not_run`，PR 输入接入未开始。PR4/PR5 仍排队，尚未分派。固定 `0.2.1` 发布包未重建，ZIP/manifest 哈希仍分别为 `42948cb0de941299c5e6ed39628acaeadd22bda26e0fbcefa0804460d05b74aa` 和 `5d8f6febfedf6532bc985b7d638099cacc7f99fdd408902c696c045135471ecf`。测试数、真实案例数和模型质量分数应由主控按后续实测更新。
