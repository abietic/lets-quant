# 仓库与发布运维

GitHub 仓库是源码和签名标签的远端副本，不是行情、账户或密钥存储。CI
只运行离线 fixture；工作流没有写权限，也不读取仓库 Secret。

## 本地门禁

提交前运行：

```bash
make ci
```

它包含公开边界检查、Ruff 静态检查、源码与测试编译，以及基础环境的完整单元测试。
公开边界检查拒绝跟踪真实数据/产物目录、环境文件、私钥文件、常见凭证和本机绝对
路径。可选引擎仍可在各自隔离环境执行：

```bash
make vectorbt-test PYTHON=.venv-vectorbt/bin/python
make rqalpha-test PYTHON=.venv-rqalpha/bin/python
```

构建 wheel 和 sdist 使用：

```bash
python -m pip install -e '.[dev]'
make package
```

`make package` 使用 PyPA build 安装的 `pyproject-build` 入口，因此即使仓库中已经
存在上一次构建留下的 `build/` 目录，也不会被同名目录遮蔽；连续执行必须都能
成功。可用 `DIST_DIR=/tmp/lets-quant-dist` 将产物写到独立目录。

为当前 clone 启用仓库内的推送门禁：

```bash
make install-git-hooks
git config --local --get core.hooksPath
```

`.githooks/pre-push` 只验证本次新增的 commit 和 tag：commit 必须通过
`git verify-commit`，tag 必须是可验证的 annotated tag 且指向已签名 commit；随后
执行 `make ci`。该设置只写入当前仓库的 `.git/config`，不会修改全局 Git 配置。

## 持续集成

[`ci.yml`](../.github/workflows/ci.yml) 只在 `main` push、以 `main` 为目标的 pull
request 和人工触发时运行。`v*` 标签只触发 `release.yml`，避免合并后的分支 CI
与标签发布验证重复运行相同任务：

| job | 范围 |
|---|---|
| `Core / Python 3.9` | 最低支持版本的 lint、编译和完整基础测试 |
| `Core / Python 3.11` | 同一门禁，并运行全部离线验收 demo |
| `Core / Python 3.14` | 当前最高支持版本的同一套门禁 |
| `Package / Python 3.14` | 构建 wheel/sdist，从 wheel 安装，并在源码目录外 smoke test |

工作流权限固定为 `contents: read`，checkout 不持久化凭证，第三方 Action 固定到
官方发布 commit。

[`vectorbt.yml`](../.github/workflows/vectorbt.yml) 和
[`rqalpha.yml`](../.github/workflows/rqalpha.yml) 每周一及人工触发时，在独立环境跑
完整核心门禁、适配器测试和 demo。它们不访问券商、行情供应商或真实账户。

如果所有 matrix job 都在数秒内结束、没有执行任何 step，并显示以下注释：

> The job was not started because recent account payments have failed or your spending limit needs to be increased.

这表示 GitHub 账户的 Billing & plans 门禁阻止 runner 启动，不是源码或测试失败。
先恢复账户额度/支付状态，再手动重新运行失败的 workflow；在至少一次云端任务真正
启动并通过前，不要把这些状态检查设为 `main` 的 required checks。

## 发布

发布顺序保持显式：

1. 更新 `pyproject.toml` 和 `src/lets_quant/__init__.py` 的版本。
2. 运行核心测试、可选引擎测试、公开 demo 和 `make package` 的干净 wheel 安装
   验证。
3. 通过受保护的 `main` pull request 合并已签名 commit，等待 required check 和合并后
   的分支 CI 通过，并验证远端合并 commit 的签名。
4. 在已验证的远端 `main` commit 上创建已签名 annotated tag，推送标签，再核对远端
   tag object、peeled commit 和 `release.yml` 结果。

[`release.yml`](../.github/workflows/release.yml) 会在后续 `v*` 标签上复核标签版本，
构建 wheel/sdist，在隔离环境执行 CLI smoke test，并上传保留 30 天的构建产物和
`SHA256SUMS`。它不会发布到 PyPI、创建 GitHub Release、替代本地签名或证明构建
可复现。

Dependabot 每月检查 GitHub Actions 和 Python 依赖。升级 PR 仍必须通过 CI 并由人
工审阅，不能因来源是 Dependabot 就自动合并。

## 公开发布边界

仓库转为 public 前必须同时满足：

1. `make publication-check` 与 `make ci` 通过。
2. 使用独立 secret scanner 扫描完整 Git 历史，而不只扫描当前工作树。
3. 人工检查历史 pull request、Actions 日志和构建产物，因为可见性变更会同时公开
   这些 GitHub 数据。
4. 确认 tracked 示例均为合成数据，真实行情的再分发权已经单独评估。

公开披露不可逆：仓库以后即使改回 private，也无法收回已经产生的 clone、fork 或
外部缓存。真实行情、持仓、账户快照、券商凭证和专有策略只允许保存在 ignored
本地目录或专用私有存储中。
