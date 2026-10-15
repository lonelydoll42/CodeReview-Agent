# 后续发布任务

本文把第三方项目的设计建议转成当前仓库可验收的任务。参考项目只提供设计线索，不代表本仓库已经具备对应能力。原方案远端基线为 `e8c719b635067ac33cca7300ffe8a4584ded3961`；v4 公开 export 固定在提交 `25d35c6`。本表中的完成状态须经主控检查实现和测试后再更新。

## 当前基线

- `review-changes` 0.2.1 已提供本地变更收集、证据校验、报告导出和修改后复查；复查脚本仍只做证据与状态约束校验，不代替宿主的语义判断。
- PR1 的本地 hermetic fixture、包外 smoke 和三 scope 验收已通过；相关测试为 225 passed、4 skipped。远端提交 `e8c719b635067ac33cca7300ffe8a4584ded3961` 的 [Standalone Skill run 38198341019](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38198341019) 中 Linux/Windows × Python 3.10/3.11 四项全部通过。此前提交 `116e47e3ff62ef7213f48e81849392a37dfed72b` 的 CI 结果保留为历史记录。
- PR2 核心、Skill、统一入口、固定历史资源和 Codex 新会话恢复验收已通过；Skill 资源 `latest` 链接漂移 P1 已修复并独立复验，核心定向测试为 39 passed。首次恢复命令因 `python` 不存在返回 127，改用 `/usr/bin/python3 -S` 后通过。
- PR3 的宿主中立输入契约、run-record schema、11 个真实案例的 23 次 A/B pilot 和便携报告已完成；旧 A/B 的人工标签、模型/宿主/Skill 加载元数据、独立覆盖审计和严格可比条件仍不齐。2026-10-14 对十条历史冻结快照完成 agent 替代判定（9 supported、0 unsupported、1 unresolved），不改变人工指标或严格 A/B 状态；当前重心是日常 Skill 使用反馈。旧 pilot integrity [run 38198341075](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38198341075) 通过，只验证公开工件完整性。
- workflow-validation v2 批次失败，详情见 [v2 失败报告](../eval/results/2026-10-12-ab-workflow-v2.md)。v3 (`2026-10-12-ab-workflow-validation-v3`) 经两个独立 GO 后正式启动，现为 `closed_incomplete`：1 个 slot 启动并失败、11 个暂停无 retry、0 host/evidence、0/6 pairs；严格 build 为 0 records，validate/score/summarize 为 NA。Codex 拒绝开放的 `properties.tool_runs.items` schema，wrapper exit 1 且无有效 JSON；trace 11198 的 `pread64` 返回裸 `?`，结果不可得且 trace incomplete。partial seal `accepted` 只覆盖 98 个一致引用，11 个 slot result 不可用。私有报告在仓库外，SHA-256 `5e4ccf3b366859d9139235c8861454d18e729fc55f51dd0abc98ec63295559a7`；snapshot-after SHA-256 `e0908e2057bf6fb91d988b1f3b86342af49d6593afe08b4a65d5f0bf3b3c755d`。closure export 已集成：[summary](../eval/results/2026-10-12-ab-workflow-validation-v3-summary.json) SHA-256 `24aef235909d8c554b508fbbb20519742933789477cd5073357bc21608f01e5a`、[evidence](../eval/results/2026-10-12-ab-workflow-validation-v3-evidence.json) SHA-256 `05a706ff69abeb76c6c6b417d89a1fdefdc0bc538454f5fb49dbd50f226d293f`、[check](../eval/results/2026-10-12-ab-workflow-validation-v3-check.json) SHA-256 `4a424c4e79197cbc2efeebe9240863054e81a048085f2627d0de727957a798eb`。`eval_reliability_review` 对当前真实零记录 closure export 给出 Limited GO，确认封存 source 复算、summary 实际 SHA 与 check 相互一致；此 GO 仅覆盖该零记录结果，不适用于非零 summary。schema 二次 review 正式 GO：生产 schema 通过，`tool_runs.items` 的 `true` 和 `{}` 实际输入分别被拒绝，新增 5 项复现测试通过。closure export 的 30 项相关 CI tests 和 tool projection 的 84 项定向 tests 通过；联合全量为 358 passed、4 skipped（3 个 Semgrep 测试在默认 PATH 下未找到 executable、1 个 PostgreSQL 测试缺少 `TEST_DATABASE_URL`），将 `.venv/bin` 加入 PATH 后 Semgrep 专项 9 passed。Ruff、`pip check`、`git diff --check` 及 v3 closure、v2 workflow、历史 pilot exporter checks 均通过。v3 批次仍为 `closed_incomplete`，其旧结果保持原样；v4 批次另见下文。
- v4 (`2026-10-12-ab-workflow-validation-v4`) 已在提交 `25d35c6` 生成公开 export，执行状态为 `closed_incomplete`：12 个 planned slots 中 1 个 launched、11 个 unstarted；evidence-valid slots 为 0、eligible pairs 为 0，封存 1 条 run record 和 1 个机械 score 文件。build、validate、score、validate、summarize 均报告通过，export check 的 5 项绑定/投影检查均为 `verified`；这些只验证处理流程与工件一致性，不说明证据有效、finding 正确或质量达标。strict summary 的 3 条 paired-case 行与 5 个缺失 `(case_id, condition)` 组合是摘要记录口径，不等于 11 个 unstarted slots。批次复用了既有语料，仅验证 workflow reproducibility，不是 fresh blind test；人工 labels pending，人工质量指标为 null，host/model/provider 身份与成本 unknown。grading index 在首个 slot 启动后才 seal（`pre_first_model=false`）；原始 `blocked_label_reference_present` 是保守的路径引用启发式，不证明标签内容曾被读取。
- v4 使用 source commit `c80ab00` 和 runtime normalizer/parser v3。parser v6 的独立 GO 只适用于未来新工件，不改写 v4/v3/pilot 旧产物；复现旧 trust/sealer/scoring 应使用 `c80ab00` 基线。closure export check 验证哈希固定的公开投影，不要求当前主分支源码保持不变。v4 三份公开文件及 SHA 见[评测输入说明](../eval/README.md)：[summary](../eval/results/2026-10-12-ab-workflow-validation-v4-summary.json) `a01af725b67b7116c8731954c98536d0a169037c86bad103ef6a66e7ced69732`、[evidence](../eval/results/2026-10-12-ab-workflow-validation-v4-evidence.json) `c27f808b9c2491efe328811223dade78053ea3e9f746eadaa41afdda1612fe14`、[check](../eval/results/2026-10-12-ab-workflow-validation-v4-check.json) `4ebd68195a205d3c0de478597fa7387efdf6c76d6fe7d61f44fb0426f5032112`。
- 当前三个 seed 是验收准备器生成的合成隔离夹具，不是真实项目盲测。真实 WorkBuddy 验收仍为 `not_run`，PR 输入接入和评论发布尚未开展。
- 2026-10-09 的独立 Codex 前向运行在合成 case-01 上生成 1 条 `confirmed` finding，首轮 finalize 成功；这只证明一次本地流程前向运行，不是 WorkBuddy 验收、盲测或 Skill 增益比较。私有操作记录未随仓库发布；公开验收边界见[WorkBuddy 验收指南](workbuddy-acceptance.md)。
- 旧的 `eval/metrics.py` 与 `eval/run_eval.py` 面向服务端 `Finding`/`pr_url` 数据；它们不消费新契约下的 23 次 A/B pilot 运行，也不覆盖覆盖率、失败状态、复查判定或成本。新 run records 构成首批宿主运行集，但因人工标注和严格可比条件未齐，仍不能声称 Skill 质量已被评测。

