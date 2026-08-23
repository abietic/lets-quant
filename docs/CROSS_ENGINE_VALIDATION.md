# 跨引擎验证

跨引擎验证的目标是发现参考回测器自己的成交和会计错误，而不是用第二个库重新
包装同一份 `trades.csv`。当前 VectorBT 适配器读取参考运行中冻结的
`signals.csv` 订单意图和原始价格。适配层先把卖出优先、动态现金缓冲、整手和
费用规则映射成 VectorBT 订单；VectorBT 再独立生成订单记录、共享现金、持仓和
NAV，最后与参考结果对账。

这条链路验证的是“相同决策和明确的适配规则交给另一套组合会计引擎后，结果是否
一致”。它不会重新生成策略信号；动态现金缓冲和可买整手数量也不是 VectorBT
原生规则，而是适配器 lowering。因而它不能独立证明仓位缩减公式、PIT 特征、
目标权重或策略逻辑，更不能证明策略在真实市场有效。

## 环境边界

核心 CLI 继续只依赖 Python 3.9+ 标准库。VectorBT 1.1.0 当前声明支持
Python 3.11 至 3.14，因此适配器必须安装在独立的 Python 3.11+ 环境：

```bash
python3.13 -m venv .venv-vectorbt
source .venv-vectorbt/bin/activate
python -m pip install -e '.[vectorbt]'
```

项目锁定 `vectorbt==1.1.0`，未验证的版本会 fail closed。版本和 Python 要求可
从 [VectorBT releases](https://github.com/polakowo/vectorbt/releases) 与
[VectorBT pyproject](https://github.com/polakowo/vectorbt/blob/master/pyproject.toml)
核对。

## 完整运行

先生成参考回测：

```bash
PYTHONPATH=src python -m lets_quant backtest \
  --policy config/policy.example.json \
  --prices examples/prices.csv \
  --output-root artifacts/runs
```

再将输出目录传给独立引擎：

```bash
PYTHONPATH=src python -m lets_quant validate-vectorbt \
  --reference-run artifacts/runs/<run-id> \
  --prices examples/prices.csv
```

也可以在已安装可选依赖的环境中直接运行：

```bash
make vectorbt-test
make vectorbt-demo
```

价格文件 SHA-256 必须与参考 manifest 一致。省略 `--prices` 时会使用参考
manifest 记录的绝对路径；为了跨机器重放，建议显式传入价格文件。

参考 manifest 还必须包含 v0.7.0 引入的 `file_sha256` 映射；每个策略快照、信号、
成交、NAV、指标和账本文件都会在候选生成前重新验哈希。v0.6.0 及更早的运行没有
这组完整性锚点，必须用当前版本重新回测，不能降级绕过。

## 候选产物

`validate-vectorbt` 在 `artifacts/engine-validation/<run-id>/` 写入：

- `manifest.json`：引擎和适配器版本、参考输入指纹、候选文件哈希、验证范围和
  `engine_native_components`、`adapter_mapped_components` 及明确排除项。
- `nav.csv`：独立引擎生成的逐日 NAV、现金和持仓。
- `trades.csv`：独立引擎生成的成交量、成交价、佣金、税费和滑点。
- `metrics.json`：两套引擎共同可比的核心汇总指标。
- `reconciliation.json`：输入绑定、文件完整性、日期轴、NAV、现金、持仓、
  成交和指标检查，以及报告自身 SHA-256。

候选 manifest 同时绑定参考运行的 manifest、策略快照、信号、成交、NAV 和指标
哈希。参考输入或候选文件发生变化后，旧候选不能继续冒充同一次验证。

对账默认要求：

| 项目 | 默认规则 |
|---|---|
| 日期、标的、方向、状态、数量 | 完全一致 |
| 成交价 | 绝对误差不超过 `1e-8` |
| NAV、现金、费用和成交额 | 绝对误差不超过 `1e-6` |
| 收益、回撤和换手率 | 绝对误差不超过 `1e-10` |

任一检查失败，报告状态为 `blocked`，命令返回退出码 `3`。输入格式、哈希或引擎
能力不受支持时返回退出码 `2`。

## 接入其他引擎

其他适配器只需生成同一候选契约，就能复用标准库对账器：

```bash
PYTHONPATH=src python -m lets_quant reconcile-engine \
  --reference-run artifacts/runs/<run-id> \
  --candidate-run artifacts/engine-validation/<candidate-id> \
  --report-out artifacts/engine-validation/reconciliation.json
```

`cross_engine.write_engine_candidate` 负责规范化 CSV、绑定参考输入和生成候选 ID。
适配器不应读取参考 `trades.csv` 来决定自己的成交；该文件只能由对账器在候选
生成后读取。

## 当前限制

VectorBT 适配器 v1 只接受：

- `standalone_prices_csv` 日线收盘价运行。
- 多标的共享现金、只做多、初始持仓为零。
- 同标的同执行日最多一笔订单。
- 参考引擎已有的整手、动态现金缓冲和固定费用模型。

它会拒绝清洗数据集中的停牌、显式公司行动和复权语义，因为这些能力还没有被
独立映射。它也不模拟盘口、涨跌停、流动性、排队或盘中路径。

示例 fixture 的零差异只证明这组冻结输入下的适配产物与独立组合会计一致。要
满足完整 M2，还需要让第二个事件驱动引擎用原生订单生命周期独立运行仓位约束和
策略决策，并在真实、具备使用权的 PIT 数据上比较成本后结论和失败阶段。
