# 仓库与发布运维

私有 GitHub 仓库是源码和签名标签的远端副本，不是行情、账户或密钥存储。CI
只运行离线 fixture；工作流没有写权限，也不读取仓库 Secret。

## 本地门禁

提交前运行：

```bash
make ci
```

它包含 Ruff 静态检查、源码与测试编译，以及基础环境的完整单元测试。可选引擎
仍可在各自隔离环境执行：

```bash
make vectorbt-test PYTHON=.venv-vectorbt/bin/python
make rqalpha-test PYTHON=.venv-rqalpha/bin/python
```

## 持续集成

[`ci.yml`](../.github/workflows/ci.yml) 在 `main` push、pull request 和人工触发时运行：

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

## 发布

发布顺序保持显式：

1. 更新 `pyproject.toml` 和 `src/lets_quant/__init__.py` 的版本。
2. 运行核心测试、可选引擎测试、公开 demo 和干净 wheel 安装验证。
3. 创建已签名 commit 和已签名 annotated tag。
4. 原子推送 `main` 与标签，再核对远端分支、tag object 和 peeled commit。

[`release.yml`](../.github/workflows/release.yml) 会在后续 `v*` 标签上复核标签版本，
构建 wheel/sdist，在隔离环境执行 CLI smoke test，并上传保留 30 天的构建产物和
`SHA256SUMS`。它不会发布到 PyPI、创建 GitHub Release、替代本地签名或证明构建
可复现。

Dependabot 每月检查 GitHub Actions 和 Python 依赖。升级 PR 仍必须通过 CI 并由人
工审阅，不能因来源是 Dependabot 就自动合并。