## 发布任务

| 任务 | 状态 | Owner | 依赖 |
|---|---|---|---|
| PR1 自动验收基础 | 本地 fixture、包 smoke 和定向测试通过（225 passed、4 skipped）；远端 `e8c719b635067ac33cca7300ffe8a4584ded3961` 的 Standalone Skill 四矩阵全过 | `skill_ci` | 当前打包器和 Skill 测试 |
| PR2 统一入口与会话记录 | 本地定向测试、固定历史资源和 Codex 新会话恢复通过；WorkBuddy 未实测 | `review_session` | 当前 `review_core` 输入、报告和复查契约 |
| PR3 宿主中立评测基线 | v3、v4 均 `closed_incomplete`；v4 后处理与公开 export check 通过，但 evidence-valid slots 和 eligible pairs 均为 0，不构成质量结论 | `eval_tasks` | 完成人工 labels、宿主/模型元数据与独立 coverage audit；需要新执行时单独固定版本和范围 |
| PR4 上下文候选选择器 | 排队中（queued），未分派 | 未分派 | PR3 可比较的基线；稳定的快照输入 |
| PR5 历史报告与发布规划 | 后置排队（queued），未分派 | 未分派 | PR2 会话关联；稳定的校验结果与复查契约 |

### PR1：自动验收基础

