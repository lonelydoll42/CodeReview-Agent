# 项目亮点与展示指南

[返回首页](../README.md) · [审查报告示例](example-report.md) · [贡献指南](../CONTRIBUTING.md)

## 一句话定位

CodeReview-Agent 将 GitHub PR 快照获取、专项 Agent 审查、静态工具辅助、结果聚合、持久化和工作台展示组织成一条可运行的代码审查链路。

## 可以从代码验证的工程能力

| 设计点 | 为什么这样做 | 实现入口 |
| --- | --- | --- |
| 专项 Agent | 用安全、逻辑、性能、风格四个维度组织审查职责 | [agents/](../agents/) |
| 结构化输出 | Finding、AgentResult、AggregatedReport 使展示与存储共享明确的数据契约 | [base.py](../agents/base.py)、[aggregator.py](../agents/aggregator.py) |
| 固定版本输入 | 避免获取期间 PR 变化，或行内评论落到未审查的新版本 | [github_client.py](../tools/github_client.py) |
| 完整源码辅助 | AST 与静态分析需要上下文，同时保留真实文件坐标 | [ast_parser.py](../tools/ast_parser.py)、[semgrep_runner.py](../tools/semgrep_runner.py) |
| 严重级别仲裁 | 区分问题影响与置信度，合并来源并按加权投票裁决 | [aggregator.py](../agents/aggregator.py) |
| 缓存版本化 | 将比较版本、分析实现和外部规则版本纳入缓存，减少错误复用 | [review_version.py](../tools/review_version.py) |
| 数据库验证 | 使用隔离 schema 检验建表、报告复制、近期任务与统计 SQL | [数据库集成测试](../tests/test_database_integration.py) |
| 演示入口 | REST API、任务列表、审查页、Dashboard 与 Markdown 下载 | [api/](../api/)、[ui/](../ui/) |

## 建议的五分钟演示

1. **展示问题场景。** 打开一个准备好的 PR，指出需要关注的安全、逻辑或性能变化。
2. **发起审查。** 在 Tasks 页提交 PR URL，说明后台任务、状态轮询与固定版本输入。
3. **解读一条发现。** 解释文件、行号、类别、置信度、修改建议与来源 Agent。没有现场 API 凭据时使用[明确标注的示例](example-report.md)。
4. **解释聚合。** 多个 Agent 的发现可以合并；置信度与严重级别分别处理。
5. **展示验证与边界。** 打开测试、CI 和 README 的后续方向，说明可恢复任务、并发控制、认证与失败状态仍可继续完善。

## 值得讨论的取舍

- 将审查拆为多个 Agent，如何影响覆盖面、延迟与模型成本？
- Semgrep、Python AST 与 LLM 提供的信号各有哪些局限？
- 为什么仅用 PR URL 或 head SHA 不足以作为所有审查缓存的标识？
- 同一问题有不同严重级别时，如何避免把高置信度误当成高风险？
- 任务持久化、缓存更新、评论发送发生在不同系统中，失败后如何保持状态清晰？
- 如何补充真实标注数据来衡量误报、漏报，而不是只展示几次成功案例？

## 当前没有实现的能力

本分支没有 RiskProfile 分类、Playbook 选择、自动 Merge Gate 或 `/agents`、`/playbooks`、`/llm/providers` API。当前模型主链路直接使用 Anthropic；`graph/` 中的 LangGraph 是备用工作流。后续如果引入这些能力，应以对应实现和验证结果更新展示材料。
