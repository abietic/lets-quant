# Bootstrap 不确定性

Bootstrap 不确定性用于回答“这段 test 日收益对样本路径有多敏感”，不是未来收益
预测、显著性检验或投资有效性证明。当前 protocol v1 固定参与
`experiment_input_id` 和结果哈希，不能在看到结果后修改块长或置信水平。

## 冻结协议

每个 test case 从 NAV 相邻行计算日对数收益，使用以下固定协议：

- 方法：`circular_moving_block`，即循环移动块 bootstrap。
- 块长：20 个日收益观测。
- 重采样次数：1000。
- 区间：95% 等尾 percentile interval。
- 最小样本：60 个日收益观测；不足时写入 `enabled=false` 和原因。
- 样本长度：每次仍取原始数量的日收益，最后一个块按需要截断。

移动块保留块内收益顺序，可部分保留短期自相关和波动聚集；循环边界允许块从序列
末尾接回开头，避免只有中间观测能成为完整块起点。它同时引入首尾相邻这一近似，
固定 20 日块长也不是所有资产或频率的唯一正确选择。

## 配对重采样

若策略配置了基准，每个 replicate 对策略和基准使用完全相同的源索引：

```text
strategy_total_return = exp(sum(sampled_strategy_log_returns)) - 1
benchmark_total_return = exp(sum(sampled_benchmark_log_returns)) - 1
relative_return = exp(sum(strategy_logs - benchmark_logs)) - 1
```

`relative_return` 表示策略财富相对基准财富的变化，不是两个普通总收益率的简单
相减。报告对三者分别保存原始点估计、下界、中位数、上界，以及 replicate 大于
零的比例。该比例只是描述性统计，不是 p-value。无基准时仍计算策略区间，基准和
相对字段保持 `null`。

## 确定性与审计

每个 case 的种子材料由实验 seed 和 `case_id` 组成，再与完整协议做 SHA-256。
每个 replicate、每个块的起点直接由 SHA-256 推导，不依赖 Python 伪随机数实现。
产物分别记录：

- `seed_sha256`：种子材料和协议的身份。
- `resample_schedule_sha256`：全部重采样源索引的日程哈希。
- `replicates_sha256`：策略、基准和相对收益数值的摘要哈希。
- 策略与基准点估计相对首末值的对账误差，绝对值不得超过 `1e-12`。

每个 `cases/<case>/bootstrap_uncertainty.json` 保存完整摘要；case snapshot、根
`summary.json` 和 `result_sha256` 同时包含该结果。实验 `manifest.json` 对除自身外
的所有产物记录逐文件 SHA-256。

抽样日程可以跨支持的 Python 版本核对，但普通浮点字段仍可能受运行时 `libm` 的
末位舍入影响。因此全局 `result_sha256` 的逐位重放以相同 Python 次版本为边界，
跨版本核对时应同时比较 manifest 的 `python_version`、抽样日程哈希和数值哈希。

## 跨折摘要

只有 test case 参与不确定性摘要。报告按“执行场景 × 参数变体”分组，跨折列出：

- 可用、禁用的 case 数和禁用原因。
- 各 case 点估计的最小值与最大值。
- 最低区间下界、最高区间上界和最低正收益 replicate 比例。
- test 窗口数量及是否重叠。

这些值以 case 为单位取极值。实现不会拼接各折收益、合并 bootstrap replicates，
也不会把共享日期或训练历史的 case 当成独立观测。

## 失败与解释边界

- NAV 日期必须唯一递增，NAV 和基准价格必须有限且严格为正。
- 配置基准后，所有 NAV 日期都必须存在基准价格；缺失即失败关闭。
- 对数收益必须精确重建首末总收益；溢出或对账失败会中止实验。
- 区间只描述已观察 test 路径在固定重采样假设下的变化，不是未来预测区间。
- 1000 次 replicate 只限制 Monte Carlo 粗糙度，不能增加真实独立样本量。
- 结果不能修复幸存者偏差、前视偏差、数据修订、参数挖掘或多重比较。
- 块长敏感性尚未形成独立协议；修改协议必须升级版本并重新生成全部实验。