- **范围：** 将 Skill/package 验收作为独立 CI job；从仓库外运行打包产物，覆盖 Linux 与 Windows 的包内脚本和路径行为。
- **验收：** job 不依赖 API 服务启动；安装/构建产物在独立项目中可导入和执行；工作区、暂存区、差异及中文/空格路径用例结果可追溯；常规测试和打包检查通过。
- **当前进度：** 本地 hermetic fixture、包外 worktree/staged/branch smoke 与 start/resume 验收通过，相关测试为 225 passed、4 skipped。代码 SHA `116e47e3ff62ef7213f48e81849392a37dfed72b` 的 [远端 run](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329) 于 2026-10-09 完成，Ubuntu/Windows × Python 3.10/3.11 四个 job 均成功：[Ubuntu 3.10](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329/job/113425907617)、[Ubuntu 3.11](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329/job/113425908062)、[Windows 3.10](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329/job/113425908039)、[Windows 3.11](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/37810529329/job/113425907970)。2026-10-14 的 UTF-8 启动检查修补绑定源码 SHA `4a0108e4409af9b8ba65800a106bea69ebcca41c`；本地全套测试为 440 passed、3 skipped（两项 adapter validation、一项因未配置 `TEST_DATABASE_URL` 的 PostgreSQL 测试），Ruff 与 diff check 通过。该精确 SHA 的 [Standalone Skill run 38437603914](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38437603914) 四矩阵全过。该 SHA 没有 GitHub Export Integrity run（改动路径不匹配 workflow filter），对应检查仅本地通过。
- **未覆盖边界：** CI 不能证明 WorkBuddy 能导入 Skill、运行工具或完成跨会话复查；这些仍需真实宿主记录。

### PR2：统一入口与会话记录

- **范围：** 让一个 Skill 入口编排范围固定、上下文收集、工具状态、语义审查、结果校验和导出；增加轻量 session 记录，以仓库身份和内容指纹关联输入、结果草稿、校验结果及后续复查。
- **验收：** 中断后可定位已有证据；恢复时核验仓库、范围和快照指纹，内容改变即形成新快照；不同仓库/worktree 的会话不串用；流程完成状态与 `completed/partial/uncovered/failed` 审查状态分开；语义未完成的草稿不会伪装成已覆盖结论。
- **当前进度：** 核心、Skill、统一入口和固定历史资源本地验收通过；latest 漂移 P1 已修复并独立复验，39 项定向测试通过。Codex 在独立 case-01 工作目录完成 review/finalize，并在新会话中只凭已保存 session 与包恢复成功，未应用准备补丁。首次恢复调用使用 `python -S`，因命令不存在返回 127；改用 `/usr/bin/python3 -S` 后通过。真实 WorkBuddy 安装与运行仍未执行。
- **2026-10-14 历史代码副本闭环验收：** 使用源码修订 `4a0108e4409af9b8ba65800a106bea69ebcca41c` 的开发验收包，在隔离的历史代码副本上完成一轮完整 Skill 审查与复查：从 `59ddba937ea45b95f79dc8d0c77d955c67e3b812` 到 `e8d0788fc58fc2be18d7b1f65410654d6a5f5233` 的初审确认两项 finding；无关文档提交 `8509abeecac39721c13af992b51434b22679ba29` 后两项均为 `persisting`；实际修复提交 `8949f4b9b6d8f3eed9ae3013e0d23e0bfb5f5d99` 后复查两项均为 `resolved`，review 状态为 `completed`、无新 confirmed finding。该轮 13 个选中文件均完成语义检查且没有本轮 coverage gap；Semgrep 适用并扫描其中 6 个 Python 文件，另 7 个文件不在规则范围，3 条路径构造提示仍是未确认候选。这只描述本轮选中范围和工具适用范围，不保证仓库其他代码无问题。该修复提交只存在于历史副本，不是当前主仓的内核改动；本轮主仓源码修订仍为 UTF-8 启动检查修补 `4a0108e4409af9b8ba65800a106bea69ebcca41c`。开发包 27 项资源与安装清单及 ZIP 一致，ZIP SHA-256 为 `c8229f375b1d336df5c0ba90a3f552172b4745937fd36088f58dad8e2ae5f81c`；完整测试为 187 passed、5 skipped。包内 `VERSION=0.2.1` 是继承值，不代表重发正式旧包；这次 Codex 本地演练也不等于 WorkBuddy 验收。
- **未覆盖边界：** 不在此任务中引入队列、数据库、服务端任务恢复或模型调用层；会话记录不能替宿主证明 finding 正确。

