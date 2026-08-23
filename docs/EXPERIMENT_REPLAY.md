# 实验离线重放

`replay-experiment` 用冻结输入重新执行一个可移植研究实验，并将新结果与已保存
产物逐项比较。它回答的是“当前代码在相同运行时能否复现该结果”，不是“该结果
是否真实、可信或值得投资”。

## 使用方式

```bash
PYTHONPATH=src python3 -m lets_quant replay-experiment \
  --experiment-run artifacts/experiments/<run-id>
```

也可以生成一个确定性合成实验并立即重放：

```bash
make experiment-replay-demo
```

重放通过返回退出码 `0` 和 JSON 报告。完整性失败、不支持的数据来源、运行时不
匹配或重放结果漂移返回退出码 `2`，错误写入 stderr。

## 重放顺序

命令按以下顺序失败关闭：

1. 先运行 `verify-experiment` 的全部路径、文件哈希和跨文件一致性检查。
2. 要求当前 `platform.python_version()` 与 manifest 记录的完整版本完全一致。
3. 要求目录包含参与 manifest 哈希的 `market.snapshot.json`。schema v2 还会通过
   `replay_input` 独立核对文件哈希、规范市场哈希、来源类型和来源哈希。
4. 严格解析日期、价格、可交易标记和公司行动，重建 `MarketData`，再核对重建后
   的规范市场身份与快照逐项相同。
5. 从 `policy.snapshot.json` 和 `experiment.snapshot.json` 加载冻结配置，调用当前
   研究内核重新执行全部 case。
6. 精确比较 `experiment_input_id`、`result_sha256` 和完整 `summary.json`；任一项
   不同都判定重放失败。

不提供跨 Python 版本强制覆盖开关。不同补丁版本也可能改变标准库或底层浮点行为，
因此只能先静态验证，再在 manifest 记录的运行时中重放。

相同 Python 版本只是必要条件，不保证升级后的研究代码仍产生旧结果。若当前代码
改变了策略、会计、指标或序列化行为，旧产物应在结果哈希比较处明确失败；这正是
重放需要暴露的行为漂移，而不是兼容层应静默吞掉的差异。

## 数据来源边界

| 市场来源 | schema v2 静态验证 | 实际重放 | 冻结内容 |
|---|---:|---:|---|
| 确定性合成市场 | 是 | 是 | 日期、价格、可交易标记和公司行动 |
| 独立 CSV | 是 | 是 | CSV 解析后实际进入内核的规范市场 |
| 清洗数据集 | 是 | 是 | PIT 清洗后的规范市场，并保留 dataset snapshot lineage |

重放不读取 manifest 中可能已经失效的原始绝对路径。schema v2 保存的是实际进入
研究内核的规范市场，不复制原始供应商响应、OHLCV 全字段或许可文件。因此它能
证明行为复现，却不能单独重新证明数据清洗过程或原始来源真实性。旧 schema 0/1
若没有 `market.snapshot.json`，仍保持 verify-only。

## 报告语义

通过报告的关键字段如下：

```json
{
  "status": "pass",
  "integrity_verified_before_replay": true,
  "embedded_market_snapshot_verified": true,
  "portable_replay_input_verified": true,
  "market_source_type": "curated_dataset",
  "python_version_match": true,
  "experiment_input_id_match": true,
  "result_sha256_match": true,
  "summary_match": true,
  "replay_performed": true,
  "artifact_authenticity_verified": false,
  "investment_validity_established": false,
  "automatic_execution_allowed": false
}
```

- **行为复现**：冻结输入经当前代码重跑后得到相同结果哈希和摘要。
- **源码身份**：manifest 会记录源码树哈希，但当前重放不把当前安装包认证为当时的
  源码包；结果相同是行为证据，不是软件供应链证明。
- **产物真实性**：目录内部哈希可以被有写权限的人整体重做，当前没有外部签名或
  透明日志，因此保持 `artifact_authenticity_verified=false`。
- **投资有效性**：无论输入来自合成场景、CSV 还是清洗数据集，成功重放都不会
  证明收益可持续，也不会改变 `investment_validity_established=false`。

静态完整性检查的详细边界见
[实验产物验证](EXPERIMENT_VERIFICATION.md)。
