# 跨引擎验证

跨引擎验证用于发现参考回测器自己的策略、成交、费用和会计错误。VectorBT
读取冻结的 `signals.csv` 订单意图；RQAlpha 默认从策略快照和 point-in-time
价格历史独立生成信号。两个适配器都不能读取参考 `trades.csv` 来决定候选结果，
候选生成后，标准库对账器才读取两边产物并逐项比较。

当前有两条互补链路：

| 引擎 | 主要独立证据 | 仍由适配层映射 |
|---|---|---|
| VectorBT 1.1.0 | 订单记录、共享现金、持仓和组合价值 | 停牌拒绝、卖出顺序、现金缓冲、整手数量和费用拆分 |
| RQAlpha 6.3.0 | 独立固定权重/动量 PIT 决策，清洗 OHLCV、原生停牌前置拒绝、撮合、费用、账户和 NAV | 目标整手与换手率规则、合成证券代码、动态现金缓冲 |

两条链路都只验证 manifest 明确声明的范围。RQAlpha 的独立实现可以发现固定
权重和动量策略的 PIT 决策、目标整手及风险门禁漂移，但不能证明市场数据正确、
策略收益有效或适用于未来，更不能授权真实下单。VectorBT 仍是执行和组合会计
验证器，不独立验证策略。

## 环境

核心 CLI 仍只依赖 Python 3.9+ 标准库。两个适配器安装在独立环境：

```bash
python3.13 -m venv .venv-vectorbt
source .venv-vectorbt/bin/activate
python -m pip install -e '.[vectorbt]'

python3.9 -m venv .venv-rqalpha
source .venv-rqalpha/bin/activate
python -m pip install -e '.[rqalpha]'
```

项目分别锁定 `vectorbt==1.1.0` 和 `rqalpha==6.3.0`，检测到其他版本会 fail
closed。版本和 Python 范围可从各自官方仓库核对：

