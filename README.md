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
- 可选且纳入哈希的初始持仓快照，首日现金、持仓和企业行动进入同一账本。
- 受保护的证券主数据快照与跨引擎逐标的代码映射证据。
- train/validation/test 时间隔离、成本与成交延迟压力实验。
- 多个滚动时间折和邻近策略参数敏感性矩阵，不自动选择最优参数。
- 基于前一交易日基准历史的冻结市场阶段标签、逐日证据和对数收益归因。
- 仅用于 test 窗口的确定性移动块 bootstrap 收益区间，策略与基准配对重采样。
- 对实验目录执行路径安全、逐文件哈希、case 身份、摘要绑定和 CSV 日期轴验证。
- 带严格输入指纹和差异报告的跨引擎候选产物契约与对账器。
- 可选 VectorBT 1.1.0 适配器，消费独立 CSV 或清洗数据集，复核停牌拒绝、成交、
  企业行动回调、费用、持仓和 NAV。
- 可选 RQAlpha 6.3.0 事件驱动适配器，独立复算 PIT 策略信号，并原生复核订单、
  清洗 OHLCV、停牌前置拒绝、企业行动、部分成交、撤拒单和账户。
- 六类确定性合成市场，用于验证软件语义和失败边界。
- 显式现金/持仓账本、公司行动入账和每日资产恒等式校验。
- 可持久化的离线 paper 订单状态机、事件幂等和重启恢复。
- 离线 Paper 运营审计：行情/任务新鲜度、风险冻结、成交偏差和导入账户对账。
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
make experiment-verify-demo
make paper-demo
make paper-audit-demo
```

核心命令仍是零运行时依赖。跨引擎适配器使用独立环境：

```bash
python3.13 -m venv .venv-vectorbt
source .venv-vectorbt/bin/activate
python -m pip install -e '.[vectorbt]'
make vectorbt-test vectorbt-demo

python3.9 -m venv .venv-rqalpha
source .venv-rqalpha/bin/activate
python -m pip install -e '.[rqalpha]'
make rqalpha-test rqalpha-demo
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
  --prices examples/prices.csv \
  --initial-holdings examples/holdings.csv

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

- `manifest.json`：输入路径、逐文件 SHA-256、源码版本、Python 版本和模型假设。
- `policy.snapshot.json`：本次运行使用的完整策略快照。
- `initial_holdings.csv`：规范化、参与运行身份的首日企业行动前持仓；未传入时
  仍写入只有表头的空快照。
- `instrument_master.csv`：规范化证券代码、交易所、资产类型和上市生命周期；
  裸价格输入会明确标记为 `SYNTH/SYNTHETIC`，不会冒充真实主数据。
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

`--initial-holdings` 只接受策略标的内的非负整手多头持仓。配置中的
`portfolio.initial_cash` 仍是实际现金，持仓市值在其上额外计入首日 NAV；现金、
静态目标权重和基准三类比较都从该首日 NAV 起算。首日若有未复权企业行动，
先登记导入持仓，再处理分红或拆并股。碎股、策略外持仓和做空持仓会失败关闭。

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

- 根目录保存实验、策略、市场来源快照、输入 ID、结果 SHA-256 和逐文件校验和。
- `summary.json` 汇总各窗口、执行场景和市场阶段，但显式标记不构成投资有效性
  证明。
- `cases/` 保存每个案例的指标、净值、决策证据、成交记录、逐日
  `regime_attribution.csv` 和 `bootstrap_uncertainty.json`。

新实验 manifest 使用 artifact schema v1。可以独立验证现有目录：

```bash
PYTHONPATH=src python3 -m lets_quant verify-experiment \
  --experiment-run artifacts/experiments/<run-id>
```

验证器拒绝路径穿越、符号链接、缺失/额外文件、哈希漂移，以及 case snapshot、
bootstrap、metrics、CSV 日期轴和根摘要之间的矛盾；v0.15 生成的无 schema manifest
会以 legacy schema 读取。成功报告中的 `file_hashes_verified` 和
`cross_file_consistency_verified` 为 `true`，但 `replay_performed` 与
`artifact_authenticity_verified` 仍为 `false`。完整边界见
[实验产物验证](docs/EXPERIMENT_VERIFICATION.md)。

