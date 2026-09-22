# 审查报告示例

[返回首页](../README.md) · [使用指南](guide.md)

> 以下均为手工构造的演示数据，不来自本仓库的真实审查。为了便于阅读使用中文描述；模型输出语言取决于提示词与上下文。

## Executive Summary

本次示例审查发现 3 个问题：1 个高风险安全问题、1 个中等级别性能问题和 1 个低等级别可维护性问题。优先处理用户输入直接拼接到 SQL 查询的路径，再检查循环中的不变计算。以下建议应结合项目实际数据库驱动、调用约束与测试结果确认。

## Statistics

| Severity | Count |
| --- | --- |
| CRITICAL | 0 |
| HIGH | 1 |
| MEDIUM | 1 |
| LOW | 1 |

## Findings

### HIGH · SQL 查询拼接用户输入

**位置：** `src/users.py`，第 24 行<br />
**类别：** `sql_injection`<br />
**来源：** SecurityAgent<br />
**置信度：** 94%

用户提供的名称直接进入 SQL 文本，可能改变查询语义。使用数据库驱动的参数绑定接口，将查询结构与输入值分离。

例如，对于 SQLite 的占位符语法：

```python
# 修改前
cursor.execute(f"SELECT id FROM users WHERE name = '{name}'")

# 修改后
cursor.execute("SELECT id FROM users WHERE name = ?", (name,))
```

不同数据库驱动的占位符语法不同，应按实际驱动调整，并补充带引号输入的测试。

### MEDIUM · 循环内重复计算不变配置

**位置：** `src/batch.py`，第 48 行<br />
**类别：** `loop_invariant`<br />
**来源：** PerformanceAgent<br />
**置信度：** 83%

每次迭代都解析相同的配置。如果配置在本次批处理中不变，可将解析放到循环外，并让循环复用结果；如果配置会变化，则需保留原有刷新语义。

### LOW · 重试次数使用未命名常量

**位置：** `src/config.py`，第 12 行<br />
**类别：** `magic_number`<br />
**来源：** StyleAgent<br />
**置信度：** 91%

数字 `3` 表示重试上限，但缺少业务含义。可提取为 `MAX_RETRIES = 3`，便于维护和测试。高置信度只说明对此发现的确定程度，不会将该问题自动升级为 CRITICAL。

## 数据结构示例

聚合报告中的一条 finding：

```json
{
  "file": "src/users.py",
  "line_start": 24,
  "line_end": 24,
  "severity": "HIGH",
  "category": "sql_injection",
  "description": "用户输入直接拼接到 SQL 查询文本。",
  "suggestion": "使用对应数据库驱动的参数绑定接口。",
  "confidence": 0.94,
  "source_agents": ["SecurityAgent"]
}
```

真实报告还会通过 `pr_metadata` 记录被审查的 base/head/merge-base SHA 与未分析文件。示例中的路径与行号仅用于展示格式。
