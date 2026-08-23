# 实验产物验证

`verify-experiment` 用于确认一个研究实验目录仍满足写出时的文件与跨文件契约。
它是只读离线检查，不读取券商账户、不联网，也不重新运行策略。

## 使用方式

```bash
PYTHONPATH=src python3 -m lets_quant verify-experiment \
  --experiment-run artifacts/experiments/<run-id>
```

也可以生成一个合成实验并立即验证：

```bash
make experiment-verify-demo
```

验证通过返回退出码 `0` 和 JSON 报告。输入损坏、结构不受支持或内容矛盾返回退出
码 `2`，错误写入 stderr，适合 CI 或本地脚本作为门禁。

## 校验范围

验证器按以下顺序失败关闭：

1. manifest 必须是 `research_experiment`，保持 `research_only=true` 和
   `investment_validity_established=false`，核心 ID 与输入哈希必须是规范 SHA-256。
2. 文件名必须是规范相对 POSIX 路径；拒绝绝对路径、`..`、反斜杠和符号链接。
3. manifest 文件清单必须排序、唯一，并与磁盘实际文件集合完全一致；缺失、额外
   或不受支持的根文件/case 文件都会失败。
4. 除 manifest 自身外，每个文件都必须匹配 `file_sha256`。
5. 根 summary 的 case 数、case ID 和每个目录必须一一对应；case 目录后缀绑定
   `case_id`，snapshot 必须绑定同一 `result_sha256`。
6. case summary、case snapshot 和 `bootstrap_uncertainty.json` 必须完全一致；
   bootstrap 启用/禁用字段、区间顺序、协议、哈希和无基准语义必须自洽。
7. summary 指标必须匹配 `metrics.json`；总成本重新由佣金、卖出税和滑点相加。
8. CSV 表头、行宽和计数必须满足契约。NAV 日期唯一递增且位于评估窗口内；会计
   日期必须等于 NAV，启用的阶段归因日期必须等于 NAV 的相邻收益日期。
9. 所有 case 的 bootstrap 和市场阶段协议/基准必须一致；test 计数与根摘要绑定，
   非 test case 不得启用 bootstrap。

新产物写入 `artifact_schema_version=1`。v0.15 已经包含完整 `file_sha256` 的目录
没有 schema 字段，验证器会将其报告为 `artifact_schema_version=0` 和
`legacy_schema_inferred=true`。更早、不含实验逐文件哈希的目录不会被降级接受。

## 报告语义

通过报告会明确给出：

```json
{
  "status": "pass",
  "file_hashes_verified": true,
  "cross_file_consistency_verified": true,
  "replay_performed": false,
  "artifact_authenticity_verified": false,
  "investment_validity_established": false,
  "automatic_execution_allowed": false
}
```

这些字段必须分开解释：

- **完整性**：当前文件与当前 manifest 相符，关键内容之间没有检测到矛盾。
- **真实性**：manifest 是否由可信主体产生。当前没有外部签名或透明日志，因此
  验证器不会声明真实性；攻击者若能同时改写全部文件和 manifest，单靠目录内部
  哈希无法识别。
- **重放性**：使用冻结输入重新执行后是否得到相同结果。当前命令不重跑，只返回
  `replay_performed=false`；带内嵌合成市场且完整 Python 版本一致的目录可继续使用
  [`replay-experiment`](EXPERIMENT_REPLAY.md)。
- **投资有效性**：完整产物不等于有效策略，验证结果不能改变
  `investment_validity_established=false`。

若需要不可抵赖的来源证明，下一层应把 `manifest_sha256` 写入受保护的签名提交、
发布证明或外部不可变存储，再由独立身份验证流程核对；不要把自签哈希误称为可信
时间戳。
