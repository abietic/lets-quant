# lets-quant

`lets-quant` 是一个本地优先、研究用途的个人量化投资工具。它当前解决的
不是“自动赚钱”，而是把投资规则变成可验证、可复现、可审计的决策流程。

当前版本支持：

- 严格校验的固定权重和透明动量过滤策略配置。
- 只暴露决策日及以前数据的 PIT-safe 策略上下文。
- 独立冻结的市场、标的、期限、基准、复权口径和最大回撤研究范围。
- 带供应商版本与许可声明的不可变原始数据快照。
- 按 `available_at <= as_of` 构建的清洗数据集和数据质量报告。
- 日线收盘价参考回测，信号在收盘后生成，下一交易日执行。
- 整手、佣金、最低佣金、卖出税费和滑点模型。
- 单标的权重、总暴露、单次换手率和最大回撤风险约束。
- 现金、静态目标权重和基准三类比较。
- 带原始快照、清洗数据集和配置哈希的回测产物。
- train/validation/test 时间隔离、成本与成交延迟压力实验。
- 多个滚动时间折和邻近策略参数敏感性矩阵，不自动选择最优参数。
- 六类确定性合成市场，用于验证软件语义和失败边界。
- 显式现金/持仓账本、公司行动入账和每日资产恒等式校验。
- 可持久化的离线 paper 订单状态机、事件幂等和重启恢复。
- 只供人工审批的订单建议，不连接券商、不自动下单。

示例中的 `ASSET_A`、`ASSET_B`、`ASSET_C` 和价格都是合成数据，不代表
任何真实资产或投资建议。示例费率也只是为了演示成本模型，使用真实数据前
必须按券商和当期规则重新配置。

## 快速开始

项目仅依赖 Python 3.9+ 标准库。

```bash
make check
make validate
make demo
make plan
make m1-validate
make m1-demo
make m15-demo
make m2-demo
make paper-demo
```

`make m1-demo` 使用短小的合成行情跑通“原始快照 -> point-in-time
清洗 -> 质量报告 -> 回测”链路。示例使用真实证券代码只是为了验证代码格式，
所有价格、停牌和公司行动记录都是合成数据，不是投资建议或真实历史记录。

`make m15-demo` 使用 780 个合成交易日运行透明动量过滤策略，并执行
train/validation/test 三段时间隔离和三组执行压力场景。它只证明研究流程
可重放、边界行为可测试，不证明策略对真实市场有效。

也可以直接运行：

```bash
PYTHONPATH=src python3 -m lets_quant backtest \
  --policy config/policy.example.json \
  --prices examples/prices.csv

PYTHONPATH=src python3 -m lets_quant plan-orders \
  --policy config/policy.example.json \
  --prices examples/prices.csv \
  --holdings examples/holdings.csv \
  --cash 25000
```

M1 回测读取的是不可变数据集目录，而不是临时联网：

```bash
PYTHONPATH=src python3 -m lets_quant backtest \
  --policy config/policy.cn-etf.example.json \
  --dataset data/curated/<dataset-id>
```

示例数据集采用 `hfq` 后复权，只能用于收益研究。`plan-orders --dataset ...`
会拒绝任何复权数据集，因为复权价不是可提交给券商的真实价格；订单规划必须
使用未复权的最新可执行价格。

`backtest` 会在 `artifacts/runs/<run-id>/` 中生成：

- `manifest.json`：输入路径、SHA-256、源码版本、Python 版本和模型假设。
- `policy.snapshot.json`：本次运行使用的完整策略快照。
- `metrics.json`：收益、波动、夏普、回撤、费用、换手率和基准指标。
- `nav.csv`：每日净值、现金、持仓和风险冻结状态。
- `signals.csv`：信号、稳定 decision ID、目标权重、决策证据、风险判断和
  计划订单。
- `trades.csv`：模拟成交、费用、税费和滑点。
- `ledger.csv`：本金、费用、分红、拆并股和滑点归因的独立会计分录。
- `accounting.csv`：每日从账本重建的现金、持仓、净值及对账误差。
- `dataset.snapshot.json`：使用 M1 数据集时，固化其 as-of、质量报告和源
  快照 lineage。

少于 252 个交易日时，结果会写入短样本警告；这时年化收益、波动率和夏普
比率不能作为决策依据。即使目录尚未初始化 Git，manifest 也会记录 Python
源码树的 SHA-256。

