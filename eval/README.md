# 宿主中立评测输入

本目录的评测契约用于记录同一宿主在不同审查条件下的实际行为。它不启动模型、不声称现有 Skill 已通过 WorkBuddy，也不把仓库单元测试当成宿主质量分数。

## 现有材料与边界

- [seed-cases.json](data/seed-cases.json) 索引 `prepare_workbuddy_acceptance.py` 生成的三个合成隔离夹具：缺失记录缺陷、行为保持的鉴权迁移、跨文件返回契约变化。它们适合检查导入、流程和报告/复查行为，不是来自真实项目的盲测。
- 每次准备器运行会将 host 产物留在其输出目录的 `host-artifacts/case-NN/`，将 operator oracle 放在 `operator/oracle.md`，并将项目测试和外部探针记录留在 `validation/`。遵循 [WorkBuddy 验收单](../docs/workbuddy-acceptance.md)：oracle、探针和准备补丁不能进入宿主提示；目前宿主结果为 `not_run`。
- 旧的 `eval/metrics.py` 读取 `pr_url`/`human_findings` 数据，并依赖服务端 `agents.base.Finding`；只按文件、类别和行号容差匹配 finding。它不读取本目录的宿主运行记录，也不覆盖状态、覆盖、失败、复查误判或成本。保留原命令兼容旧数据；本契约不把它描述为 Skill 质量评测器。

## A/B 基线与后续 C 组

先用同一宿主、同一批真实变更案例建立 A/B 基线。第一批建议 10–12 个案例，覆盖已知缺陷、跨文件契约、删除引入的问题和合法重构；答案由独立标注者保存，不进入宿主输入。建立 A/B 基线不需要等待上下文候选选择器，也不需要先凑齐三组。

每个案例可按以下条件记录；先执行 A、B，C 在上下文选择器实现后作为后续增量：

| `condition` | 输入方式 |
|---|---|
| `same_host_direct` | A：同一宿主，不加载本项目 Skill |
| `current_skill` | B：同一宿主加载当前固定版本的 Skill |
| `context_selector_skill` | C：同一宿主加载加入上下文候选选择器的 Skill；选择器实现前不运行 |

固定仓库快照、审查范围、宿主版本和模型配置；保存其指纹与实际运行条件。若宿主、模型或输入配置不同，分开记录，不能汇总成同条件比较。WorkBuddy 暂时无法连接时，可先用能够执行两种条件的其他同一宿主完成 A/B，并如实记录宿主；这不构成 WorkBuddy 验收。2026-10-09 已完成 11 个真实案例、23 次保存运行的试跑，见下文；它没有人工质量标签，不能报告 Skill 精度或召回率。C 仍未运行。

## 2026-10-09 A/B 试跑

[试跑报告](results/2026-10-09-ab-pilot.md)与[可移植 summary](results/2026-10-09-ab-pilot-summary.json)记录了 23 条尝试、重试历史、宿主身份限制、快照检查和协议事件；[证据索引](results/2026-10-09-ab-pilot-evidence.json)包含逐次运行摘要、原始 host-output SHA-256 与匿名 agent 判定投影。原始 host-output、冻结 oracle 和完整私有映射保留在仓库外，导出文件不包含它们。

这批运行中 A 完成 11/12 次，B 完成 11/11 次；这些是流程结果，不是质量分数。8 组输入匹配但宿主身份不完整且 Skill 加载未验证，2 组因宿主自报名称不一致而不比较，case 10 因 digest 匹配的协议违规事件而列为 inconclusive。该 incident 的通用 actor 为 `agent`；运行 agent 在 review 中覆盖模板，协调/finisher agent 在 review 结束后从验证过的运行前副本恢复模板。Agent grader v2 的 A 4 条 supported/1 条 unresolved、B 3 条 supported/2 条 unresolved 仅是暂定证据；两个冻结候选都有 agent prediction 覆盖，但不能当作 human recall。没有人工标注，因此人工 precision、recall、clean-case false alarms 和 wrong-resolution 仍为 null。案例集尚未认证为盲测，报告也不代表 WorkBuddy 验收。

**B0 历史检查（不是本轮严格重评分证据）：** 当时曾用标准库路径 `python -S` 从受控输入重建 23 条 run record 与分组 summary，并记录 23/23 records 通过 JSON Schema、239 passed/4 skipped。那些历史 `score_record`/`summarize` 成功结果走过当前副本 hash 兼容路径，不能视为严格历史重评分；23 条记录缺少同期 `input.review_manifest_sha256`，新的 strict scorer 会拒绝这些旧 runs。本轮仅做离线历史导出与盲审准备，没有调用模型、没有重跑 A/B，也没有为 23 条历史运行创建新的有效评分；人工盲审仍 pending。case 10 的恢复角色/阶段是附加说明，不增加 run event 或干预计数。

