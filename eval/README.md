# 宿主中立评测输入

本目录的评测契约用于记录同一宿主在不同审查条件下的实际行为。它不启动模型、不声称现有 Skill 已通过 WorkBuddy，也不把仓库单元测试当成宿主质量分数。

## 现有材料与边界

- [seed-cases.json](data/seed-cases.json) 索引 `prepare_workbuddy_acceptance.py` 生成的三个合成隔离夹具：缺失记录缺陷、行为保持的鉴权迁移、跨文件返回契约变化。它们适合检查导入、流程和报告/复查行为，不是来自真实项目的盲测。
- 每次准备器运行会将 host 产物留在其输出目录的 `host-artifacts/case-NN/`，将 operator oracle 放在 `operator/oracle.md`，并将项目测试和外部探针记录留在 `validation/`。遵循 [WorkBuddy 验收单](../docs/workbuddy-acceptance.md)：oracle、探针和准备补丁不能进入宿主提示；目前宿主结果为 `not_run`。
- 当前 `eval/metrics.py` 读取旧的 `pr_url`/`human_findings` 数据，并依赖服务端 `agents.base.Finding`；只按文件、类别和行号容差匹配 finding。没有现成宿主数据集，也没有对状态、覆盖、失败、复查误判或成本的判分。保留原命令兼容旧数据；本契约不把它描述为 Skill 质量评测器。

## A/B 基线与后续 C 组

先用同一宿主、同一批真实变更案例建立 A/B 基线。第一批建议 10–12 个案例，覆盖已知缺陷、跨文件契约、删除引入的问题和合法重构；答案由独立标注者保存，不进入宿主输入。建立 A/B 基线不需要等待上下文候选选择器，也不需要先凑齐三组。

每个案例可按以下条件记录；先执行 A、B，C 在上下文选择器实现后作为后续增量：

| `condition` | 输入方式 |
|---|---|
| `same_host_direct` | A：同一宿主，不加载本项目 Skill |
| `current_skill` | B：同一宿主加载当前固定版本的 Skill |
| `context_selector_skill` | C：同一宿主加载加入上下文候选选择器的 Skill；选择器实现前不运行 |

固定仓库快照、审查范围、宿主版本和模型配置；保存其指纹与实际运行条件。若宿主、模型或输入配置不同，分开记录，不能汇总成同条件比较。WorkBuddy 暂时无法连接时，可先用能够执行两种条件的其他同一宿主完成 A/B，并如实记录宿主；这不构成 WorkBuddy 验收。当前没有 A/B 质量结果，C 也尚未运行。

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

当前 seed index 只有三组合成 fixture，且 WorkBuddy 状态是 `not_run`。A/B 可先用 10–12 个真实案例试跑，再逐步扩充至 20–30 个作为数据集成熟度目标；无需等到目标规模或 C 组齐备才建立首轮基线。在案例、独立 oracle、人工标注和实际宿主运行记录进入仓库或受控存储前，不得报告该规模已达成，也不生成占位评分。盲测资格应由案例管理者另外标记，含已公开 oracle 的合成夹具不属于盲测。

实现 PR3 时先让输入记录、原始结果和评分产物可审计，再以小批真实案例启动 A/B，核实标注一致性与流程。选择器实现后，在相同快照和宿主条件下增加 C，重点比较 B/C 的变化，并保留 A 作为直接审查基线。只有重新运行宿主才可以评价 Skill 或上下文选择效果；单纯回放已存输出只验证判分脚本和该批旧输出。

## 开发流程前向验证

2026-10-09 在准备好的合成 `case-01` 上，用当前 checkout 单独构建的 Skill 完成了一次 Codex 本地前向运行。session 到达 `report_ready`，校验结果包含 1 条 `confirmed` finding；首次 finalize 成功。Semgrep 状态为 `missing`，模型、token 与费用均不可测，记录为 `null`。准备器生成的 case-01 review diff 哈希在运行前后相同，未应用 follow-up patch。

这次只验证入口、session、校验和导出的操作流程，不是 WorkBuddy 使用验收、真实项目盲测、三组同宿主对照或 Skill 增益评分。没有 WorkBuddy 原始 transcript，也没有生成质量分数。运行产物保存在 `/root/project/Session Skill 前向运行 20261009/`，包括初始草稿、`semantic-result.json`、`runs/0001/host-result.json`、`validated-result.json`、`report.md`、session 状态和 `run-log.md`。临时构建 ZIP 的 26 个条目及文件摘要均与 manifest 一致；它由当前 checkout 构建，不等于固定的 `dist/review-changes-0.2.1` 发布包。

基于已保存 session 和包的新会话恢复验证已完成：独立 Codex 会话仅使用保存的 Skill 与 session 产物，核对仓库身份、输入指纹并成功读取已有报告。记录保存在 `/root/project/Session Skill 恢复验证 20261009/resume-validation.md`。这仍不是 WorkBuddy 验收、质量 A/B 或盲测结果；WorkBuddy 状态为 `not_run`。固定 `0.2.1` 包仍是独立发布基线，未被本次前向运行重建或覆盖。
