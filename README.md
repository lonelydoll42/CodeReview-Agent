# CodeReview-Agent

> 面向 GitHub Pull Request 的多智能体 AI 代码审查系统。输入一个 PR URL 后，系统会自动拉取 diff、并行运行专项审查 Agent，并输出结构化报告与 GitHub 评论回写结果。

## Recruiter Snapshot

- **项目定位：** 一个可展示 AI 应用落地能力的多智能体工程项目，而不只是单轮对话 Demo
- **核心能力：** GitHub 集成、异步编排、专项 Agent 分工、结果聚合与严重级别仲裁
- **技术亮点：** FastAPI、Streamlit、Pydantic、PostgreSQL、Redis、LLM Provider 抽象

招聘/面试视角速览见：[docs/recruiter_brief.md](docs/recruiter_brief.md)。

开发、测试与 Git 工作流见 [CONTRIBUTING.md](CONTRIBUTING.md)，本次分支整合与验证范围见
[Git 优化实施记录](docs/git-optimization.md)。

---

## 目录

1. [项目概述](#项目概述)
2. [系统架构](#系统架构)
3. [目录结构](#目录结构)
4. [核心模块](#核心模块)
5. [扩展功能](#扩展功能)
6. [快速开始](#快速开始)
7. [配置参考](#配置参考)
8. [API 文档](#api-文档)
9. [Streamlit UI 使用指南](#streamlit-ui-使用指南)
10. [测试](#测试)
11. [技术栈](#技术栈)

---

## 项目概述

CodeReview-Agent 是一个生产级多智能体代码审查平台，具备以下核心能力：

- **四个并行专项 Agent**：安全漏洞、逻辑缺陷、性能问题、代码风格，各自独立运行
- **智能聚合**：跨 Agent 去重、严重级别仲裁、Claude 生成 Executive Summary
- **全链路持久化**：PostgreSQL 存储历史、Redis 实时缓存状态
- **多种接入方式**：REST API、Streamlit UI、GitHub Webhook 自动触发
- **结果回写 GitHub**：顶层评论 + 行内 Inline Comment
- **通知集成**：Slack、企业微信
- **历史 Dashboard**：趋势图、类别分布、严重级别统计

---

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                       接入层                             │
│   Streamlit UI    FastAPI REST    GitHub Webhook         │
└────────────────────────┬────────────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │     Orchestrator     │  asyncio 并行调度
              │   (去重缓存检查)     │
              └──────────┬──────────┘
                         │
         ┌───────────────┼──────────────┬──────────────┐
   ┌─────▼─────┐  ┌──────▼─────┐  ┌───▼──────┐  ┌────▼──────┐
   │StyleAgent │  │SecurityAgent│  │LogicAgent│  │Performance│
   │ 代码风格  │  │Semgrep+Claude│  │AST+Claude│  │  Agent    │
   └─────┬─────┘  └──────┬─────┘  └───┬──────┘  └────┬──────┘
         └───────────────┴─────────────┴───────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │         Aggregator           │
                    │   去重 → 仲裁 → 摘要生成    │
                    └─────────────┬──────────────┘
                                  │
         ┌────────────────────────┼─────────────────────┐
  ┌──────▼──────┐        ┌───────▼──────┐      ┌───────▼──────┐
  │  PostgreSQL  │        │    Redis      │      │  GitHub PR   │
  │  (持久化)   │        │  (状态缓存)  │      │  (评论回写)  │
  └─────────────┘        └──────────────┘      └──────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │    Slack / 企业微信 通知     │
                    └────────────────────────────┘
```

---

## 目录结构

```
CodeReview-Agent/
├── agents/
│   ├── base.py              Finding / AgentResult / FileDiff / BaseReviewAgent
│   ├── style_agent.py       代码风格检查（6类）
│   ├── security_agent.py    安全漏洞检测（Semgrep + Claude，8类）
│   ├── logic_agent.py       逻辑缺陷检测（AST + Claude，9类）
│   ├── performance_agent.py 性能问题检测（7类）
│   ├── aggregator.py        结果聚合：去重 + 仲裁 + 摘要
│   └── orchestrator.py      任务调度 + 扩展功能集成
├── tools/
│   ├── github_client.py     GitHub API：diff拉取 / 评论回写 / Inline Review
│   ├── ast_parser.py        Python AST 解析 + radon 圈复杂度
│   └── semgrep_runner.py    Semgrep 静态分析
├── graph/
│   └── workflow.py          LangGraph 状态机（备用编排）
├── api/
│   └── main.py              FastAPI：/review / /webhook/github / /stats/*
├── storage/
│   ├── models.py            SQLAlchemy ORM：ReviewTask/Result/Report
│   └── cache.py             Redis：状态缓存 + 去重缓存
├── notifications/
│   └── webhook.py           Slack / 企业微信 Webhook 通知
├── ui/
│   └── app.py               Streamlit UI：审查页 + Dashboard
├── eval/
│   └── metrics.py           Precision / Recall / F1 评测
├── tests/                   pytest 单元测试
├── config.py                pydantic-settings 配置管理
├── requirements.txt
└── .env.example
```

---

## 核心模块

### 1. 数据模型（`agents/base.py`）

```python
class Finding(BaseModel):
    file: str
    line_start: int
    line_end: int
    severity: str        # CRITICAL | HIGH | MEDIUM | LOW
    category: str
    description: str
    suggestion: str
    confidence: float    # 0.0 – 1.0

class AgentResult(BaseModel):
    agent_name: str
    findings: List[Finding]
    summary: str
    execution_time: float
    token_used: int

class FileDiff(BaseModel):
    filename: str
    language: str
    added_lines: List[tuple[int, str]]
    removed_lines: List[tuple[int, str]]
    raw_diff: str
```

---

### 2. 四个专项 Agent

#### StyleAgent

使用 Claude tool-use，只分析新增行，6 类检查：

| 类别 | 说明 |
|------|------|
| `naming` | camelCase vs snake_case、过短命名 |
| `function_length` | 函数超过 50 行 |
| `missing_docstring` | 公有函数/类缺少文档字符串 |
| `magic_number` | 裸数字字面量 |
| `duplicate_code` | 重复/相似代码块 |
| `import_hygiene` | 通配符导入、未使用导入 |

#### SecurityAgent

双引擎：Semgrep 静态规则 + Claude 语义理解，8 类漏洞：

`sql_injection` | `xss` | `hardcoded_secret` | `path_traversal` |
`insecure_deserialization` | `weak_crypto` | `ssrf` | `open_redirect`

#### LogicAgent

AST 预处理（函数结构 + 圈复杂度）+ Claude tool-use，9 类问题：

`null_dereference` | `boundary_condition` | `bare_except` | `missing_error_handling` |
`high_complexity` | `infinite_loop_risk` | `unused_return` | `infinite_recursion` | `other`

#### PerformanceAgent

AST 辅助 + Claude tool-use，7 类问题：

`n_plus_one` | `loop_invariant` | `unnecessary_copy` | `high_complexity` |
`inefficient_data_structure` | `blocking_io_in_async` | `redundant_computation`

---

### 3. Aggregator（核心亮点）

**Step 1 — 去重**：同文件 ±3 行、同类别合并，`source_agents` 记录所有来源。

**Step 2 — 严重级别仲裁**：按加权置信度投票决定最终级别：

```
SecurityAgent 1.0 > LogicAgent 0.8 > PerformanceAgent 0.6 > StyleAgent 0.4
```

**Step 3 — 摘要生成**：调用 `claude-opus-4-6` 生成 Executive Summary + Markdown 报告。

---

### 4. Orchestrator 执行流程

```
步骤 0  GitHub 固定 base/head SHA，拉取 diff、完整源码与 metadata
步骤 1  去重缓存检查（版本与分析代码指纹命中则复制历史报告）
步骤 2  过滤支持语言文件（15种语言）
步骤 3  Agent × File 全矩阵并行（asyncio.gather，每 Agent 独立超时）
步骤 4  Aggregator 聚合
步骤 5  持久化至 PostgreSQL
步骤 6  所有 Agent 调用返回后写入去重缓存
步骤 7  条件化回写 PR 顶层评论
步骤 8  条件化发布 Inline Comment
步骤 9  发送 Slack / 企业微信通知
```

**支持语言**：Python、JavaScript、TypeScript、Go、Java、Ruby、Rust、C、C++、C#、PHP、Swift、Kotlin、Scala、Bash、SQL

---

### 5. 持久化层

**PostgreSQL 表结构**

| 表 | 主要字段 | 说明 |
|----|----------|------|
| `review_tasks` | id, pr_url, status, created_at | 任务主表 |
| `review_results` | task_id, agent_name, findings(JSON), confidence | 每 Agent 一行 |
| `review_reports` | task_id, final_report(JSON), markdown_report | 聚合报告 |

**Redis Key 模式**

| Key | 内容 | TTL |
|-----|------|-----|
| `codereview:task:{id}:status` | 任务状态 | 24h |
| `codereview:task:{id}:agent:{name}` | Agent 结果 JSON | 24h |
| `codereview:dedup:{url_hash}:{fingerprint}` | 已完成 task_id（比较版本与分析代码指纹） | 可配置 |

---

## 扩展功能

### Feature 1 — PR 顶层评论回写

审查完成后，将完整 Markdown 报告作为 PR 顶层 Issue Comment 发出。

```ini
ENABLE_PR_COMMENT=true
```

### Feature 2 — Inline Comment 行内标注

将每条 Finding 精确标注到 PR diff 对应的文件和行号，体验对标 GitHub Advanced Security。

```ini
ENABLE_INLINE_COMMENT=true
```

### Feature 3 — 重复提交去重缓存

以 PR URL、base/head SHA、共同祖先和分析代码指纹为 key。相同输入重复提交时，
为新任务复制历史报告并跳过 Agent 调用。仍需获取 GitHub 快照，耗时取决于网络和文件数量。
外部分析规则变化时递增 `REVIEW_RULESET_VERSION` 并重启服务。

```ini
ENABLE_DEDUP_CACHE=true
DEDUP_CACHE_TTL=86400
REVIEW_RULESET_VERSION=1
```

### Feature 4 — GitHub Webhook 自动触发

配置 GitHub 仓库 Webhook 后，PR 打开/更新时自动触发审查，无需手动提交。

```ini
GITHUB_WEBHOOK_SECRET=your_secret
```

Webhook URL 填写：`http://your-server:8000/webhook/github`
监听事件：`pull_request`（opened / synchronize / reopened）

### Feature 5 — 历史趋势 Dashboard

Streamlit 左侧导航切换到 **Dashboard** 页，展示：
- 总任务数 / 完成 / 失败 / 总 Finding 数（4 个 Metric 卡片）
- 按严重级别的 Finding 分布柱状图
- TOP 10 问题类别排行
- 每日任务数 + Finding 数时间趋势（7~90 天可调）

### Feature 6 — Slack / 企业微信通知

审查完成且发现超过阈值严重级别的问题时，主动推送通知。

```ini
ENABLE_NOTIFY=true
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
NOTIFY_ON_SEVERITIES=CRITICAL,HIGH
```

Slack 消息包含「View PR」按钮；企业微信使用 markdown 格式。

---

## 快速开始

### 1. 安装依赖

```bash
cd CodeReview-Agent
pip install -r requirements.txt
```

### 2. 启动基础服务

```bash
docker run -d -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=codereview \
  postgres:15

docker run -d -p 6379:6379 redis:7
```

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写 ANTHROPIC_API_KEY 和 GITHUB_TOKEN
```

### 4. 启动 API 服务

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

### 5. 启动 UI（可选）

```bash
streamlit run ui/app.py
# 访问 http://localhost:8501
```

---

## 配置参考

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `ANTHROPIC_API_KEY` | — | **必填**，所有 Agent 使用 |
| `GITHUB_TOKEN` | — | **必填**，拉取 PR diff；回写评论需 write 权限 |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/codereview` | PostgreSQL 连接 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接 |
| `MAX_PARALLEL_AGENTS` | `5` | 最大并行 Agent 数 |
| `AGENT_TIMEOUT_SECONDS` | `30` | 单 Agent 超时（秒） |
| `ENABLE_PR_COMMENT` | `false` | 审查完成后回写 PR 顶层评论 |
| `ENABLE_INLINE_COMMENT` | `false` | 发布行内 Inline Comment |
| `ENABLE_DEDUP_CACHE` | `true` | 同 commit SHA 去重跳过 |
| `DEDUP_CACHE_TTL` | `86400` | 去重缓存有效期（秒） |
| `GITHUB_WEBHOOK_SECRET` | — | Webhook HMAC 签名密钥 |
| `ENABLE_NOTIFY` | `false` | 开启 Slack/企微通知 |
| `SLACK_WEBHOOK_URL` | — | Slack Incoming Webhook URL |
| `WECHAT_WEBHOOK_URL` | — | 企业微信机器人 Webhook URL |
| `NOTIFY_ON_SEVERITIES` | `CRITICAL,HIGH` | 触发通知的最低严重级别 |

---

## API 文档

服务启动后访问 `http://localhost:8000/docs` 查看完整 Swagger UI。

### 核心端点

#### `POST /review` — 提交审查任务

```bash
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{"pr_url": "https://github.com/owner/repo/pull/42"}'
```

响应：
```json
{"task_id": 1, "status": "pending", "message": "Review task created and queued."}
```

#### `GET /review/{task_id}` — 查询任务状态与结果

```bash
curl http://localhost:8000/review/1
```

`status` 字段值：`pending` → `running` → `completed` / `failed`

完成后响应包含 `results`（每 Agent 输出）和 `report`（聚合报告 + Markdown）。

#### `POST /webhook/github` — GitHub Webhook 接收

在 GitHub 仓库 Settings → Webhooks 中配置：
- Payload URL：`http://your-server:8000/webhook/github`
- Content type：`application/json`
- Secret：与 `GITHUB_WEBHOOK_SECRET` 一致
- Events：勾选 `Pull requests`

#### `GET /stats/summary` — 总体统计

```json
{
  "total_tasks": 120,
  "completed": 115,
  "failed": 5,
  "total_findings": 843,
  "by_severity": [
    {"severity": "CRITICAL", "count": 12},
    {"severity": "HIGH", "count": 87}
  ]
}
```

#### `GET /stats/top_categories?limit=10` — TOP 问题类别

#### `GET /stats/trends?days=30` — 每日趋势数据

#### `GET /health` — 健康检查

---

## Streamlit UI 使用指南

### Review 页（默认）

1. 在输入框粘贴 GitHub PR URL
2. 点击 **Start Review**
3. 等待进度条完成（通常 30~90 秒）
4. 查看 5 个 Tab 的结果：

| Tab | 内容 |
|-----|------|
| Summary | 统计表 + Executive Summary |
| Security | SecurityAgent 的安全漏洞 |
| Logic | LogicAgent 的逻辑缺陷 |
| Performance | PerformanceAgent 的性能问题 |
| Style | StyleAgent 的代码风格问题 |

5. 点击底部 **Download Markdown Report** 下载报告

### Dashboard 页

左侧导航栏选择 **Dashboard**：
- 顶部 4 个 Metric 卡片（总量统计）
- 严重级别分布图
- TOP 10 问题类别图
- 时间趋势图（侧边栏滑块调整时间窗口，7~90 天）

---

## 测试

```bash
pytest tests/ -v
```

测试覆盖：
- `test_style_agent.py` — StyleAgent tool-use 流程
- `test_logic_agent.py` — LogicAgent AST + tool-use
- `test_performance_agent.py` — PerformanceAgent
- `test_aggregator.py` — 去重、仲裁逻辑
- `test_orchestrator.py` — 成功/超时/GitHub失败三种场景
- `test_metrics.py` — Precision/Recall/F1 计算

普通测试 mock 外部服务，无需真实基础设施。配置专用测试数据库 `TEST_DATABASE_URL`
后，还会运行 PostgreSQL 建表、报告复制和统计查询集成测试；详见贡献指南。

---

## 技术栈

| 层次 | 技术 |
|------|------|
| LLM 调用 | Anthropic SDK，`claude-opus-4-6`，tool-use 模式 |
| 工作流编排 | asyncio（主）+ LangGraph（备用） |
| Web API | FastAPI + uvicorn |
| 数据库 | PostgreSQL 15 + SQLAlchemy 2.0 async |
| 缓存 | Redis 7 |
| 静态分析 | Semgrep |
| 代码分析 | Python AST + radon（圈复杂度） |
| UI | Streamlit |
| 配置管理 | pydantic-settings |
| 测试 | pytest + pytest-asyncio |
| GitHub 集成 | PyGithub |
| 通知 | httpx（Slack / 企业微信 Webhook） |