B0 evidence 中 `runs[].artifacts.run_record_sha256` 与 `score_sha256` 没有可查的生成或规范化定义。将这些投影与本轮归档的 23 个 run-record、23 个 score 文件原始字节逐项比较，分别匹配 0/23 和 3/23；此结果不支持推断差异何时、由谁或因何产生。独立核对的 b0 raw-output expected hashes 与 23 条原始 run records 的 `raw_host_output_sha256` 字段全部匹配。23/23 旧记录缺少 `input.review_manifest_sha256`，当前 reviewer-input hash 只能作为导出时审计值，不能回填为历史绑定；strict scoring 对 23 条旧运行全部 blocked。旧 score 仅保留，不作质量证据、replay cache 或有效重评分。历史 summary 中的 scoring-time `verified` 标为 source summary 的旧状态，当前严格重算为 `not_replayed`；`validated_result` 在 23 条记录中均为 null。

B0 导出中的 raw-output expected 独立匹配 23/23 原始 run records。外部 `run-score-original.tar`（SHA-256 `3562c789dfdcffd7a9abb9395cd41704ec5ce105c051db5b880038a5fed2a0e5`）及 46 条 source manifest 用于核验本轮保存的归档快照；它们证明的是该快照此后的完整性，不能证明这些字节就是 b0 发布时的原始字节。未来重建前仍须核验仓库外归档；导出 CI 不携带私有 run/score 文件，也不能独立证明外部来源完整性。CI 结论只覆盖公开 summary/evidence 的字节和内部一致性。

在 v3 启动前，pilot/export 补丁回归为 266 passed、4 skipped；独立 core 检查 32 项、exporter 检查 8 项、`python -S` 导出 `check` 和 4 个 tamper probes 均通过。较早的 v2 workflow 修复全量测试为 325 passed、4 skipped；3 个 Semgrep 测试因当时 `PATH` 未包含 Semgrep 而跳过，随后用现有 `.venv/bin` 补跑的 Semgrep 检查为 9 passed；另 1 个 PostgreSQL 测试因未设置 `TEST_DATABASE_URL` 跳过。独立重点回归为 54 passed。Ruff、`pip check`、`git diff --check` 和 v2 workflow exporter CLI `check` 均通过。这些历史检查覆盖实现、导出与会话流程，不评定 A/B 质量，也不覆盖后来的 v3 批次或当前待审修补。strict scoring 对 23 条旧运行仍全部 blocked，人工盲审仍 pending，也没有为这 23 条历史运行创建新的有效评分。

后续人工盲审的输入、冻结来源索引、空白模板和操作步骤见[盲审准备指南](results/2026-10-09-ab-pilot-blind-review.md)。这批材料只提供十条 condition-blind prediction 和 source entry；它们不含 agent verdict、oracle mapping 或 human labels，尚未执行人工盲审。覆盖审计仍需保留真实文件读取和工具调用轨迹；Skill bundle digest 只说明包身份，不能证明宿主实际读取或使用了它。

## 2026-10-12 workflow v2 失败与 v3 关闭状态

[workflow v2 失败报告](results/2026-10-12-ab-workflow-v2.md)记录了基于既有语料的 12-slot 流程复现尝试：1 个 slot 启动，0 个宿主完成，0 个证据有效，11 个未启动；没有生成宿主输出或 run record。seal 的 `accepted` 只表示依赖、冻结计划和 runtime-config 身份一致。artifact-reference 检查实际检查了 0 个引用，12 个 `slot-result.json` 不可用，因此 seal 完整性为 `incomplete`，不代表流程或质量通过。

首个 slot 失败的原因包括：provider 拒绝缺少 `type` 的 `schema_version` 输出 schema；argument 0 的外部二进制 symlink 使 artifact 枚举越界，ledger 错误路径随后重新抛出异常；18 个在成功关闭描述符后出现的 `close`/`EBADF` 事件又使 trace 变为 `unknown`。该批次来自既有语料，不是新盲测；人工标签仍 pending，质量指标仍为 null。完整细节和独立 ledger 核对见失败报告。

v2 的历史 projection check 记录为 `passed`，仅对应 v2 工件：[summary](results/2026-10-12-ab-workflow-v2-summary.json)、[evidence](results/2026-10-12-ab-workflow-v2-evidence.json) 和[check](results/2026-10-12-ab-workflow-v2-check.json)。重算所需的原始 source inputs 保存在仓库外的受控存储中，因此本仓库不提供依赖机器本地路径的重算命令。