- [VectorBT releases](https://github.com/polakowo/vectorbt/releases)
- [VectorBT pyproject](https://github.com/polakowo/vectorbt/blob/master/pyproject.toml)
- [RQAlpha releases](https://github.com/ricequant/rqalpha/releases)
- [RQAlpha pyproject](https://github.com/ricequant/rqalpha/blob/master/pyproject.toml)

RQAlpha 官方仓库对商业使用另有授权约束；当前适配器定位为个人、非商业、离线
研究，改变用途前必须重新核对其最新许可。

## 完整运行

先生成带完整输入和输出哈希的参考回测：

```bash
PYTHONPATH=src python -m lets_quant backtest \
  --policy config/policy.example.json \
  --prices examples/prices.csv \
  --output-root artifacts/runs
```

再在对应可选环境中运行一个或两个独立引擎：

```bash
PYTHONPATH=src python -m lets_quant validate-vectorbt \
  --reference-run artifacts/runs/<run-id> \
  --prices examples/prices.csv

PYTHONPATH=src python -m lets_quant validate-rqalpha \
  --reference-run artifacts/runs/<run-id> \
  --prices examples/prices.csv
```

参考运行使用 `curated_dataset` 时，适配器会从参考 manifest 推断原目录；数据集
移动后也可显式传入 `--dataset`。此时禁止传入 `--prices`，因为裸收盘价会丢失
OHLCV、停牌、复权口径和数据 lineage：

```bash
PYTHONPATH=src python -m lets_quant validate-rqalpha \
  --reference-run artifacts/runs/<run-id> \
  --dataset data/curated/<dataset-id>
```

适配器会重新验证数据集自身全部受保护文件、参考中的 `dataset.snapshot.json`、
dataset ID、as-of、源快照 ID、质量状态和价格哈希。另一个同样合法但身份不同的
数据集也不能替换参考输入。

RQAlpha 默认使用 `--decision-mode independent_policy`。仅需隔离诊断执行链路时，
可以改用：

```bash
PYTHONPATH=src python -m lets_quant validate-rqalpha \
  --reference-run artifacts/runs/<run-id> \
  --prices examples/prices.csv \
  --decision-mode frozen_orders
```

`frozen_orders` 不写候选 `signals.csv`，也不会产生 `policy_decisions` 检查，不能
用它声称策略已被第二实现复核。

也可以使用验收入口：

```bash
make vectorbt-test vectorbt-demo
make rqalpha-test rqalpha-demo
```

价格或数据集 SHA-256 必须与参考 manifest 一致。参考 manifest 还必须包含
v0.7.0 引入的 `file_sha256` 映射。v0.10.0 的候选和报告 schema 已升级为 v4；
旧候选必须重新生成，不能降级绕过。

## RQAlpha 流动性压力

`validate-rqalpha` 可额外接收完整的 `date,symbol,volume` CSV：

```bash
PYTHONPATH=src python -m lets_quant validate-rqalpha \
  --reference-run artifacts/runs/<run-id> \
  --prices examples/prices.csv \
  --liquidity path/to/liquidity.csv \
  --volume-percent 0.25
```

文件必须覆盖参考日期与策略标的的完整笛卡尔积，且哈希会写入验证范围。流动性
压力改变成交时，跨引擎结果应为 `blocked`；但 `order_lifecycle` 仍必须通过，证明
候选内部的请求量、累计成交、撤余量、费用和最终状态自洽。由于部分成交会让
候选持仓与参考持仓分叉，后续目标整手和换手率也可能合理分叉，
`policy_decisions` 会同时阻断；首个受压前信号仍应完全一致。它是失败阶段诊断，
不是放宽对账标准。

## 候选契约

候选 schema v4 的基础产物为：

- `manifest.json`：引擎版本、参考指纹、候选哈希、验证范围和限制。
- `nav.csv`：逐日 NAV、现金和持仓。
- `trades.csv`：规范化订单结果、成交价、佣金、税费和滑点。
- `metrics.json`：共同可比的核心指标。
- `reconciliation.json`：绑定、完整性、结果差异和报告 SHA-256。

事件驱动引擎还必须写入：

- `orders.csv`：每个原生订单的请求量、成交量、均价、费用和最终状态。
- `events.csv`：带全局顺序和时区的受理、活动、成交、撤单或拒单事件；引擎在
  创建订单对象前拒绝时，使用单事件的 `order_precheck_reject`，明确由停牌触发
  时使用 `order_tradability_reject`。

声明 `validation_scope.input=independent_policy` 的候选还必须写入：

- `signals.csv`：信号日期、执行日期、状态、稳定 decision ID、策略类型、目标
  权重、PIT 证据、诊断、换手率和拟议订单。

对账器逐信号核对上述字段，并验证日期、JSON 类型、有限数值、SHA-256 decision
ID 格式和订单结构。`accepted` 必须包含订单，`no_action` 不能包含订单；因换手率
超限而 `blocked` 的信号可以保留拟议订单作为审计证据，但适配器不会提交它们。

两份生命周期文件必须同时存在并纳入 candidate ID。对账器验证：

- 事件序号连续、时间不倒退，事件不能引用未知订单，成交 ID 不能复用。
- 已创建订单从 `PENDING_NEW` 开始，经过受理后才能成交；引擎前置拒绝必须是
  单个终态事件，不能伪造订单受理过程。
- 累计成交等于逐事件成交之和，且不超过请求量。
- 非成交事件不能携带成交量、成交价或费用；满额成交、撤单和拒单状态必须与
  实际成交量一致。
- 成交均价、佣金、税费、事件数和最终状态与订单摘要一致。
- 订单摘要与规范化 `trades.csv` 一一对应。
- 最终状态之后不能再出现事件。

只修改内容并同步更新文件哈希和 candidate ID，也无法绕过这些语义检查。

## 对账规则

| 项目 | 默认规则 |
|---|---|
| 日期、标的、方向、状态、数量 | 完全一致 |
| 成交价 | 绝对误差不超过 `1e-8` |
| NAV、现金、费用和成交额 | 绝对误差不超过 `1e-6` |
| 收益、回撤和换手率 | 绝对误差不超过 `1e-10` |
| 策略状态、decision ID、证据、诊断和拟议订单 | 语义字段完全一致，换手率按 ratio 容差 |
| 生命周期顺序、数量和终态 | 规范化状态机完全一致 |

任一检查失败，报告状态为 `blocked`，命令返回退出码 `3`。输入、哈希或引擎
能力不受支持时返回退出码 `2`。

其他适配器可以复用同一对账器：

```bash
PYTHONPATH=src python -m lets_quant reconcile-engine \
  --reference-run artifacts/runs/<run-id> \
  --candidate-run artifacts/engine-validation/<candidate-id> \
  --report-out artifacts/engine-validation/reconciliation.json
```

## 当前限制

VectorBT adapter v2 与 RQAlpha adapter v3 接受 `standalone_prices_csv` 或质量
通过且身份与参考快照一致的 `curated_dataset`，仍限日线、只做多、零初始持仓，
且同标的同执行日最多一笔订单。VectorBT 的停牌拒绝属于显式 adapter lowering；
RQAlpha 使用清洗 OHLCV、实际成交量和原生交易状态检查。两者都将 `qfq/hfq`
公司行动视为已嵌入价格；`adjustment=none` 且存在公司行动时直接拒绝，因为独立
现金分红和拆并股会计尚未实现。

RQAlpha 默认通过独立模块复算当前支持的固定权重和动量策略，不导入参考
`strategies.py`、`risk.py` 或 `backtest.py`；旧的 `frozen_orders` 模式只用于执行
诊断。目标整手、换手率和现金缓冲仍是适配层规则，VectorBT 的可买数量也仍是
adapter lowering。下一阶段需要独立实现未复权公司行动、非零初始持仓和证券
主数据映射，并用真实且有使用权的数据做容量复核。
