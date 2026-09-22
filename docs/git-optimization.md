# Git 优化实施记录

## 整合范围

基线为 `main@179206d`，原始测试为 42 项通过。所有实现位于
`integration/review-updates` 独立 worktree。

按历史顺序合入以下连续提交，保留原作者与提交历史：

| 提交 | 内容 |
| --- | --- |
| `ce99a9f` | UI 加载与 API 查询优化 |
| `6749fff` | 移除 Google Fonts 外部加载 |
| `9c1d43a` | 分层缓存 TTL |
| `66d9834` | HTTP 超时与连接池限制 |

`21c1b44` 一次涉及 45 个文件，包含多模型抽象、部署与 UI 重构，本次不混入。
它应作为独立后续 PR 评估，尤其需要审查线程中的异步 Agent 生命周期、配置迁移
和新部署默认值。`84aaf92` 的文档基于该重构，也一并保留在原分支。
测试用分支 `codex/test-pr-smoke-20260330` 不包含产品功能，未整合。

## 本次修复

1. 固定审查提交快照，拒绝拉取过程中变化的 PR；报告和评论保留提交身份。
2. 正确处理 Git patch 的无末尾换行标记、以加减号开头的源码和重命名。
3. 缺失/截断 patch 从共同祖先重建，完整文件用于 AST/安全分析并保留真实行号。
4. 缓存包含比较版本及分析代码版本；持久化后写缓存，命中后复制可读取的结果。
5. PostgreSQL 建表默认状态从 `pending` 修正为 SQLAlchemy 枚举实际使用的
   `PENDING`，不改变现有枚举名称或 API 的小写状态值。
6. 新增 CI、PR 模板、基础 Ruff 检查、忽略规则和 LF 换行规则。

## 验证方式

```bash
python -m pytest -q
ruff check .
python -m pip check
git diff --check
```

单元测试覆盖 PR 更新竞态、base/head/规则变化、源码坐标、二进制/删除文件、
重命名、缺失 patch、越界评论、缓存写入顺序和失败路径。
可选 PostgreSQL 测试真实执行建表、缓存结果复制、近期任务列表和 Dashboard
统计 SQL。所有测试均不发送 GitHub 评论、不调用模型 API。

最初整合时的本地验证结果（历史记录）：

| 检查 | 结果 |
| --- | --- |
| 原始 main，Python 3.11 | 42 项通过 |
| 整合分支，Python 3.10.21 | 65 项通过，含真实 PostgreSQL 18 集成测试 |
| 整合分支，Python 3.11.16 | 65 项通过，含真实 PostgreSQL 18 集成测试 |
| Ruff、依赖兼容性、Git 空白检查 | 通过 |
| GitHub PR #3 只读验证 | 固定 SHA 获取 `ui/app.py`，完整源码 39,668 字符，新增行 1 行 |

PR #3 验证使用 base `ce99a9fbf77f64093ba5920082648b6221d4f2dd`、
head `6749ffffe674a23cc57edda7f47d489b5bf9b257`。该检查未发布任何评论。
CI 使用 PostgreSQL 15；本地使用已有 PostgreSQL 18 镜像运行独立临时数据库。

上述最初整合阶段未执行 GitHub 远端 CI 或真实模型端到端审查；UI 性能提交已整合并验证其后端
查询，但未进行浏览器性能基准测量。该阶段没有发布、部署、删除原分支或重写历史。

## 后续验证（2026-09-22）

- `4a51528` 修正严重级别投票与同一 Agent 跨文件的问题数累加，补充聚合器回归测试。
- 本地测试为 81 项通过、1 项 PostgreSQL 测试按配置跳过；接入独立临时 PostgreSQL 18 后为 82 项全部通过。
- 集成分支已推送至 GitHub，[CI 运行记录](https://github.com/lonelydoll42/CodeReview-Agent/actions/runs/35698701060)成功，覆盖 Python 3.10 / 3.11 与 PostgreSQL 15。
- 真实模型端到端审查和浏览器性能基准仍未验证；未将本地测试结果当作线上效果或性能承诺。
