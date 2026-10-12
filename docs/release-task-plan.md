# 后续发布任务

本文把第三方项目的设计建议转成当前仓库可验收的任务。参考项目只提供设计线索，不代表本仓库已经具备对应能力。本轮远端基线为 `e8c719b635067ac33cca7300ffe8a4584ded3961`；本表中的完成状态须经主控检查实现和测试后再更新。

## 当前基线

- `review-changes` 0.2.1 已提供本地变更收集、证据校验、报告导出和修改后复查；复查脚本仍只做证据与状态约束校验，不代替宿主的语义判断。
- PR1 的本地 hermetic fixture、包外 smoke 和三 scope 验收已通过；相关测试为 225 passed、4 skipped。远端提交 `e8c719b635067ac33cca7300ffe8a4584ded3961` 的 [Standalone Skill run 38198341019](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38198341019) 中 Linux/Windows × Python 3.10/3.11 四项全部通过。此前提交 `116e47e3ff62ef7213f48e81849392a37dfed72b` 的 CI 结果保留为历史记录。
- PR2 核心、Skill、统一入口、固定历史资源和 Codex 新会话恢复验收已通过；Skill 资源 `latest` 链接漂移 P1 已修复并独立复验，核心定向测试为 39 passed。首次恢复命令因 `python` 不存在返回 127，改用 `/usr/bin/python3 -S` 后通过。
- PR3 的宿主中立输入契约、run-record schema、11 个真实案例的 23 次 A/B pilot 和便携报告已完成；人工 labels、模型/宿主/Skill 加载元数据、独立覆盖审计和严格可比的 A/B 对照仍未完成。PR3 的质量评估仍未完成，C 组待 PR4 实现并补足可比较基线后追加。旧 pilot integrity [run 38198341075](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38198341075) 通过，只验证公开工件完整性。
- workflow-validation v2 批次失败，详情见 [v2 失败报告](../eval/results/2026-10-12-ab-workflow-v2.md)。v3 (`2026-10-12-ab-workflow-validation-v3`) 经两个独立 GO 后正式启动，现为 `closed_incomplete`：1 个 slot 启动并失败、11 个暂停无 retry、0 host/evidence、0/6 pairs；严格 build 为 0 records，validate/score/summarize 为 NA。Codex 拒绝开放的 `properties.tool_runs.items` schema，wrapper exit 1 且无有效 JSON；trace 11198 的 `pread64` 返回裸 `?`，结果不可得且 trace incomplete。partial seal `accepted` 只覆盖 98 个一致引用，11 个 slot result 不可用。私有报告在仓库外，SHA-256 `5e4ccf3b366859d9139235c8861454d18e729fc55f51dd0abc98ec63295559a7`；snapshot-after SHA-256 `e0908e2057bf6fb91d988b1f3b86342af49d6593afe08b4a65d5f0bf3b3c755d`。closure export 已集成：[summary](../eval/results/2026-10-12-ab-workflow-validation-v3-summary.json) SHA-256 `24aef235909d8c554b508fbbb20519742933789477cd5073357bc21608f01e5a`、[evidence](../eval/results/2026-10-12-ab-workflow-validation-v3-evidence.json) SHA-256 `05a706ff69abeb76c6c6b417d89a1fdefdc0bc538454f5fb49dbd50f226d293f`、[check](../eval/results/2026-10-12-ab-workflow-validation-v3-check.json) SHA-256 `4a424c4e79197cbc2efeebe9240863054e81a048085f2627d0de727957a798eb`。`eval_reliability_review` 对当前真实零记录 closure export 给出 Limited GO，确认封存 source 复算、summary 实际 SHA 与 check 相互一致；此 GO 仅覆盖该零记录结果，不适用于非零 summary。schema 二次 review 正式 GO：生产 schema 通过，`tool_runs.items` 的 `true` 和 `{}` 实际输入分别被拒绝，新增 5 项复现测试通过。closure export 的 30 项相关 CI tests 和 tool projection 的 84 项定向 tests 通过；联合全量为 358 passed、4 skipped（3 个 Semgrep 测试在默认 PATH 下未找到 executable、1 个 PostgreSQL 测试缺少 `TEST_DATABASE_URL`），将 `.venv/bin` 加入 PATH 后 Semgrep 专项 9 passed。Ruff、`pip check`、`git diff --check` 及 v3 closure、v2 workflow、历史 pilot exporter checks 均通过。v3 仍为 `closed_incomplete`；v2 失败记录和旧 23 条 pilot 不变，本轮没有 v4 产物或执行。
- 当前三个 seed 是验收准备器生成的合成隔离夹具，不是真实项目盲测。真实 WorkBuddy 验收仍为 `not_run`，PR 输入接入和评论发布尚未开展。
- 2026-10-09 的独立 Codex 前向运行在合成 case-01 上生成 1 条 `confirmed` finding，首轮 finalize 成功；这只证明一次本地流程前向运行，不是 WorkBuddy 验收、盲测或 Skill 增益比较。记录见[前向运行日志](</root/project/Session Skill 前向运行 20261009/session/run-log.md>)。
- 旧的 `eval/metrics.py` 与 `eval/run_eval.py` 面向服务端 `Finding`/`pr_url` 数据；它们不消费新契约下的 23 次 A/B pilot 运行，也不覆盖覆盖率、失败状态、复查判定或成本。新 run records 构成首批宿主运行集，但因人工标注和严格可比条件未齐，仍不能声称 Skill 质量已被评测。

