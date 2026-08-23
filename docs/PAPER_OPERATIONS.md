# 离线 Paper 运营审计

这套审计能力解决的不是自动下单，而是模拟执行阶段的四个基本问题：

1. 当前行情和任务状态是否仍然新鲜。
2. 风险冻结后是否仍有订单处于活动状态。
3. 预期订单与实际模拟成交是否发生数量、价格、费用或延迟偏差。
4. 本地 Paper 状态是否与导入的外部账户快照一致。

审计过程不联网，不主动查询券商，也不修改订单或账户。无论结果如何，报告中的
`automatic_execution_allowed` 始终为 `false`。

## 数据流

```text
离线 Paper 事件 -> 带校验和的 Paper 状态 ---------+
                                                   |
订单预期、报价、任务、风险状态 --------------------+--> 运营审计报告
                                                   |
外部账户导出或 fixture -> 规范化账户快照 ----------+
```

审计报告同时绑定 `paper_state_sha256`、`audit_input_sha256` 和自身的
`report_sha256`。修改任一输入都会改变报告身份。

## 运行

```bash
PYTHONPATH=src python3 -m lets_quant replay-paper-events \
  --initial-cash 100000 \
  --events examples/paper/audit_events.jsonl \
  --state-out artifacts/paper/audit-demo-state.json

PYTHONPATH=src python3 -m lets_quant audit-paper-state \
  --state artifacts/paper/audit-demo-state.json \
  --audit-input examples/paper/audit_input.json \
  --report-out artifacts/paper/audit-demo-report.json
```

也可以直接运行 `make paper-audit-demo`。示例会返回 `review_required`，因为外部
账户数据明确标记为 `fixture`；它证明对账契约可运行，但不冒充券商真相源。

## 输入契约

完整示例见
[`examples/paper/audit_input.json`](../examples/paper/audit_input.json)。根对象包含：

| 字段 | 含义 |
|---|---|
| `as_of` | 带时区的审计截面；Paper 事件和观测不能晚于该时点 |
| `thresholds` | 报价、任务、账户、活动订单、成交延迟、滑点、费用和现金容差 |
| `required_tasks` | 本次审计必须看到健康观测的任务列表 |
| `quotes` | 活动订单涉及标的的最新报价及观测时间 |
| `order_expectations` | 与 `decision_id` 关联的数量、价格、费用、终态和截止时间 |
| `task_checks` | 数据刷新、策略运行或事件同步任务的健康观测 |
| `risk_state` | 风险冻结状态及原因 |
| `external_account` | 规范化的现金、持仓和完整订单快照；可以暂时为 `null` |

所有对象拒绝未知字段，所有金额必须有限且非负，所有时间戳必须带时区。审计不会
把缺字段、未来观测或拼写错误猜成默认值。

## 报告状态

| 状态 | CLI 退出码 | 含义 |
|---|---:|---|
| `pass` | `0` | 没有告警；导入快照声明来自 broker 且与本地状态一致 |
| `review_required` | `0` | 没有阻断项，但缺外部快照或只使用 fixture，需要人工确认 |
| `blocked` | `3` | 存在 critical 告警，禁止把状态解释为可继续执行 |

输入或校验和错误返回 `2`。即使状态为 `blocked`，报告仍会先原子写入，便于人工
处理和事后审计。

主要告警包括：

- `missing_quote`、`stale_quote`：活动订单缺行情或行情过期。
- `open_order_stale`：活动订单超过允许时长。
- `required_task_missing`、`task_failed`、`task_observation_stale`：任务不可用。
- `risk_frozen`：风险门禁已冻结。
- `fill_quantity_deviation`、`fill_price_deviation`、`fill_fee_deviation`、
  `fill_latency_exceeded`：实际模拟成交偏离预期。
- `missing_order_expectation`、`expected_order_missing`、
  `order_contract_mismatch`：订单没有可追溯决策或内容不一致。
- `account_cash_mismatch`、`account_position_mismatch`、
  `account_order_mismatch`：本地状态与外部快照不一致。

## 外部真相源边界

`source_kind=fixture` 永远产生 warning。`source_kind=broker` 只表示输入声称是已经
规范化的券商导出；当前代码不会验证文件来源、登录券商或主动刷新数据。因此，
手工把字段改成 `broker` 不能证明已经完成 M3。

未来适配器应位于 `execution/`，负责认证、查询和规范化，但不能绕过本审计：

- 查询结果必须记录来源和观测时间。
- 恢复时以券商现金、持仓和订单为真相源，本地缓存只作重放证据。
- 快照过期、分页不完整、状态无法映射或网络失败时不得生成 `pass`。
- 所有差异必须先形成告警和人工处理记录，不能静默覆盖本地状态。