Standalone Skill 的远端矩阵在提交 `e8c719b635067ac33cca7300ffe8a4584ded3961` 上通过：[run 38198341019](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38198341019)，Linux/Windows × Python 3.10/3.11 四项全过。旧 pilot 的完整性检查也通过：[run 38198341075](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/38198341075)；这验证保存工件的一致性，不产生质量结论。

### workflow-validation-v3 执行结果

`2026-10-12-ab-workflow-validation-v3` 在两个独立 GO 后正式启动。12 个冻结 slot 中只有首个 `case0002/A/rep1` 启动：Codex 拒绝了宿主 schema 中开放的 `properties.tool_runs.items` object，wrapper 以 exit 1 结束且未产生有效 JSON；其余 11 个 slot 暂停，没有重试。计数为 started 1、failed 1、unstarted 11、host-completed 0、evidence-valid 0、pairs 0/6。严格 build 得到 0 records，validate、score、summarize 均为 NA。v3 状态为 `closed_incomplete`。

trace 事件 `11198` 实际是 `pread64` 返回裸 `?`，不能解释成 `ERESTARTSYS`；对应结果不可得，trace 保持 incomplete。最终 partial seal 的 `accepted` 仅表示 98 个已检查引用一致；11 个 slot result 不可用，不能据此认定批次或证据完整。私有 failure report 位于仓库外，SHA-256 为 `5e4ccf3b366859d9139235c8861454d18e729fc55f51dd0abc98ec63295559a7`；`snapshot-after` SHA-256 为 `e0908e2057bf6fb91d988b1f3b86342af49d6593afe08b4a65d5f0bf3b3c755d`。

v3 closure export 已集成到仓库，公开投影文件及 SHA-256 如下：

- [summary](results/2026-10-12-ab-workflow-validation-v3-summary.json)：`24aef235909d8c554b508fbbb20519742933789477cd5073357bc21608f01e5a`
- [evidence](results/2026-10-12-ab-workflow-validation-v3-evidence.json)：`05a706ff69abeb76c6c6b417d89a1fdefdc0bc538454f5fb49dbd50f226d293f`
- [check](results/2026-10-12-ab-workflow-validation-v3-check.json)：`4a424c4e79197cbc2efeebe9240863054e81a048085f2627d0de727957a798eb`

共享工作区中的 strict schema/preflight 定向 26 项及 trace 定向 21 项通过；normalizer v3 保留 raw trace 的精确 SHA 绑定。schema 二次 review 正式 GO：生产 schema 验证通过，`tool_runs.items` 为 `true` 和 `{}` 的实际输入分别被拒绝，新增 5 项复现测试通过。`eval_reliability_review` 对 v3 零记录 closure export 给出 Limited GO：封存 source 复算、summary 实际 SHA 与 check 相互一致；该 GO 仅覆盖这份零记录结果，不能外推到非零 summary。closure export 的 30 项相关 CI tests、Ruff 和 CLI check 通过；tool projection 已完成，另有 84 项定向 tests 独立通过。联合全量为 358 passed、4 skipped：3 个 Semgrep 测试在默认 PATH 下找不到可执行文件，1 个 PostgreSQL 集成测试因未设置 `TEST_DATABASE_URL` 跳过；将 `.venv/bin` 加入 PATH 后，Semgrep 测试另行 9 passed。Ruff、`pip check`、`git diff --check` 以及 v3 closure、v2 workflow、历史 pilot exporter checks 均通过。这些结论不改变 v3 的 `closed_incomplete` 状态，也不构成质量或 Skill 增益结论。旧 `0.2.1` ZIP/manifest、v2 失败记录和 23 条历史 pilot 不变。

### workflow-validation-v4 closure 结果

`2026-10-12-ab-workflow-validation-v4` 是基于既有语料的 workflow-only 执行，不是 fresh blind test。最终状态为 `closed_incomplete`：12 个计划 slot 中 1 个启动、11 个未启动；evidence-valid slots 为 0，eligible pairs 为 0。封存了 1 条 run record 和 1 个机械 score 文件，但这不使该 slot 成为 evidence-valid，也不产生质量结论。严格后处理流水线 build → validate → score → validate → summarize 均通过；公开 export check 的 closure bindings、evidence bindings、source hashes、summary bytes、summary projection 五项均为 `verified`。这些结果验证流程、来源绑定和投影一致性，不代表证据有效、审查正确或 Skill 有增益。

