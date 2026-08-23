# 实验目录与审核队列

`catalog-experiments` 用于回答“本地有哪些实验、哪些可验证、哪些重复、哪些需要
人工处理”。它是描述性库存与审核入口，不是策略排行榜或自动清理器。

## 使用方式

直接输出完整 JSON：

```bash
PYTHONPATH=src python3 -m lets_quant catalog-experiments \
  --experiments-root artifacts/experiments
```

将完整报告写到实验根目录之外，并在 stdout 输出摘要：

```bash
PYTHONPATH=src python3 -m lets_quant catalog-experiments \
  --experiments-root artifacts/experiments \
  --catalog-out artifacts/catalogs/catalog.json
```

输出文件使用排他创建，不覆盖已有报告，也拒绝写入被扫描根目录内。合成演示：

```bash
make experiment-catalog-demo
```

## 发现与验证

1. 根目录本身必须是真实目录，不能是符号链接。
2. 只扫描直接子目录；普通文件被忽略，子目录不会递归发现。
3. 每个候选独立执行完整实验产物验证。一个损坏目录不会阻止其他目录进入报告。
4. 验证后重新读取 manifest、实验、策略和摘要，并与刚验证的字节哈希绑定，避免
   验证与编目之间静默换文件。
5. 仅已验证目录进入 `entries`，再按 `experiment_id` 分组。组内多个
   `result_sha256` 表示同一冻结输入产生了冲突结果，必须人工调查。
6. 缺少 artifact schema 和逐文件哈希映射的早期 manifest 标为
   `unverifiable_legacy_format`。它只是格式诊断，仍未通过验证，工具不会自动迁移。
7. 完整报告使用规范 JSON 计算 `catalog_sha256`；报告验证器会重算分组、审核项、
   摘要、状态和哈希。

## 状态与退出码

| `status` | 含义 | 退出码 |
|---|---|---|
| `pass` | 所有候选均可验证；允许存在结果一致的重复实验 | `0` |
| `empty` | 根目录不存在候选子目录 | `0` |
| `attention_required` | 至少一个阻塞审核项 | `3` |

根目录、JSON、报告写出等输入错误返回 `2`。`attention_required` 不是写出失败；指定
`--catalog-out` 时，完整报告会先成功落盘，再用退出码提醒调用方需要人工处理。

## 审核项

| `code` | 严重度 | 含义 |
|---|---|---|
| `artifact_verification_failed` | error | 当前格式目录未通过完整验证 |
| `legacy_artifact_unverifiable` | error | 早期格式没有逐文件哈希，无法建立完整性边界 |
| `inconsistent_results_for_experiment_id` | error | 同一实验身份出现多个结果哈希 |
| `verified_but_nonportable_replay_input` | warning | 已验证，但没有可移植 replay input |
| `repeated_verified_experiment` | info | 多个目录身份和结果一致；仅提示可能冗余 |

## 固定边界

报告固定声明：

```json
{
  "descriptive_only": true,
  "ranking_performed": false,
  "preferred_experiment": null,
  "automatic_cleanup_performed": false,
  "artifact_authenticity_verified": false,
  "replay_performed": false,
  "investment_validity_established": false,
  "automatic_execution_allowed": false
}
```

- 结果一致只说明当前保存的冻结身份与结果哈希一致，不认证作者、来源或软件供应链。
- 目录验证不替代 [`replay-experiment`](EXPERIMENT_REPLAY.md)，目录报告也不运行策略。
- 重复实验不等于可以安全删除；外部引用、备份策略和保留政策不在当前工具范围内。
- 目录没有收益排序字段，不会根据收益、回撤或其他指标选出“最佳”实验。