相同策略、实验定义、市场、源码和 Python 次版本应产生相同
`experiment_input_id` 与 `result_sha256`。目录时间戳可以不同，这两个摘要才是
同运行时重放的核对依据；跨 Python 次版本比较还应读取 manifest 中的运行时版本。

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

同一实验还使用冻结的 market-regime protocol v1 做描述性归因。日期 `t` 的
阶段标签只读取基准截至 `t-1` 的 60 日历史，收益贡献使用可加总的对数收益；
test 摘要按执行场景、参数变体和阶段跨折比较，但不合并重叠窗口，也不参与策略
决策或自动选参。无基准策略会显式禁用归因。完整契约见
[市场阶段归因](docs/MARKET_REGIME_ATTRIBUTION.md)。

test case 还使用 bootstrap protocol v1 对日对数收益做确定性循环移动块重采样：
固定 20 日块长、1000 次样本、95% 分位区间和至少 60 个日收益观测。策略与基准
共用每个块的索引，并报告策略收益、基准收益和策略相对基准财富变化；训练与验证
窗口明确禁用。跨折摘要只比较各 case 区间边界，不拼接或池化窗口，也不输出
`p-value`。无基准时仍可计算策略区间。完整契约见
[Bootstrap 不确定性](docs/BOOTSTRAP_UNCERTAINTY.md)。

## M2 跨引擎执行对账

两个适配器都不读取参考成交来决定候选结果。VectorBT 读取冻结订单意图和绑定
哈希的原始价格，独立生成共享现金、持仓、NAV 和订单记录；RQAlpha 默认读取
策略快照与截至信号日可见的价格历史，使用独立于参考回测器的策略实现重新生成
decision ID、目标权重、换手率和订单意图，再通过原生事件循环生成订单受理、
成交、部分成交后撤余单、拒单、费用和账户变化：

```bash
PYTHONPATH=src python -m lets_quant validate-vectorbt \
  --reference-run artifacts/runs/<run-id> \
  --prices examples/prices.csv

PYTHONPATH=src python -m lets_quant validate-rqalpha \
  --reference-run artifacts/runs/<run-id> \
  --prices examples/prices.csv
```

参考回测来自 M1 清洗数据集时，必须保留完整数据语义，不能降级传入裸
`prices.csv`：

```bash
PYTHONPATH=src python -m lets_quant validate-vectorbt \
  --reference-run artifacts/runs/<run-id> \
  --dataset data/curated/<dataset-id>

PYTHONPATH=src python -m lets_quant validate-rqalpha \
  --reference-run artifacts/runs/<run-id> \
  --dataset data/curated/<dataset-id>
```

候选目录包含带 SHA-256 的 manifest、规范化 NAV、成交、指标、
`instrument_mapping.csv` 和对账报告。参考
账本含企业行动时还必须包含 `corporate_actions.csv`，逐事件记录来源类型、会计
处理、现金/数量变化和参考 ID。RQAlpha 候选还包含 `signals.csv`、`orders.csv`
与 `events.csv`；对账器会分别验证策略决策与拟议订单、事件顺序、累计成交、
费用和最终状态。任何输入漂移、文件篡改或结果差异都会进入 `blocked`，命令
返回退出码 `3`。

RQAlpha 的成交数量和生命周期由引擎原生生成；现金缓冲的暂存/恢复仍是适配层
映射。`validate-rqalpha` 默认使用 `independent_policy`；诊断旧执行链路时可显式
传入 `--decision-mode frozen_orders`，但该模式不会验证策略决策。VectorBT 仍只
重放冻结订单意图。两个适配器支持绑定哈希的独立 CSV 和质量通过的清洗日线
数据集，并复核停牌拒绝；支持绑定快照的整手非零初始持仓，但仍只做多。复权
数据中的公司行动按“已嵌入价格”处理；未复权现金分红和可保持整股的拆并股会
进入独立事件证据，
跨越拆并股的待执行订单会因数量过期而拒绝。VectorBT 通过适配器回调改变模拟
状态，RQAlpha 使用原生分红/拆并股账户路径。

VectorBT 的初始持仓通过首个 segment 回调注入；RQAlpha 使用原生
`init_positions`。为让 RQAlpha 在运行首日正确筛选企业行动，适配器添加一个仅供
账户初始化的首日前哨日，并使用首日收盘估值；该前哨日不会进入策略历史、NAV
日期轴或候选信号。