strict summary 有 3 条 paired-case 行和 5 个缺失的 `(case_id, condition)` 组合；前者包括一个已观察 replicate 行和两个 case-level null 占位行，后者按 case/condition 汇总。这两种摘要记录不等于 11 个未启动的计划 slot，不能互相替代。人工 labels 仍 pending，人工质量指标为 null；host/model/provider 身份和成本均 unknown。原始 closure 的 grading index 在首个 slot 启动后才 seal（`pre_first_model=false`）；其 `blocked_label_reference_present` 是保守的路径引用启发式，不证明标签内容曾被读取，也不能作为预运行隔离已成立的证据。

v4 公开 export 固定在提交 `25d35c6`，source commit 为 `c80ab00`，runtime normalizer/parser 为 v3。后续 parser v6 的独立 GO 只适用于将来新工件，不回写或重解释旧 v4、v3、pilot 产物；要复现旧 trust/sealer/scoring，应使用 `c80ab00` 基线。closure export check 根据保存工件的哈希绑定验证公开投影，不要求当前主分支代码保持不变。公开文件与 SHA-256：

- [summary](results/2026-10-12-ab-workflow-validation-v4-summary.json)：`a01af725b67b7116c8731954c98536d0a169037c86bad103ef6a66e7ced69732`
- [evidence](results/2026-10-12-ab-workflow-validation-v4-evidence.json)：`c27f808b9c2491efe328811223dade78053ea3e9f746eadaa41afdda1612fe14`
- [check](results/2026-10-12-ab-workflow-validation-v4-check.json)：`4ebd68195a205d3c0de478597fa7387efdf6c76d6fe7d61f44fb0426f5032112`

### 2026-10-13 单案 A/B 尝试

本次在源码 `c49ec52970f61260bf14d97320ed18c056f6fcb5`、normalizer v10 下，仅授权 `case0006` 的 A/B replicate 1。实际启动 1 次宿主审查：A 完成输出，wrapper exit code 为 0，但 `evidence_valid=false`；B 和其余 10 个槽位未启动。失败后停止，没有重试或进入评分，证据有效配对仍为 0。此记录独立于旧 v4 closure，不构成审查质量或 Skill 增益结论；10 条历史匿名预测的人工判定仍为 `PENDING`。

失败涉及请求路径与 trace 返回路径的差异，以及 namespace PID 身份和唯一命令关联证据不足。路径字符串、PID 数值一致性和会话级 `thread_id` 均不能补足这些证明。现有产物不足以核实运行时别名，不能用事后主机状态、路径字符串规则或新解析器将旧 A 追认为有效。

独立复验确认冻结输入文件、源码依赖和 case0006 Git index 未漂移，首次启动和 A 后的 checkpoint 及 sidecar 均已保存。原产物和诊断保留在仓库外，没有新的公开评分导出。GO receipt 引用的一份临时预检报告已被清理，无法复验其原字节；独立最终核验报告和真实首次启动报告仍保留，此缺口不回填。

## 记录和产物

每个条件/案例各有一份符合 [run-record.schema.json](schemas/run-record.schema.json) 的 `run.json`。路径相对该记录文件所在目录，实际产物分开放置：

```text
<run-dir>/
  run.json
  artifacts/
    host-transcript.txt       # 宿主原始对话/输出，不经整理
    host-output.md            # 宿主原样交付的报告草稿（若单独存在）
    review-manifest.json      # 该次运行实际使用的快照
    validated-result.json     # 包校验后的结构化结果
    review.md                 # 确定性导出的审查报告
    recheck.json              # 复查脚本输出（若运行）
    recheck.md
  annotations/
    oracle.json               # 独立预期行为、测试和已知问题
    labels.json               # 人工逐条判定、歧义和标注者
  scores/
    score.json                # 评分脚本、版本、输入指纹和输出
```

原始宿主输出、oracle/人工标注、脚本评分必须各自保留；不要把宿主原话改写成“原始输出”，也不要将 oracle 填入宿主可见输入。数据若不能随仓库分发，可保留受控外部路径和内容哈希；缺失文件要记录为缺失，不能用空文件代替。

`run.json` 应完整记录一次运行的条件、宿主/环境、输入指纹、执行状态、未覆盖路径、上下文遗漏、人工介入及产物引用。某个工具执行失败应有失败状态和原因，不能记成成功空结果；未执行、失败或覆盖不足不能当作“没有发现问题”。未知 token 数、成本、耗时或介入数使用 JSON `null`，不以 `0` 代替未知值。

