# 实验差异报告

`compare-experiments` 用于解释两个已经写出的实验“输入哪里不同、哪些 case 真正
可比、结果差了多少”。它先独立验证两边产物，再生成描述性报告；不会自动选择
实验、参数或策略。

## 使用方式

直接输出完整 JSON：

```bash
PYTHONPATH=src python3 -m lets_quant compare-experiments \
  --baseline-run artifacts/experiments/<baseline-run-id> \
  --candidate-run artifacts/experiments/<candidate-run-id>
```

将完整报告写入独立目录，并在 stdout 只返回摘要：

```bash
PYTHONPATH=src python3 -m lets_quant compare-experiments \
  --baseline-run artifacts/experiments/<baseline-run-id> \
  --candidate-run artifacts/experiments/<candidate-run-id> \
  --report-out artifacts/comparisons/<comparison-id>.json
```

`--report-out` 不覆盖现有文件，也拒绝写入任一被比较的实验目录，避免破坏其严格
文件集合。合成示例可运行：

```bash
make experiment-compare-demo
```

## 比较顺序

1. 分别执行 `verify-experiment`，任一目录不完整或跨文件矛盾都立即失败。
2. 对策略快照和实验快照做规范 JSON Pointer diff，最多回显 200 条，但保留完整
   差异计数。
3. 比较规范市场哈希、原始来源类型/哈希、Python 版本、源码树哈希和 Git revision。
   旧产物没有内嵌市场时，市场身份的 `equal` 为 `null`，不会把“都未知”误报为
   相等。
4. 使用完整 case contract 对齐：窗口名称、角色、fold、起止日期、完整执行场景和
   完整参数变体必须全部相同。`case_id` 绑定整个实验输入，不用于跨实验对齐。
5. 只对已对齐 case 比较指标、市场阶段归因和 bootstrap 摘要。所有数值 delta 都
   定义为 `candidate - baseline`。
6. 对完整报告计算规范 JSON SHA-256，写入 `report_sha256`。

## 状态语义

| `comparison_status` | 含义 |
|---|---|
| `identical` | `experiment_input_id` 和 `result_sha256` 都相同 |
| `aligned_with_differences` | case contract 集合相同，但输入或结果存在差异 |
| `partially_aligned` | 只有一部分 case contract 相同 |
| `not_aligned` | 没有可安全配对的 case |

这四种状态都可以是一次成功的比较，命令返回退出码 `0`。`not_aligned` 是研究输入
不可比的结论，不是程序故障；产物损坏或报告写出失败才返回退出码 `2`。

## 报告边界

报告固定声明：

```json
{
  "descriptive_only": true,
  "ranking_performed": false,
  "preferred_experiment": null,
  "automatic_parameter_selection": false,
  "artifact_authenticity_verified": false,
  "investment_validity_established": false,
  "automatic_execution_allowed": false
}
```

- 指标差异不等于统计显著性，也不自动解释因果。
- candidate 收益更高不代表 candidate 更优；回撤、成本、样本区间和研究假设都需
  人工审阅。
- 比较读取已保存结果，不替代 [`replay-experiment`](EXPERIMENT_REPLAY.md)。源码或
  运行时不同但结果相同，只说明当前保存的身份与结果相同，不认证软件供应链。
- 目录内部哈希与比较报告哈希都不能单独证明产物由可信主体生成。