清洗数据集路径会把受保护 `instruments.csv` 固化到参考运行。VectorBT 保留规范
代码作为列标识；RQAlpha 对当前支持的 `XSHG/XSHE`、`ETF/CS` 使用规范代码、
交易所、资产类型和上市日期。裸价格没有可验证主数据，只能使用显式合成回退；
两条路径都会提交逐标的映射文件并由 schema v7 对账。

完整契约与扩展方法见
[跨引擎验证](docs/CROSS_ENGINE_VALIDATION.md)。

## 显式会计与公司行动

未复权数据中的现金分红、拆股和合股会在除权日进入显式账本；`qfq/hfq`
数据中的同一事件只记录为 `corporate_action_embedded`，不会再次改变现金或
持仓。无法整股处理的拆并股、同标的同日多个含义不明的公司行动会 fail closed。
当前现金分红按每股税前金额在除权日入账，不处理红利税批次或到账日差异。

模拟器每天从全部账本分录独立重建现金和持仓，并计算预期 NAV。任何现金、
持仓或 NAV 误差都会终止回测，而不是只在报告里给 warning。跨引擎候选还必须
逐事件匹配参考账本，不能只让最终 NAV 恰好相同。

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

## M3 离线运营审计

`audit-paper-state` 将带校验和的 Paper 状态与严格的运营审计输入进行比较：

```bash
PYTHONPATH=src python3 -m lets_quant audit-paper-state \
  --state artifacts/paper/audit-demo-state.json \
  --audit-input examples/paper/audit_input.json \
  --report-out artifacts/paper/audit-demo-report.json
```

它会检查活动订单行情是否过期、任务是否失败、风险是否冻结、订单是否超时，
并按 `decision_id` 比较预期与实际成交数量、均价、费用和延迟。可选的外部账户
快照用于核对现金、持仓、订单状态、成交数量和柜台订单号。

报告状态为 `pass`、`review_required` 或 `blocked`。存在 critical 告警时命令
返回退出码 `3`，但仍保存带 `report_sha256` 的报告。fixture 对账即使完全一致
也只能得到 `review_required`；当前实现不验证文件来源，不连接券商，也不允许
自动执行。输入格式、告警和适配器边界见
[离线 Paper 运营审计](docs/PAPER_OPERATIONS.md)。

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
- 初始持仓必须是策略范围内的整手多头；这不是券商账户导入或恢复真相源。
- 清洗数据集的主数据会进入跨引擎映射；裸价格只有合成身份，不能据此推断真实
  交易所规则、结算周期或资产分类。
- 使用复权价格时，整手数量和交易成本是研究近似值，不能解释成历史可成交
  数量或真实费用。
- 不处理融资、融券、期权、期货或杠杆。
- 最大回撤触发后只冻结新调仓，不会擅自平仓。
- 多个买单资金不足时按标的代码顺序处理，因此不能用于评估高频执行质量。
- 实验中的成交延迟只是日线压力参数，不模拟真实排队位置或部分成交概率。
- 当前滚动时间折会在每个窗口重置组合，但不会训练或拟合模型；它是时间稳定性
  检查，不是机器学习意义上的完整 walk-forward retraining。
- bootstrap 区间依赖收益过程在局部可重采样的假设和固定 20 日块长；它不是未来
  收益预测区间，也不能消除数据偏差、策略选择偏差或测试窗口重叠。

当前已经接入 RQAlpha 作为第二个事件驱动验证引擎；使用真实资金前仍需在目标
券商回测和模拟环境核对订单、成交、持仓及恢复真相源。

## 项目文档

- [架构和安全不变量](docs/ARCHITECTURE.md)
- [分阶段路线图](docs/ROADMAP.md)
- [M1 数据管道](docs/DATA_PIPELINE.md)
- [Bootstrap 不确定性](docs/BOOTSTRAP_UNCERTAINTY.md)
- [实验产物验证](docs/EXPERIMENT_VERIFICATION.md)
- [跨引擎验证](docs/CROSS_ENGINE_VALIDATION.md)
- [离线 Paper 运营审计](docs/PAPER_OPERATIONS.md)

本项目不提供投资建议。接入中国证券市场自动交易前，需要向开户券商确认
程序化交易权限、报告义务、接口限制和当前监管要求。
