# 跨引擎验证

跨引擎验证用于发现参考回测器自己的成交、费用和会计错误。适配器读取冻结的
`signals.csv` 订单意图和原始价格，但不能读取参考 `trades.csv` 来决定成交。
候选生成后，标准库对账器才读取两边结果并逐项比较。

当前有两条互补链路：

| 引擎 | 主要独立证据 | 仍由适配层映射 |
|---|---|---|
| VectorBT 1.1.0 | 订单记录、共享现金、持仓和组合价值 | 卖出顺序、现金缓冲、可买整手数量和费用拆分 |
| RQAlpha 6.3.0 | 事件循环、撮合、资金不足部分成交、撤拒单、费用、账户和 NAV | 合成证券代码、冻结订单提交顺序、动态现金缓冲、平坦 OHLC |

两条链路都只验证 manifest 明确声明的范围。它们不重新生成策略信号，不能证明
PIT 特征、目标权重、市场数据或策略收益有效，更不能授权真实下单。

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

也可以使用验收入口：

```bash
make vectorbt-test vectorbt-demo
make rqalpha-test rqalpha-demo
```

价格文件 SHA-256 必须与参考 manifest 一致。参考 manifest 还必须包含 v0.7.0
引入的 `file_sha256` 映射；旧运行必须用当前版本重新生成，不能降级绕过。

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
候选内部的请求量、累计成交、撤余量、费用和最终状态自洽。它是失败阶段诊断，
不是放宽对账标准。

## 候选契约

候选 schema v2 的基础产物为：

- `manifest.json`：引擎版本、参考指纹、候选哈希、验证范围和限制。
- `nav.csv`：逐日 NAV、现金和持仓。
- `trades.csv`：规范化订单结果、成交价、佣金、税费和滑点。
- `metrics.json`：共同可比的核心指标。
- `reconciliation.json`：绑定、完整性、结果差异和报告 SHA-256。

事件驱动引擎还必须写入：

- `orders.csv`：每个原生订单的请求量、成交量、均价、费用和最终状态。
- `events.csv`：带全局顺序和时区的受理、活动、成交、撤单或拒单事件。

两份生命周期文件必须同时存在并纳入 candidate ID。对账器验证：

- 事件序号连续、时间不倒退，事件不能引用未知订单，成交 ID 不能复用。
- 每个订单从 `PENDING_NEW` 开始，经过受理后才能成交。
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

两个适配器 v1 都只接受 `standalone_prices_csv`、日线、只做多、零初始持仓，且
同标的同执行日最多一笔订单。它们会拒绝清洗数据集中的停牌、公司行动和复权
语义，因为这些能力尚未独立映射。

RQAlpha 已独立运行原生订单生命周期，但策略决策仍来自冻结信号；VectorBT 的
可买数量仍是适配器 lowering。完整 M2 还需要第二引擎独立生成 PIT 决策，并把
停牌、公司行动、非零初始持仓、容量与真实且有使用权的数据纳入复核。
