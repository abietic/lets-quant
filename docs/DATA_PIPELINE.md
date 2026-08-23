# M1 数据管道

## 目标

M1 首先解决的不是“多接几个行情 API”，而是让任何回测都能回答四个问题：

1. 使用了哪份原始数据，供应商和版本是什么？
2. 在给定 `as_of` 时刻，哪些记录已经可见？
3. 清洗时采用了什么研究范围、复权口径和质量门禁？
4. 源数据修订后，旧回测是否仍指向原来的版本？

因此生产链路只允许：

```text
供应商/本地文件
  -> content-addressed raw snapshot
  -> available_at <= as_of
  -> calendar/lifecycle/event quality gates
  -> immutable curated dataset
  -> backtest/manual plan
```

回测过程不会临时访问网络。

## 三类时间

不要把以下时间混成一个字段：

| 时间 | 含义 |
|---|---|
| `date` / `ex_date` | 市场事件发生日期 |
| `available_at` | 该记录按当前数据契约可被策略读取的最早时间 |
| `fetched_at` | 本系统实际抓取并固化原始响应的时间 |

数据集只保留 `available_at <= as_of` 的行。`snapshot_id` 固化本地实际观察到
的版本，因此后续修订会生成新 ID，而不会覆盖旧版本。

这仍有一个重要边界：今天抓取的历史行情只能证明“今天观察到了这组历史值”，
不能证明供应商在多年前也返回同样的值。对财务报表、指数成分、评级等可修订
数据，必须额外购买或构建真正的 vintage/point-in-time 数据。

研究策略必须显式选择：

| `point_in_time_mode` | 行为 |
|---|---|
| `provider_publication` | 按行的发布时间回放；若快照是事后抓取则产生 warning |
| `local_observation` | 同时要求 `snapshot.fetched_at <= as_of`，否则数据集失败 |

前者适合当前日线价格研究，但不是 vendor vintage 证明；后者证据最强，却只能
回放本系统开始持续留存快照之后的历史。

## 原始行情契约

ETF 日线快照使用严格 CSV：

```csv
date,symbol,open,high,low,close,volume,amount,available_at,adjustment
2025-01-02,510300.XSHG,3.90,3.96,3.88,3.94,1000000,3930000,2025-01-02T15:30:00+08:00,hfq
```

- 时间戳必须带时区。
- `adjustment` 只能是 `none`、`qfq` 或 `hfq`，且必须与研究策略一致。
- AKShare 适配器假设日线在收盘 30 分钟后可用。
- 前复权历史会随未来公司行动变化；快照能固定一次观察结果，但不能消除这种
  经济含义上的修订风险。

复权价不是历史可执行价格。使用 `qfq/hfq` 时，回测里的收益路径有研究意义，
但整手数量、名义成交额和费用只是近似；系统会拒绝用这类数据集生成手工订单。
未复权数据中的现金分红和整股拆并股由显式会计账本处理；每天会从分录独立
重建现金、持仓和 NAV。碎股现金替代、红利税批次、代码变更和复杂公司行动
顺序仍未实现，遇到无法无歧义处理的事件会 fail closed。

辅助输入契约：

- `calendar.csv`：`date,is_open,available_at`
- `instruments.csv`：
  `symbol,exchange,asset_type,listed_on,delisted_on,available_at`
- `suspensions.csv`：`date,symbol,available_at`
- `corporate_actions.csv`：
  `symbol,event_type,ex_date,announced_at,cash_amount,ratio,available_at`

当前辅助输入会被哈希并写入数据集 manifest；后续应为交易所日历、证券主数据
和公司行动分别增加原始快照适配器。

## 质量门禁

`curate-data` 会生成 `quality.json`。以下问题使数据集变为 `fail`，不能被
回测读取：

- 日期/标的重复、OHLC 非法或复权口径不一致。
- 标的超出冻结研究范围。
- 行情落在非交易日、上市前或退市后。
- 开市日缺少任一在册标的价格；不会向前填充。
- 停牌或公司行动引用未知标的。
- 公司行动类型、比例或适用标的不合法，或事件无法按声明的价格语义处理。

数据集 schema v2 在 manifest 中声明：未复权价格使用 `explicit_ledger`，复权
价格使用 `embedded_in_adjusted_prices`。读取器仍兼容已通过质量门禁的 v1
数据集，并按其 `adjustment` 推断相同语义。

历史长度不足只产生 warning，因为短 fixture 仍需用于测试；真实研究不能据此
忽略样本长度风险。

## 目录和身份

```text
data/raw/<provider>/<dataset>/<snapshot-id>/
  manifest.json
  <payload>

data/curated/<dataset-id>/
  manifest.json
  quality.json
  research_policy.snapshot.json
  observations.csv
  prices.csv
  calendar.csv
  instruments.csv
  suspensions.csv
  corporate_actions.csv
```

`snapshot_id` 由供应商、版本、请求、数据权利声明和 payload 哈希共同决定。
`dataset_id` 由源快照、研究策略、as-of、辅助输入和数据构建代码哈希共同
决定。同一输入和代码重复执行是幂等的；任一项变化都会产生新目录。

## 数据源边界

`DailyBarsProvider` 是供应商接口，当前有可选
`AkshareEtfDailyBarsProvider`。AKShare
[`fund_etf_hist_em`](https://akshare.akfamily.xyz/data/fund/fund_public.html)
用于研究抓取，不进入回测进程。

[`config/data_providers.example.json`](../config/data_providers.example.json)
明确区分开源库许可和上游数据权利。`redistribution=not_assessed` 时，不应把
快照提交到 Git、公开对象存储或对外 API。

## 进入真实研究前

当前 CN ETF 文件是工程候选，不是已经确认的个人投资政策。至少还要确认：

1. 可投资资金、紧急备用金和未来三年的确定性支出。
2. 能承受的最大账面亏损，以及触发后是冻结调仓还是退出策略。
3. 实际券商佣金、最低收费、申赎/交易规则和可交易标的。
4. 用交易所或有明确授权的数据源替换示例日历、主数据和事件文件。
5. 拉取不少于研究策略要求的历史长度，再进行样本外和独立引擎复核。
