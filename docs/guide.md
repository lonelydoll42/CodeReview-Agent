# 使用指南

[返回仓库首页](../README.md) · [报告示例](example-report.md) · [开发指南](../CONTRIBUTING.md)

本指南对应 `integration/review-updates` 中的实现。首次运行请先完成 README 的[快速开始](../README.md#快速开始)。

## 配置参考

API 使用 [config.py](../config.py) 中的 Pydantic Settings，从进程环境和项目根目录 `.env` 读取配置。修改后需要重启服务。

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | 空 | Claude 审查与摘要生成所用密钥 |
| `GITHUB_TOKEN` | 空 | 访问仓库；私有仓库和评论回写需要对应权限 |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/codereview` | PostgreSQL 异步连接 |
| `REDIS_URL` | `redis://localhost:6379/0` | 状态、结果与重复审查缓存 |
| `AGENT_TIMEOUT_SECONDS` | `30` | 单次 Agent 协程等待超时；同步调用期间不能保证及时取消 |
| `MAX_PARALLEL_AGENTS` | `5` | 预留配置，当前调度器尚未使用它限制并发 |
| `ENABLE_DEDUP_CACHE` | `true` | 开启重复审查报告复用 |
| `DEDUP_CACHE_TTL` | `86400` | 重复审查缓存有效期，单位为秒 |
| `REVIEW_RULESET_VERSION` | `1` | 外部规则或分析配置改变后递增并重启服务 |
| `ENABLE_PR_COMMENT` | `false` | 发布 PR 顶层报告评论 |
| `ENABLE_INLINE_COMMENT` | `false` | 发布新增行上的行内评论 |
| `GITHUB_WEBHOOK_SECRET` | 空 | GitHub Webhook HMAC-SHA256 验签密钥 |
| `ENABLE_NOTIFY` | `false` | 开启通知 |
| `SLACK_WEBHOOK_URL` | 空 | Slack Incoming Webhook |
| `WECHAT_WEBHOOK_URL` | 空 | 企业微信机器人 Webhook |
| `NOTIFY_ON_SEVERITIES` | `CRITICAL,HIGH` | 完成审查后触发通知的严重级别集合 |

模型名称当前由各 Agent 和 Aggregator 的 `MODEL` 常量指定为 `claude-opus-4-6`。`OPENAI_API_KEY` 虽出现在配置中，但现有主审查链路不使用它；只填此项不能启动 Claude 审查。

### 工作台环境变量

Streamlit 直接从进程环境读取以下变量，**不会自行加载 API 的 `.env`**。在启动 UI 的终端使用 `export`，或由服务管理器传入；不要把这些未在 `Settings` 中声明的字段直接追加到 API `.env`，否则可能触发配置校验错误。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `API_BASE` | `http://localhost:8000` | API 服务地址 |
| `DASHBOARD_USER` | `admin` | 工作台登录用户名 |
| `DASHBOARD_PASSWORD` | `admin` | 工作台登录密码，部署时替换 |
| `SECRET_KEY` | 开发用固定值 | 登录令牌签名密钥，部署时设置随机值 |

README 中的随机密钥命令适合本地体验；长期运行时应由服务环境持久配置。工作台登录不保护 FastAPI 接口，公开部署需要单独为 API 添加认证和访问控制。

## 工作台怎么用

| 页面 | 常用操作 |
| --- | --- |
| **Tasks**（登录后默认页） | 提交 PR，筛选近期任务，打开任务详情 |
| **Review** | 提交审查、查看最近队列、跟踪任务、查看报告 |
| **Dashboard** | 汇总任务和问题数量，查看严重级别、类别和日期趋势 |

1. 将可访问的 GitHub PR URL 粘贴到 Tasks 输入框，点击 **Queue Review**。
2. 在任务列表中打开对应任务，或切换到 Review 跟踪任务状态。
3. 使用手动刷新，或在 Review 页面启用 **Auto-refresh (4s)**。
4. 审查完成后按 Agent、严重级别等条件查看问题，下载 Markdown 报告。

任务状态通常表现为 `pending → running → completed / failed`。耗时受文件数量、GitHub API、模型和静态分析影响，没有固定的秒级完成保证。`Stop Tracking` 停止前端跟踪，不取消后台任务。

Dashboard 合并接口当前接受 **7–90 天**；UI 中的 `24h`、`All` 选项与该范围尚未完全对齐，体验时请使用 `7d`、`30d` 或 `90d`。

## API 速查

启动后访问 [Swagger UI](http://localhost:8000/docs)，可查看实际请求与响应模型。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/review` | 创建 PR 审查任务 |
| `GET` | `/review/{task_id}` | 查询任务状态、Agent 结果与报告 |
| `GET` | `/reviews/recent?limit=20` | 近期任务摘要 |
| `POST` | `/webhook/github` | 接收 GitHub PR 事件 |
| `GET` | `/stats/summary` | 任务与问题总量、严重级别分布 |
| `GET` | `/stats/top_categories?limit=10` | 常见问题类别 |
| `GET` | `/stats/trends?days=30` | 按日期统计任务与问题 |
| `GET` | `/stats/dashboard?days=30` | 合并 Dashboard 数据，短期缓存 |
| `GET` | `/health` | 进程存活检查，不代表外部依赖全部健康 |

### 提交与轮询

将示例 URL 替换为真实 PR：

```bash
curl -X POST http://localhost:8000/review \
  -H 'Content-Type: application/json' \
  -d '{"pr_url":"https://github.com/OWNER/REPO/pull/NUMBER"}'
```

创建成功后返回 HTTP 201，响应形如：

```json
{
  "task_id": 1,
  "status": "pending",
  "message": "Review task created. Poll GET /review/{task_id} for updates."
}
```

```bash
# 完整结果
curl http://localhost:8000/review/1

# 省略逐 Agent 结果，报告字段仍保留
curl 'http://localhost:8000/review/1?include_results=false'
```

`results[].findings` 是包含 `findings` 数组的 AgentResult 对象。`report.final_report` 当前是 **JSON 字符串**，客户端需要再解析一次；`report.markdown_report` 可直接作为 Markdown 使用。

<a id="github-与通知集成"></a>
## GitHub 与通知集成

### 评论回写

在 `.env` 中开启需要的功能：

```ini
ENABLE_PR_COMMENT=true
ENABLE_INLINE_COMMENT=true
```

应用所用 GitHub Token 需要目标仓库的读取权限，以及发布 Issue / Pull Request 评论所需的写权限。顶层评论注明被审查的 commit，行内评论只发送到审查 diff 的新增行；发现 PR head 已更新时跳过旧版本行内评论。

### GitHub Webhook

在 GitHub 仓库 **Settings → Webhooks → Add webhook** 中设置：

- Payload URL：你的服务地址加 `/webhook/github`。
- Content type：`application/json`。
- Secret：与服务的 `GITHUB_WEBHOOK_SECRET` 一致。
- Events：选择 **Pull requests**。

系统处理 `opened`、`synchronize`、`reopened`，其他事件或操作会忽略。签名验证只有配置 Secret 后才启用。公网 Webhook 需要可从 GitHub 访问的 HTTPS 入口，本地回环地址不能直接接收 GitHub 请求。

### Slack / 企业微信

```ini
ENABLE_NOTIFY=true
SLACK_WEBHOOK_URL=
WECHAT_WEBHOOK_URL=
NOTIFY_ON_SEVERITIES=CRITICAL,HIGH
```

填写所需渠道地址即可；完成审查且出现配置集合中的级别时发送摘要，部分失败路径会发送失败通知。评论与通知属于外部写操作，首次体验建议保留默认关闭状态。

## 聚合与缓存约定

- **去重**：按文件与类别分组，相邻 `line_start` 距离不超过 3 行时合并为一组；连续相近问题可能形成较宽的簇。
- **投票**：严重级别得票为 Agent 权重 × finding 置信度。同级别累加，平票选择更高的已报告级别。
- **权重**：Security `1.0`、Logic `0.8`、Performance `0.6`、Style `0.4`。Security 的 CRITICAL 发现不会降级。
- **置信度**：聚合后的置信度独立加权计算，不用它直接推断问题严重级别。
- **统计**：最终报告的严重级别统计基于去重结果；`by_agent` 累加各 Agent 所有文件的原始问题数。Dashboard 当前统计原始 Agent 结果，可能高于去重后的报告总数。
- **缓存**：仍需获取 GitHub 快照来确认输入版本；缓存主要节省 Agent 调用，不代表完全免网络或毫秒级响应。

## 常见问题

| 现象 | 建议检查 |
| --- | --- |
| API 启动失败或数据库拒绝连接 | PostgreSQL 是否就绪，`DATABASE_URL` 的库名、账号与端口是否匹配 |
| GitHub 403 / 404 | Token 的目标仓库范围与权限、私有仓库访问权、API 限流 |
| 模型调用失败 | Anthropic Key、模型访问权、配额与网络；不要只配置 `OPENAI_API_KEY` |
| Redis 不可用 | 检查服务与 `REDIS_URL`；部分缓存操作会降级，不能替代数据库 |
| 找不到支持的文件 | 查看报告 `skipped_files`；删除文件、二进制、编码、大小与语言都可能影响覆盖 |
| PR 在审查期间有新提交 | 基于最新版本重新提交任务 |
| 审查结果为空 | 同时检查服务日志与覆盖范围，Agent 或静态分析失败可能被降级处理 |
| 修改 UI 密码不生效 | 确认变量已导出到启动 Streamlit 的进程环境，再重启 UI |
| 任务在重启后没有继续 | 当前使用进程内后台任务，不支持断点恢复，需重新提交 |

## 测试与实现入口

- [测试与 Git 工作流](../CONTRIBUTING.md)
- [任务调度器](../agents/orchestrator.py)
- [报告聚合器](../agents/aggregator.py)
- [GitHub 快照与评论客户端](../tools/github_client.py)
- [API](../api/main.py) / [UI](../ui/app.py)

封面与示例仅用于说明功能；实际效果、成本和耗时需要在自己的代码与模型配置上验证。