## 发布任务

| 任务 | 状态 | Owner | 依赖 |
|---|---|---|---|
| PR1 自动验收基础 | 本地 fixture、包 smoke 和定向测试通过（225 passed、4 skipped）；远端 `e8c719b635067ac33cca7300ffe8a4584ded3961` 的 Standalone Skill 四矩阵全过 | `skill_ci` | 当前打包器和 Skill 测试 |
| PR2 统一入口与会话记录 | 本地定向测试、固定历史资源和 Codex 新会话恢复通过；WorkBuddy 未实测 | `review_session` | 当前 `review_core` 输入、报告和复查契约 |
| PR3 宿主中立评测基线 | v3 `closed_incomplete`；零记录 closure export 获 Limited GO（仅限当前工件）；schema 二次 review GO，`true`/`{}` runtime repro 和 5 项测试通过；联合全量 358 passed、4 skipped | `eval_tasks` | 冻结并执行新的 12-slot workflow-only batch。质量评估仍需 labels/metadata/coverage audit |
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
- **当前进度：** 11 个真实案例、23 条旧 A/B run 保持原样；A 有 12 次尝试/11 次完成，B 有 11 次尝试/11 次完成，23/23 raw host-output hash 与原记录一致。旧记录缺少同期 `input.review_manifest_sha256`，strict replay blocked；Agent grader v2 只提供暂定证据，不是 human recall。v3 仍为 `closed_incomplete`（1 failed、11 unstarted、0 records、0/6 pairs）。公开 closure summary/evidence/check 已集成，路径与 SHA 见 [评测输入说明](../eval/README.md)；`eval_reliability_review` 对当前真实零记录 closure export 给出 Limited GO，确认封存 source 复算、summary 实际 SHA 和 check 一致，该结论不适用于非零 summary。schema 二次 review 正式 GO：生产 schema 通过，`tool_runs.items` 的 `true` 和 `{}` 实际输入分别拒绝，新增 5 项复现测试通过。closure export 30 项相关 tests、tool projection 84 项定向 tests 通过；联合全量 358 passed、4 skipped，3 项 Semgrep skip 另以 PATH 补跑为 9 passed，1 项 PostgreSQL 因缺少 `TEST_DATABASE_URL` 跳过。Ruff、`pip check`、差异检查及 v3/v2/pilot exporter checks 通过。旧 23 条记录的人工标签仍 pending，quality 仍为 null；没有 v4 产物或执行。
- **未覆盖边界：** 11 个真实案例仍未人工标注或认证为盲测，缺少独立覆盖审计，且宿主身份/Skill 加载证据不足，不能得出严格 A/B 质量比较或 Skill 增益结论。人工 precision、recall、clean-case false alarms 和 wrong-resolution 仍为 null。先完成人工标注、补全元数据并审计 coverage，再重复同快照 A/B；上下文选择器 C 组只有在 PR4 实现且上下文缺口有证据支持后再排入。WorkBuddy 暂不可用时使用其他宿主不构成 WorkBuddy 验收。此任务不加入 Promptfoo 运行时、模型 API 接入或大规模评测框架。