### PR3：宿主中立 A/B 评测基线

- **范围：** 使用 [评测输入契约](../eval/README.md)、[seed 索引](../eval/data/seed-cases.json) 和 [run-record schema](../eval/schemas/run-record.schema.json)，分开保存宿主原始输出、oracle/人工标注及脚本判分。先在同一宿主和固定案例上比较直接审查 `same_host_direct` 与当前 Skill `current_skill`；上下文选择器条件 `context_selector_skill` 在 PR4 实现后追加，不作为启动 A/B 的前置条件。首批目标为 10–12 个有独立标注的真实案例，再逐步扩展。
- **验收：** 每个运行有唯一条件、宿主/环境、确切快照指纹、原始转录和导出路径；标注不进入宿主输入；失败、未覆盖、工具缺失和人工介入单独保留；脚本分数可由保存的输出和标注重算；未知模型、token、成本写 `null`，不能写成零。
- **当前进度：** 11 个真实案例、23 条旧 A/B run 保持原样；A 有 12 次尝试/11 次完成，B 有 11 次尝试/11 次完成，23/23 raw host-output hash 与原记录一致。旧记录缺少同期 `input.review_manifest_sha256`，strict replay blocked；Agent grader v2 只提供暂定证据，不是 human recall。v3 仍为 `closed_incomplete`（1 failed、11 unstarted、0 records、0/6 pairs）。v4 也为 `closed_incomplete`：12 planned、1 launched、11 unstarted，0 evidence-valid slots、0 eligible pairs，1 run record 和 1 个机械 score；strict build/postprocessing 与公开 export 的 5 项校验通过，但不构成质量结论。v4 是既有语料上的 workflow reproducibility 执行，不是 fresh blind test；3 条 strict paired-case 行和 5 个 missing `(case_id, condition)` 组合不等于 11 个 unstarted slots。旧 A/B 的 human labels 未补齐，人工质量指标仍为 null，host/model/provider 与成本 unknown。grading index 在首个 slot 启动后才 seal（`pre_first_model=false`）；`blocked_label_reference_present` 为保守路径引用启发式，不证明标签内容被读取。此外，2026-10-14 对十条历史冻结快照完成独立于 Agent grader v2 的最终 agent 替代判定：9 supported、0 unsupported、1 unresolved（`p0007`）；重复根因组为 `p0004/p0005` 与 `p0008/p0009`，属于相互重叠的预测标签，不计为独立根因。该统计是预测记录的 agent 判定，不是准确率、召回率或独立缺陷数；证据范围与未外部复验边界见[历史 agent 判定归档](validation/2026-10-14-agent-adjudication.md)。历史结果包 SHA-256 为 `e824c59684d452a46efddf974dd93173a4a772a942a3386e3eb11fb47b770ef9`；本次归档汇总 JSON SHA-256 为 `c200e7a83de0a751c1ec0d074b49338d5b21c47f1d8d03bf86989e078559b98e`。原 human 表的 `PENDING` 仅作为历史输入保留；本次 agent 判定不冒充人工标签，也不填充人工指标、有效 A/B、召回率或 Skill 增益。该汇总仅描述历史冻结快照；严格 A/B 仍为 0 个有效配对且暂停。三份旧 export 和哈希见[评测输入说明](../eval/README.md)。
- **未覆盖边界：** 旧 11-case/23-run A/B 缺少人工标注、认证盲测、独立覆盖审计及足够的同期宿主/模型/Skill 加载元数据，不能得出严格 A/B 质量比较或 Skill 增益结论；人工 precision、recall、clean-case false alarms 和 wrong-resolution 继续为 null。严格 A/B 当前暂停，agent 替代判定不改变这一状态；当前工作重心是日常 Skill 使用反馈。上下文选择器 C 组仍需 PR4 实现及上下文缺口证据后另行评估。此任务不加入 Promptfoo 运行时、模型 API 接入或大规模评测框架。

