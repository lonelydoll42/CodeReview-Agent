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

此前的 pilot/export 补丁回归为 266 passed、4 skipped；独立 core 检查 32 项、exporter 检查 8 项、`python -S` 导出 `check` 和 4 个 tamper probes 均通过。此次 workflow 修复后的完整测试为 325 passed、4 skipped；4 个 skip 中，3 个 Semgrep 测试因该次全量运行的 `PATH` 未包含 Semgrep 而跳过，之后用现有 `.venv/bin` 补跑的 Semgrep 检查为 9 passed；另 1 个 PostgreSQL 测试因未设置 `TEST_DATABASE_URL` 跳过。独立重点回归为 54 passed。Ruff、`pip check`、`git diff --check` 和 workflow exporter CLI `check` 均通过。本轮没有调用模型或执行 v3；这些检查覆盖实现、导出与会话流程，不评定 A/B 质量。strict scoring 对 23 条旧运行仍全部 blocked，人工盲审仍 pending，也没有为这 23 条历史运行创建新的有效评分。

后续人工盲审的输入、冻结来源索引、空白模板和操作步骤见[盲审准备指南](results/2026-10-09-ab-pilot-blind-review.md)。这批材料只提供十条 condition-blind prediction 和 source entry；它们不含 agent verdict、oracle mapping 或 human labels，尚未执行人工盲审。覆盖审计仍需保留真实文件读取和工具调用轨迹；Skill bundle digest 只说明包身份，不能证明宿主实际读取或使用了它。

## 2026-10-12 workflow v2 失败与 v3 准备状态

[workflow v2 失败报告](results/2026-10-12-ab-workflow-v2.md)记录了基于既有语料的 12-slot 流程复现尝试：1 个 slot 启动，0 个宿主完成，0 个证据有效，11 个未启动；没有生成宿主输出或 run record。seal 的 `accepted` 只表示依赖、冻结计划和 runtime-config 身份一致。artifact-reference 检查实际检查了 0 个引用，12 个 `slot-result.json` 不可用，因此 seal 完整性为 `incomplete`，不代表流程或质量通过。

首个 slot 失败的原因包括：provider 拒绝缺少 `type` 的 `schema_version` 输出 schema；argument 0 的外部二进制 symlink 使 artifact 枚举越界，ledger 错误路径随后重新抛出异常；18 个在成功关闭描述符后出现的 `close`/`EBADF` 事件又使 trace 变为 `unknown`。该批次来自既有语料，不是新盲测；人工标签仍 pending，质量指标仍为 null。完整细节和独立 ledger 核对见失败报告。

可用下列命令重算导出投影并核验其绑定；[已保存的检查结果](results/2026-10-12-ab-workflow-v2-check.json)为 `passed`：

```sh
python eval/export_workflow_batch.py check \
  --seal-report /root/project/review-benchmark-20261010/seal/workflow-report-v2.json \
  --ab-summary /root/project/review-benchmark-20261010/seal/ab-summary-final-v2.json \
  --dependency-snapshot /root/project/review-benchmark-20261010/seal/dependencies-before-v2.json \
  --plan /root/project/review-benchmark-20261010/batch-plan.json \
  --runtime-config /root/project/review-benchmark-20261010/runtime-config-v2.json \
  --summary eval/results/2026-10-12-ab-workflow-v2-summary.json \
  --evidence eval/results/2026-10-12-ab-workflow-v2-evidence.json \
  --check-output eval/results/2026-10-12-ab-workflow-v2-check.json
```

`2026-10-12-ab-workflow-validation-v3` 的 `runs-v3/` 目录已准备；本轮代码补丁和回归检查已完成，但 v3 输入尚未冻结，v3 批次尚未执行，也没有质量通过结论。该流程尝试没有改变旧的 23-run pilot、待完成人工标签、固定 `0.2.1` 夹具或 WorkBuddy `not_run` 状态；PR4 上下文选择器和 PR5 历史报告/发布规划仍在后续队列。

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

这次只验证入口、session、校验和导出的操作流程，不是 WorkBuddy 使用验收、真实项目盲测、三组同宿主对照或 Skill 增益评分。没有 WorkBuddy 原始 transcript，也没有生成质量分数。运行产物保存在 `/root/project/Session Skill 前向运行 20261009/`，包括初始草稿、`semantic-result.json`、`runs/0001/host-result.json`、`validated-result.json`、`report.md`、session 状态和 `run-log.md`。临时构建 ZIP 的 26 个条目及文件摘要均与 manifest 一致；它由当前 checkout 构建，不等于固定的 `dist/review-changes-0.2.1` 发布包。

基于已保存 session 和包的新会话恢复验证已完成：独立 Codex 会话仅使用保存的 Skill 与 session 产物，核对仓库身份、输入指纹并成功读取已有报告。记录保存在 `/root/project/Session Skill 恢复验证 20261009/resume-validation.md`。这仍不是 WorkBuddy 验收、质量 A/B 或盲测结果；WorkBuddy 状态为 `not_run`。固定 `0.2.1` 包仍是独立发布基线，未被本次前向运行重建或覆盖。