`plan-orders` 会在 `artifacts/plans/<plan-id>/` 中生成订单建议。输出始终包含：

```json
{
  "approval_required": true,
  "automatic_execution_allowed": false
}
```

当风险检查失败时命令返回退出码 `3`，但仍会保存被拦截的计划和原因，便于
排查。输入或配置错误返回退出码 `2`。

## M1.5 离线研究实验

策略只接收 `HistoricalContext`，其中 `dates` 永远截断到决策日；请求未来日期
会抛出 `FutureDataAccessError`。`momentum_filter` 使用明确的收盘价回看窗口，
历史不足时调仓状态为 `blocked`，不会把缺数据解释成清仓信号。

运行完整离线实验：

```bash
PYTHONPATH=src python3 -m lets_quant run-experiment \
  --policy config/policy.momentum.example.json \
  --experiment config/experiment.m1_5.example.json \
  --scenario regime_shift \
  --scenario-start 2022-01-03 \
  --scenario-trading-days 780
```

内置场景包括 `trend_up`、`trend_down`、`sideways`、`crash_recovery`、
`regime_shift` 和 `suspension`。实验定义必须恰好包含互不重叠且按时间排列的
train、validation、test 窗口，可以定义多组佣金、税费、滑点和成交延迟。

实验产物位于 `artifacts/experiments/<run-id>/`：

- 根目录保存实验、策略、市场来源快照、输入 ID 和结果 SHA-256。
- `summary.json` 汇总各窗口和执行场景，但显式标记不构成投资有效性证明。
- `cases/` 保存每个案例的指标、净值、决策证据和成交记录。

相同策略、实验定义、市场和源码应产生相同 `experiment_input_id` 与
`result_sha256`。目录时间戳可以不同，这两个摘要才是重放核对依据。

## M2 参数稳定性实验

`experiment` schema v2 可以声明多个滚动时间折，每个折都必须包含按时间顺序、
互不重叠的 train、validation 和 test 窗口；后一个折的 test 窗口必须向未来
推进。它还要求一个无覆盖项的 `configured` 基线，以及至少一个只改动明确参数
的邻近变体。

```bash
make m2-demo
```

示例分别测试 40、60、80 日回看和更严格的动量阈值。摘要按“时间折 + 执行
场景”展示各参数变体的 test 收益范围，并按变体汇总最差、最好和平均结果。
这些统计只描述敏感性，`automatic_parameter_selection` 和
`model_refit_per_fold` 都明确为 `false`；不能把表现最好的变体倒推成已验证策略。

## 显式会计与公司行动

未复权数据中的现金分红、拆股和合股会在除权日进入显式账本；`qfq/hfq`
数据中的同一事件只记录为 `corporate_action_embedded`，不会再次改变现金或
持仓。无法整股处理的拆并股、同标的同日多个含义不明的公司行动会 fail closed。

模拟器每天从全部账本分录独立重建现金和持仓，并计算预期 NAV。任何现金、
持仓或 NAV 误差都会终止回测，而不是只在报告里给 warning。

## 离线 Paper 状态机

`paper-demo` 重放 [示例事件](examples/paper/events.jsonl)，覆盖提交、确认、部分
成交、完整成交、卖出和拒单：

```bash
PYTHONPATH=src python3 -m lets_quant replay-paper-events \
  --initial-cash 100000 \
  --events examples/paper/events.jsonl \
  --state-out artifacts/paper/demo-state.json
```

恢复后继续重放使用 `--resume-state`。状态文件包含事件日志、订单、成交、账户、
已处理事件哈希和整体 SHA-256；相同事件重复到达不会重复成交，内容冲突、超额
成交、资金或持仓不足、跨订单复用柜台订单号以及非法状态迁移都会失败。

这是本地状态机测试工具，不是券商 paper adapter，不会联网，也不会自动读取
回测成交或发送订单。`automatic_execution_allowed` 始终为 `false`。

## 输入格式

价格数据必须提供每个交易日的完整价格，不会自动向前填充：

```csv
date,symbol,close
2025-01-02,ASSET_A,10.00
```

当前持仓格式：

```csv
symbol,quantity
ASSET_A,2000
```

策略配置见 [`config/policy.example.json`](config/policy.example.json)。
为了避免配置拼写错误或偷偷混入账号凭证，未知字段会被直接拒绝；`live`
执行模式也会被直接拒绝。