### PR4：上下文候选选择器

- **范围：** 在给定工作区/暂存区/分支快照内，为接口变更、授权迁移、删除/重命名、数据结构变化及异步/资源生命周期变化生成带理由的候选调用方、契约、测试和配置；预算不足、检索失败及未读取候选须显式保留。
- **验收：** 对暂存审查只基于所选暂存快照；跨文件契约和合法鉴权迁移案例列出关键候选；遗漏不能计为覆盖；选择器实现后，在 PR3 固定的同宿主案例中增加 C 组，与 B 组比较发现、误报、覆盖和人工干预变化，并保留 A 组直接审查基线。
- **未覆盖边界：** 候选不是完整调用图，也不能证明候选已被宿主读取；首版不承诺所有语言的 AST/符号解析，不按候选列表推断语义结论。

### PR5：历史报告与发布规划

- **范围：** 为 finding 记录跨轮次事件、快照及证据引用；将已校验结果转换成可发布位置计划，区分可落在当前 diff 的行内 finding 和必须进入顶层报告的 finding。
- **验收：** 部分复查不能清除旧 finding；修复、持续、无法确认和再次出现均能追溯到当轮结果及证据；发布位置失败、权限不足或旧 SHA 不能使 finding 丢失；重复生成计划具有幂等键或可识别更新目标。
- **未覆盖边界：** 本任务先做历史和平台中立的位置规划；GitHub PR 获取、评论写入、权限配置和重试由后续 PR 接入任务另行验收。不能把删除行或非 diff 行 finding 从完整报告过滤掉。

## 推进顺序与门槛

PR1 的历史验收与远端四矩阵通过；主仓源码 `4a0108e4409af9b8ba65800a106bea69ebcca41c` 的 UTF-8 修补本地全套测试为 440 passed、3 skipped，Ruff 与 diff check 通过，精确 SHA 的 [Standalone Skill run 38437603914](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38437603914) 四矩阵全过。此 SHA 未触发 GitHub Export Integrity run，仅本地检查通过。PR2 的历史代码副本闭环演练已记录在上文；真实 WorkBuddy 安装与运行仍未执行。PR3 的 strict schema guard 历史验证通过；v3/v4 workflow-only 批次均保持 `closed_incomplete` 且 0 evidence-valid、0 eligible pairs，流程与 export checks 不构成质量结论。parser v6 独立 GO 只用于未来的新工件；PR4、PR5 继续排队。

截至 2026-10-14，本轮可发布结论是：UTF-8 修补绑定主仓源码 SHA `4a0108e4409af9b8ba65800a106bea69ebcca41c`；本地 440 passed、3 skipped，Ruff 与 diff check 通过，[Standalone Skill CI](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38437603914) 四矩阵成功。该 SHA 没有 GitHub Export Integrity run，只有本地检查通过。十条历史冻结快照的 agent 替代判定为 9 supported、0 unsupported、1 unresolved（`p0007`）；agent 不冒充真人，不补写人类指标、有效 A/B、召回率或 Skill 增益。严格 A/B 仍暂停，0 个有效配对；当前重心是日常 Skill 使用反馈。开发验收包基于该源码 SHA，27 项资源均与 ZIP/清单一致，且演练前后 ZIP SHA 相同；首次完整真实审查/复查闭环发生在隔离历史代码副本，副本中的修复提交 `8949f4b9b6d8f3eed9ae3013e0d23e0bfb5f5d99` 不属于主仓改动，也不表示当前主仓内核新增修复。