脚本判分读取已保存的结构化结果和独立标注，并把评分输出写入 `scores/`。结果消失本身不算修复；复查需单独计入 `resolved`、`persisting`、`unverified`，并检查错误解决数。判分方案至少记录有效发现、误报、漏报、证据/覆盖状态、结果校验失败、运行失败及人工介入。对存在未覆盖路径的运行保留其覆盖缺口，不应将缺失预测等同为干净审查。当前旧 `run_eval.py` 不能独立完成这些判分，本轮没有添加模型调用或新评分引擎。

下面的记录只展示字段形状，所有状态均为尚未运行；它不是宿主结果，也不是一条通过记录：

```json
{
  "schema_version": 1,
  "benchmark_id": "review-changes-synthetic-seeds-v1",
  "case_id": "runtime-missing-record",
  "case_origin": "synthetic_fixture",
  "condition": "current_skill",
  "run_status": "not_run",
  "host": {"name": null, "version": null, "model": null},
  "environment": {"os": null, "architecture": null},
  "input": {
    "repository_id": null,
    "review_manifest": null,
    "snapshot_fingerprint": null,
    "prior_result": null,
    "current_manifest": null
  },
  "artifacts": {
    "raw_transcript": null,
    "raw_host_output": null,
    "validated_result": null,
    "markdown_report": null,
    "recheck_json": null,
    "recheck_markdown": null
  },
  "annotation": {"status": "pending", "oracle": null, "human_labels": null},
  "scoring": {"status": "not_run", "script": null, "version": null, "output": null},
  "execution": {
    "failure_reason": null,
    "uncovered_paths": [],
    "context_omissions": [],
    "manual_interventions": null,
    "duration_seconds": null
  },
  "model_usage": {
    "input_tokens": null,
    "output_tokens": null,
    "cost": null,
    "currency": null
  }
}
```

## 案例规模与状态

当前 `seed-cases.json` 仍只有三组合成 fixture；另外已保存 11 个真实案例、23 次 A/B pilot 尝试。它达到首批 10–12 个真实案例的试跑规模，但不等于已人工标注或认证的基准集；20–30 个案例仍是后续成熟度目标。实际 host-output 和 oracle 留在仓库外受控存储，仓库只提交摘要、摘要哈希和便携证据索引。先完成人工标注、补齐宿主/模型/Skill 元数据并独立审计覆盖，再决定是否重复 A/B 和启动 C；PR4 上下文选择器和 PR5 历史报告/发布规划继续排队，WorkBuddy 状态仍为 `not_run`。盲测资格应由案例管理者另外标记，含已公开 oracle 的合成夹具不属于盲测。

PR3 已完成输入记录、保存运行、评分产物和首批 A/B pilot 的可审计实现；当前报告仍是流程与暂定 agent 证据，不是人工质量评估。下一步先完成人工标注和元数据/覆盖审计，再按相同快照与可核验的宿主条件重复 A/B。选择器实现后再增加 C，重点比较 B/C，并保留 A 作为直接审查基线。只有重新运行宿主才可评价 Skill 或上下文选择效果；单纯回放已存输出只验证判分脚本和该批旧输出。

## 开发流程前向验证

2026-10-09 在准备好的合成 `case-01` 上，用当前 checkout 单独构建的 Skill 完成了一次 Codex 本地前向运行。session 到达 `report_ready`，校验结果包含 1 条 `confirmed` finding；首次 finalize 成功。Semgrep 状态为 `missing`，模型、token 与费用均不可测，记录为 `null`。准备器生成的 case-01 review diff 哈希在运行前后相同，未应用 follow-up patch。

这次只验证入口、session、校验和导出的操作流程，不是 WorkBuddy 使用验收、真实项目盲测、三组同宿主对照或 Skill 增益评分。没有 WorkBuddy 原始 transcript，也没有生成质量分数。原始草稿、`semantic-result.json`、`runs/0001/host-result.json`、`validated-result.json`、`report.md`、session 状态和 `run-log.md` 保留在本机受控 operator 目录，不随仓库发布；公开使用入口见[用户指南](../docs/guide.md)。临时构建 ZIP 的 26 个条目及文件摘要均与 manifest 一致；它由当前 checkout 构建，不等于固定的 `dist/review-changes-0.2.1` 发布包。

基于已保存 session 和包的新会话恢复验证已完成：独立 Codex 会话仅使用保存的 Skill 与 session 产物，核对仓库身份、输入指纹并成功读取已有报告。恢复记录保留在本机受控 operator 目录，不随仓库发布；session 与 Skill 的公开边界见[Skill-first 说明](../docs/skill-first.md)。这仍不是 WorkBuddy 验收、质量 A/B 或盲测结果；WorkBuddy 状态为 `not_run`。固定 `0.2.1` 包仍是独立发布基线，未被本次前向运行重建或覆盖。
