# 离线 Paper 告警运营

这层能力把已经校验的 Paper 运营审计报告变成可追踪的告警生命周期。它解决：

1. 同一告警是否只生成一个确定性的待投递项。
2. 人工确认和临时静默是否有身份、操作者、原因和时间证据。
3. 未确认告警是否按策略重复提醒并升级。
4. 本地投递中断后是否能根据回执恢复，而不重复记录。

当前唯一 sink 是本地 `JSONL`。系统不会发邮件、Webhook 或即时消息，也不会连接
券商。告警状态和通知载荷中的 `automatic_execution_allowed`、
`automatic_external_delivery_allowed` 始终为 `false`。

## 运行

先生成 Paper 审计报告，再同步告警状态：

```bash
PYTHONPATH=src python3 -m lets_quant sync-paper-alerts \
  --report artifacts/paper/audit-demo-report.json \
  --policy config/paper_alert_policy.example.json \
  --now 2025-01-03T09:35:30+08:00 \
  --state-out artifacts/paper/alert-state.json
```

将待投递项写入本地回执日志：

```bash
PYTHONPATH=src python3 -m lets_quant dispatch-paper-alerts \
  --state artifacts/paper/alert-state.json \
  --delivery-log artifacts/paper/alert-deliveries.jsonl \
  --delivered-at 2025-01-03T09:35:31+08:00 \
  --state-out artifacts/paper/alert-state.json
```

完整 fixture 链路可运行 `make paper-alert-demo`。

## 状态与恢复

`sync-paper-alerts` 会先验证审计报告的 `report_sha256`，再维护：

- `open`：尚未确认，且不在有效静默窗口内。
- `acknowledged`：人工已看到；这不会消除底层审计问题。
- `silenced`：在有截止时间的维护窗口内暂不生成通知。
- `resolved`：后续审计报告中已经不存在该告警。

同一 `alert_id` 消失后再次出现会产生新的 occurrence，之前的确认和静默不会沿用。
报告时间不能倒退；同一 `as_of` 出现不同报告哈希会失败关闭。策略变化也不能静默
套用到已有状态，需要显式建立新状态。

每个待投递项由 `alert_id + occurrence + sequence + level + channel` 产生稳定 ID。
本地 dispatch 先合并已有回执，再更新状态：若进程在“写回执”和“写状态”之间
中断，重试会识别同一 `notification_id`，验证载荷哈希后恢复，不会追加重复回执。

## 人工动作

动作使用 JSONL，每行一个严格对象：

```json
{"action_id":"ack-20250103-1","alert_id":"<alert-id>","action":"acknowledge","actor":"operator","occurred_at":"2025-01-03T09:40:00+08:00","reason":"已核对外部快照来源"}
```

临时静默额外要求 `silence_until`；提前解除使用 `unsilence`，且不能携带该字段。
应用动作时同时传入现有状态：

```bash
PYTHONPATH=src python3 -m lets_quant sync-paper-alerts \
  --report artifacts/paper/audit-demo-report.json \
  --policy config/paper_alert_policy.example.json \
  --resume-state artifacts/paper/alert-state.json \
  --actions artifacts/paper/operator-actions.jsonl \
  --now 2025-01-03T09:40:00+08:00 \
  --state-out artifacts/paper/alert-state.json
```

`action_id` 幂等；复用相同 ID 但改变内容会失败。动作不能早于告警首次出现，不能
作用于已解决告警，未来时间戳和无截止时间的静默也会被拒绝。

## 提醒与升级

策略文件分别为 `critical` 和 `warning` 设置重复间隔与升级等待时间。首次提醒
立即生成；之后只有上一条已经得到本地投递回执且达到重复间隔才生成下一条。
未确认告警达到升级等待时间后使用 `escalated` level。人工确认停止后续提醒；
静默只在截止时间前抑制提醒，截止时精确恢复。

这些规则只证明告警队列和本地回执可复现，不证明人已收到外部通知。接入真实
邮件、飞书或 PagerDuty 类 sink 时，必须保留 notification ID、供应商回执、失败
重试和权限边界，并继续禁止 sink 修改订单或账户。