### PR4：上下文候选选择器

- **范围：** 在给定工作区/暂存区/分支快照内，为接口变更、授权迁移、删除/重命名、数据结构变化及异步/资源生命周期变化生成带理由的候选调用方、契约、测试和配置；预算不足、检索失败及未读取候选须显式保留。
- **验收：** 对暂存审查只基于所选暂存快照；跨文件契约和合法鉴权迁移案例列出关键候选；遗漏不能计为覆盖；选择器实现后，在 PR3 固定的同宿主案例中增加 C 组，与 B 组比较发现、误报、覆盖和人工干预变化，并保留 A 组直接审查基线。
- **未覆盖边界：** 候选不是完整调用图，也不能证明候选已被宿主读取；首版不承诺所有语言的 AST/符号解析，不按候选列表推断语义结论。

### PR5：历史报告与发布规划

- **范围：** 为 finding 记录跨轮次事件、快照及证据引用；将已校验结果转换成可发布位置计划，区分可落在当前 diff 的行内 finding 和必须进入顶层报告的 finding。
- **验收：** 部分复查不能清除旧 finding；修复、持续、无法确认和再次出现均能追溯到当轮结果及证据；发布位置失败、权限不足或旧 SHA 不能使 finding 丢失；重复生成计划具有幂等键或可识别更新目标。
- **未覆盖边界：** 本任务先做历史和平台中立的位置规划；GitHub PR 获取、评论写入、权限配置和重试由后续 PR 接入任务另行验收。不能把删除行或非 diff 行 finding 从完整报告过滤掉。

## 推进顺序与门槛

PR1 的本地验收与远端四矩阵均已通过；PR2 本地验收和 Codex 新会话恢复已通过，WorkBuddy 验收仍待执行。PR3 的 strict schema guard 已通过二次 review：生产 schema 接受，运行时 `true`/`{}` 反例均被拒绝，新增 5 项复现测试通过；联合全量为 358 passed、4 skipped，Semgrep 专项补跑 9 passed。当前零记录 closure export 的 Limited GO 仅覆盖该工件，不改变 v3 `closed_incomplete` 状态。下一步冻结并执行新的 12-slot workflow-only batch；它只验证运行流程，不产生质量或 Skill 增益结论。旧 pilot 的 human labels、host/model/Skill-load 元数据和 coverage audit 仍是质量评估的独立门槛。PR4、PR5 继续排队。

当前可发布的结论是：PR1 远端提交 `e8c719b635067ac33cca7300ffe8a4584ded3961` 的 Standalone Skill run 38198341019 四矩阵通过；ab-pilot integrity run 38198341075 通过，但只覆盖工件完整性。PR3 v3 closure summary/evidence/check 已公开，SHA 见 [评测输入说明](../eval/README.md)；`eval_reliability_review` 对当前真实零记录 closure export 给出 Limited GO，确认封存 source 复算、summary 实际 SHA 与 check 相互一致，但结论仅覆盖零记录工件、不能外推至非零 summary，也不改变 v3 `closed_incomplete` 的 1 failed/11 unstarted、0 records、0 pairs 结果。schema 二次 review 正式 GO：生产 schema 通过，`tool_runs.items` 为 `true` 和 `{}` 的实际输入分别拒绝，5 项复现测试通过。closure export 30 项相关 tests、tool projection 84 项定向 tests、联合全量 358 passed/4 skipped 均通过；Semgrep 专项补跑 9 passed，Ruff、`pip check`、`git diff --check` 及 v3/v2/pilot exporter checks 通过。4 个全量跳过项为默认 PATH 下缺少 Semgrep executable 的 3 项和缺少 `TEST_DATABASE_URL` 的 PostgreSQL 集成测试。下一步冻结并执行新的 workflow-only batch；人工标签仍 pending、质量仍 null，模型身份和成本 unknown；WorkBuddy 仍为 `not_run`。没有 v4 产物或执行。PR4/PR5 未分派。固定 `0.2.1` 发布包未重建，ZIP/manifest 哈希仍分别为 `42948cb0de941299c5e6ed39628acaeadd22bda26e0fbcefa0804460d05b74aa` 和 `5d8f6febfedf6532bc985b7d638099cacc7f99fdd408902c696c045135471ecf`。