正式旧 `0.2.1` 发布包保持原有 ZIP/manifest 哈希 `42948cb0de941299c5e6ed39628acaeadd22bda26e0fbcefa0804460d05b74aa` 和 `5d8f6febfedf6532bc985b7d638099cacc7f99fdd408902c696c045135471ecf`。UTF-8 修补另有开发验收包：ZIP SHA-256 `c8229f375b1d336df5c0ba90a3f552172b4745937fd36088f58dad8e2ae5f81c`、资源树 SHA-256 `d983ad94bb2878e2cd2fc15d6021beeb90ed8141b6365809423df4468e7a5cab`；包内 `VERSION=0.2.1` 为继承值，该包不是正式旧包、不是重发，也不替换原有正式发布 fixture。

## 当前小范围试用准备

截至 2026-10-14，`0.3.0-rc.1` 小范围试用预发布已发布，tag `review-changes-v0.3.0-rc.1` 指向 source A `956439ea9c08afaffa4084473a19cc7c98cc521c`。独立包包含 27 项资源；ZIP SHA-256 为 `cc17d9cc9270613fe84bd86a876f09a0c7a92918f928d08f8d0786edcaf97bed`，manifest SHA-256 为 `4ea7eca2cc18e63a0906d95b331464b7cf8b9b4d76a42bc9d58d12779070d87f`，manifest 与 ZIP 的哈希及逐资源清单匹配。发布后从 GitHub 下载的 ZIP、manifest 和 checksum 与本地验收资产逐一同哈希；ZIP 在中文空格目录解包后，以 `python3 -X utf8 -S` 单独运行 `project_tool_runs.py`，向其输入空 `tool_runs` JSON 得到 `[]`。无 `-X utf8` 且禁用 `PYTHONUTF8` 的独立测试以退出码 2 输出 UTF-8 guard 提示，未写文件。该 smoke 只验证包文件与启动边界，不代表真实外部用户或语义质量。

source A 的打包测试为 5 passed，`test_ab_batch.py` 为 37 passed、2 skipped；相关补丁的 3 项定向测试、Ruff、diff check 通过。[Standalone Skill CI run 38506803679](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38506803679) 的 Ubuntu/Windows × Python 3.10/3.11 四矩阵均通过。试用下载和导入步骤见[试用指南](skill-trial.md)。正式旧 `0.2.1` fixture 未变，ZIP 与 manifest SHA-256 分别为 `42948cb0de941299c5e6ed39628acaeadd22bda26e0fbcefa0804460d05b74aa` 和 `5d8f6febfedf6532bc985b7d638099cacc7f99fdd408902c696c045135471ecf`。

2026-10-15 完成一次本地历史样本工作流试用：初审报告确认 1 条 P2，未跟踪文件负向对照 recheck 为 `persisting`，隔离副本修复后的 recheck 为 `resolved`。过程、来源、包哈希和实际命令摩擦见[本地试用摘要](validation/2026-10-15-local-trial.md)。默认首页入口经 [PR #12](https://github.com/lonelydoll42/CodeReview-Agent/pull/12) 合并，merge commit 为 `be199b5028acb27c295830a245f32859f169a88f`；Python 3.10/3.11 两项必需 CI 在 head `16e3a10` 均通过，试用指南固定于 [`b5b7161`](https://github.com/lonelydoll42/CodeReview-Agent/commit/b5b71610a531c755b252ceb2b2b9e1c29d7be483)。该样本来自已知历史提交，不是盲测、独立外部用户试用或质量增益结论，也不表示当前主分支存在该缺陷。

同日另完成首次宿主调用检查：Codex CLI `0.153.4` 的显式调用与自然语言调用均有 native Skill-load 证据；两份报告均为 `partial` 且无确认发现，共享远端 GitHub 证据缺口。Catalog picker 未测试；该检查不是外部试用或质量比较，且未重跑历史样本。详见[首次调用检查](validation/2026-10-15-first-use.md)。

严格 A/B 仍暂停，0 个有效配对；真实外部试用反馈尚未收集，WorkBuddy 导入和运行也尚未验收。历史 agent 替代判定仍为 9 supported、0 unsupported、1 unresolved（`p0007`），不代表准确率、召回率、独立缺陷数或 Skill 增益。下一步是收集具体项目首次审查和修改后复查的使用反馈，不扩展 trace 或评测范围。
