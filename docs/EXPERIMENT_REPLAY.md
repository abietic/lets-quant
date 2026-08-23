# 实验离线重放

`replay-experiment` 用冻结输入重新执行一个自包含研究实验，并将新结果与已保存
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
3. 要求目录包含参与 manifest 哈希的 `market.snapshot.json`，并验证其规范 JSON
   哈希与 `manifest.market_source.sha256` 一致。
4. 严格解析日期、价格、可交易标记和公司行动，重建 `MarketData`，再核对重建后
   的规范市场身份与快照逐项相同。
5. 从 `policy.snapshot.json` 和 `experiment.snapshot.json` 加载冻结配置，调用当前
   研究内核重新执行全部 case。
6. 精确比较 `experiment_input_id`、`result_sha256` 和完整 `summary.json`；任一项
   不同都判定重放失败。

不提供跨 Python 版本强制覆盖开关。不同补丁版本也可能改变标准库或底层浮点行为，
因此只能先静态验证，再在 manifest 记录的运行时中重放。

## 数据来源边界

| 市场来源 | 静态验证 | 实际重放 | 原因 |
|---|---:|---:|---|
| 内嵌确定性合成市场 | 是 | 是 | 完整日期、价格、停牌和公司行动都冻结在目录中 |
| 独立 CSV | 是 | 否 | 当前 manifest 只绑定外部文件，不把可移植副本封装进实验目录 |
| 清洗数据集 | 是 | 否 | 尚未封装完整不可变数据集、质量报告和加载契约 |

后续若支持外部来源重放，应先定义可移植输入 bundle，而不是读取 manifest 中可能
已经失效或被替换的绝对路径。

## 报告语义

通过报告的关键字段如下：

```json
{
  "status": "pass",
  "integrity_verified_before_replay": true,
  "embedded_market_snapshot_verified": true,
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
- **投资有效性**：确定性合成数据只验证软件语义。成功重放不会证明收益可持续，
  也不会改变 `investment_validity_established=false`。

静态完整性检查的详细边界见
[实验产物验证](EXPERIMENT_VERIFICATION.md)。
