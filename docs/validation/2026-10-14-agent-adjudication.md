# 2026-10-14 历史预测记录的 Agent 替代判定

## 结论范围

本归档记录两份内部 Agent 报告对离线冻结历史证据的判定，并由最终 Agent 汇总逐条证据与缺口。评判主体为 Agent；模型版本、token/usage 和费用未知。共 10 条预测记录：9 条 `supported`、0 条 `unsupported`、1 条 `unresolved`（`p0007`）。范围仅限所提供的历史冻结快照和补丁，不判断当前主分支是否仍有这些问题。

这是内部 Agent 判定，不是人工标签，也未由外部独立审稿人复验。原证据包及原始报告保存在内部结果包中，未随仓库公开；外部读者无法仅凭本仓库重做源码和探针核验。输入离线包 `human-adjudication-offline-20261013.zip` 的 SHA-256 为 `7082879a7a54ce27c5925d8704c9c0fa5a32904dbdf80868a1f3ee9889581320`；最终 Agent 汇总 Markdown/JSON 的 SHA-256 分别为 `9db851e06ecd4c25a645b3caebdad81974cce81ae92ef640d1707b773a3ec3a4` 与 `c200e7a83de0a751c1ec0d074b49338d5b21c47f1d8d03bf86989e078559b98e`。两份内部报告分别为 `reports/agent-1-adjudication.*` 和 `reports/agent-2-check.*`；不在本仓库复制冻结源码、原始输出、A/B 映射或 ZIP。

## 逐条摘要

| 记录 | 判定 | 摘要及边界 |
| --- | --- | --- |
| `p0001` | supported | 缺失 decision evidence 时仍接受 reason-only 的 `resolved`；冻结的同 manifest 测试和第二份报告的定向调用均观察到该结果。 |
| `p0002` | supported | 已关联的同 ID confirmed finding 仍可被显式 `resolved` 覆盖，并从 `new_findings` 中排除；第二份报告的定向调用复现了该输出。 |
| `p0003` | supported | `partial` 范围与空 evidence 仍可产出单条 `resolved`。第二份报告补做了仅覆盖无关文件的定向调用，仍观察到 resolved；缺失相关上下文不应构成修复证明。 |
| `p0004` | supported | CliRunner 的 stdin wrapper 把有限输入正常耗尽转为 `EOFError`，Click `File("-")` 路径最终以 `Aborted` 和退出码 1 结束。 |
| `p0005` | supported | 同一 wrapper 直接用于 `sys.stdin`，普通有限 stdin 循环也会异常终止；与 `p0004` 是重叠根因。 |
| `p0006` | supported | 局部候选数歧义启发式拒绝了一个实际存在唯一全局一对一匹配的候选集合；两份报告的定向调用确认没有关联，而现有测试只覆盖真正歧义情况。 |
| `p0007` | unresolved | 有效约 1 MiB 源文件可生成约 240 万字符的摘要并重复进入 prompt，但没有模型 tokenizer/context 上限或实际拒绝请求的证据，无法证明触发模型输入限制。 |
| `p0008` | supported | `_safe_get` 的请求失败返回 `None` 可进入 terminal-task 的 300 秒缓存；旧 3 秒 TTL 对比缺少独立 before-side 证据。与 `p0009` 根因重叠。 |
| `p0009` | supported | 新增的 300 秒 terminal-task 缓存会保留 `_safe_get` 的 `None`。所引旧源码行 359–360 不在提供的 patch before hunk 内且无 base snapshot，因此旧 3 秒 TTL 主张未核实；与 `p0008` 根因重叠。 |
| `p0010` | supported | 按冻结 HTTPX 0.27.0 配置耗尽 10 个连接后，第 11 次 POST 在约 2 秒后触发 `PoolTimeout` 且未到达服务器；生产环境是否出现该并发与时长没有负载数据。 |

## 统计边界

9/0/1 是 10 条预测记录的逐条 disposition 计数，不是准确率、precision、recall、Skill 增益或其他质量分数；不得换算或称作“90% 准确率”。这些记录也不等于 10 个或 9 个独立缺陷：`p0004/p0005` 与 `p0008/p0009` 分别存在根因重叠，独立缺陷数未确定。该判定没有使用原 A/B 映射，没有形成有效 A/B 配对，也没有补写人类指标。原 human 表中的 `PENDING` 保持为历史输入；本次替代判定已完成，不以等待真人判定作为当前任务阻塞。
