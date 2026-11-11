# 效果验证

2026-11-11 完成最终验收。测试源码提交为 `723b0918ee0ed54e23260cf1fdce7ef78307cdea`。当前交付的可执行代码与该提交一致，后续提交只整理文档和验证记录。

35 项自动化测试全部通过。19 组 CLI 离线验收和同一套 MiniMax-M3.1-Flash 真实模型验收全部通过。源码归档在新建的独立 Python 3.9.6 环境中运行，无需安装第三方依赖。 [干净环境验证](../examples/final-package-verification.txt) · [自动化测试结果](../examples/test-results.txt) · [离线验收报告](../examples/final-acceptance.mock.json) · [真实模型完整报告](../examples/final-acceptance.minimax.json)

## 必须验收点

报告内的 cases 按下表名称定位。每组使用独立临时数据库，组内多阶段请求共享数据库。phases 记录各阶段输入和草稿数量，actual 包含真实工具 Trace，database_rows 与 confirmation_rows 来自运行后的 SQLite 查询。

| 验收点 | 报告用例 | 实际结果 |
| --- | --- | --- |
| 正常排期 | normal-1、normal-2、normal-3 | 三次都审核通过并保存，数据库各 1 份草稿 |
| 素材信息缺失 | missing-and-answered | 先调用 ask_user 追问版权记录，补齐 E02 后复审并保存，草稿数 0 → 1 |
| 规则风险 | rule-risk | 命中 R01，返回 blocked，零草稿 |
| 时段冲突 | conflict-1 至 conflict-3 | 三次都查询替代时段，建议 19:00 待确认，零草稿 |
| 重复提交 | confirmation-ticket、confirmed-and-replayed | 同一请求复用同一个草稿 ID，数据库保持 1 份 |
| 保存失败 | save-failure | 返回 save_failed，事务回滚，零草稿 |

正常与冲突用例使用相同请求和提示词，只改变日历。正常例中 query_slots(exact) 后的动作是 save_draft，冲突例中下一步是 query_slots(alternatives)。这两条真实模型路径验证了观察结果变化后的动作选择。

## 补充业务与工程边界

| 用例或测试 | 实际结果 |
| --- | --- |
| shift-candidate、no-candidate | 19:00 被占用后改提 20:00；全部候选被占用时明确返回时段冲突 |
| supplement-clears-risk、supplement-reveals-risk | 缺项补查后重新审核，分别保存和阻断，并保留 E05、E06 来源 |
| evidence-conflict-and-resolved | 列出 E02 与 E02-REVIEW 的相反结论并追问，操作者核实后复审再保存，草稿数 0 → 1 |
| metadata-evidence-conflict | 元数据已授权、单条凭证未授权时也返回 evidence_conflict，列出双方来源并追问，零草稿 |
| confirmation-ticket | 待确认、篡改时段、正确确认、重复确认四个独立 CLI 阶段的草稿数为 0 → 0 → 1 → 1 |
| batch-slot-contention | 第一项保存，第二项查询替代并等待确认，数据库保持 1 份 |
| step-limit | 达到两轮测试上限时返回 incomplete 和 step_limit，保留已有 Trace |
| 并发与回滚测试 | 同请求并发复用，不同请求不能占用同一时段；保存失败同时回滚草稿与确认关联 |
| 授权与规则变化测试 | 越权改期被拒绝，确认后的规则变化仍能阻断，查询后被抢占的候选不生成待确认记录 |

## 上下文验证

单项上下文默认预算 24 KiB，超过一半尝试压缩较早完整交互，单条工具观察最多 2 KiB。程序检查点保留审核、补查、候选和保存状态。必要上下文仍超限时，在模型调用前返回 context_budget_exceeded。

自动化测试验证了长工具观察限长、保留最新模型响应及匹配工具 ID、压缩后等待改期确认，以及保存后超限仍报告真实草稿凭据。真实模型专项记录还验证了包含 8 万字合成转录稿的素材预览和压缩路径。 [上下文专项记录](../examples/context-minimax.output.json)

## 失败记录与限制

早期并行验证曾触发 HTTP 429，后续增加有界重试并对验收请求限速。早期补查用例曾因多读取一次素材而触发过严的动作相邻断言，按实际的补查、复审和保存因果关系修正了断言。历史报告保留供复盘，最终验收以上述 final-acceptance 报告为准。 [早期限流记录](../examples/minimax-regression-rate-limited.json) · [历史工作流报告](../examples/minimax-workflow-regression.json)

数据为合成资料，模型效果结论限于这套小规模场景。单渠道、固定 +08:00、离散候选和八轮上限为当前范围。CLI 信任操作者提供的授权及证据，证据的现实有效性由操作者核实。长期记忆、多用户身份、实时日历同步和生产吞吐尚未纳入实现范围。