## M1 数据链路

研究范围见
[`config/research_policy.cn-etf.example.json`](config/research_policy.cn-etf.example.json)，
供应商使用权声明见
[`config/data_providers.example.json`](config/data_providers.example.json)。
两者都是工程示例；在投入真实资金前，必须根据你的期限、可承受亏损、账户
费率和实际标的重新确认。

核心命令：

```bash
# 1. 将已有 CSV 固化为不可变原始快照
PYTHONPATH=src python3 -m lets_quant snapshot-data \
  --provider local_csv \
  --provider-version 1 \
  --dataset-name etf_daily_bars \
  --input examples/m1/bars.csv \
  --license-manifest config/data_providers.example.json

# 2. 根据明确的历史截面构建清洗数据集
PYTHONPATH=src python3 -m lets_quant curate-data \
  --snapshot data/raw/local_csv/etf_daily_bars/<snapshot-id> \
  --research-policy config/research_policy.cn-etf.example.json \
  --calendar examples/m1/calendar.csv \
  --instruments examples/m1/instruments.csv \
  --suspensions examples/m1/suspensions.csv \
  --corporate-actions examples/m1/corporate_actions.csv \
  --as-of 2025-01-08T23:59:59+08:00
```

可选 AKShare 适配器只负责抓取 ETF 日线并立即落原始快照：

```bash
python3 -m pip install -e '.[akshare]'
PYTHONPATH=src python3 -m lets_quant fetch-akshare \
  --research-policy config/research_policy.cn-etf.example.json \
  --start 2018-01-01 \
  --end 2025-12-31 \
  --license-manifest config/data_providers.example.json
```

核心工具继续支持 Python 3.9；联网数据环境建议单独使用 Python 3.11+，避免
旧系统 Python 的 TLS/LibreSSL 兼容性警告。

AKShare 的 MIT 许可是代码许可，不等于其上游行情的再分发或商业使用授权。
当前适配器抓取的是“现在看到的历史序列”，原始快照可以防止以后静默修订，
但不能证明供应商在某个历史时点返回过完全相同的序列。需要财务指标、成分股
历史或其他易修订字段时，应使用明确提供 point-in-time/vintage 能力的数据源。
示例研究策略将这一取舍显式设为
`point_in_time_mode=provider_publication`；改为 `local_observation` 后，任何
晚于 as-of 才抓到的快照都会直接失败。
完整约束见 [M1 数据管道](docs/DATA_PIPELINE.md)。

## 回测边界

当前回测器是可测试的“目标权重参考模拟器”，不是完整交易所撮合引擎：

- 不模拟盘口深度、部分成交概率、涨跌停和盘中流动性。
- M1 数据集可将停牌标记为不可交易，订单会被拒绝但不会自动排队。
- 未复权价格通过账本处理现金分红和整股拆并股；复权价格不会重复入账。
- 未复权拆并股跨越信号日和执行日时，原订单数量会被标记为过期并拒绝，等待
  下一次策略决策重新计算，不猜测真实柜台的改单规则。
- 暂不处理碎股现金替代、代码变更、红利税批次或复杂公司行动顺序。
- 使用复权价格时，整手数量和交易成本是研究近似值，不能解释成历史可成交
  数量或真实费用。
- 不处理融资、融券、期权、期货或杠杆。
- 最大回撤触发后只冻结新调仓，不会擅自平仓。
- 多个买单资金不足时按标的代码顺序处理，因此不能用于评估高频执行质量。
- 实验中的成交延迟只是日线压力参数，不模拟真实排队位置或部分成交概率。
- 当前滚动时间折会在每个窗口重置组合，但不会训练或拟合模型；它是时间稳定性
  检查，不是机器学习意义上的完整 walk-forward retraining。

在使用真实资金前，应使用 RQAlpha、目标券商回测环境或其他成熟引擎进行
第二套独立验证，并在模拟盘中核对订单、成交和持仓。

## 项目文档

- [架构和安全不变量](docs/ARCHITECTURE.md)
- [分阶段路线图](docs/ROADMAP.md)
- [M1 数据管道](docs/DATA_PIPELINE.md)

本项目不提供投资建议。接入中国证券市场自动交易前，需要向开户券商确认
程序化交易权限、报告义务、接口限制和当前监管要求。
