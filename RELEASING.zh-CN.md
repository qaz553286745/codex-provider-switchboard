# 发布指南

[English](https://github.com/qaz553286745/codex-provider-switchboard/blob/main/RELEASING.md) |
[简体中文](https://github.com/qaz553286745/codex-provider-switchboard/blob/main/RELEASING.zh-CN.md)

正式发布只能来自已经审查、工作树干净的提交。不得从包含真实 Provider 凭据、
运行状态、个人路径或真实 Codex 配置的目录直接发布。

## 首次配置包索引

使用 PyPI Trusted Publishing，不在 GitHub 保存长期上传 Token。在 PyPI 与
TestPyPI 分别配置 Publisher：

| 配置项 | 值 |
| --- | --- |
| GitHub Owner | `qaz553286745` |
| Repository | `codex-provider-switchboard` |
| Workflow | `release.yml` |
| Environment | PyPI 使用 `pypi`；TestPyPI 使用 `testpypi` |

在 GitHub 创建同名 Environment，并为 `pypi` 设置 Required reviewers，使每次
正式上传都必须人工批准。手动运行工作流时默认发布到 TestPyPI；推送 `v*` Tag
时才以正式 PyPI 为目标。

编写这套流程时，包名查询结果仍是未注册状态；首次发布可以配置 Pending Trusted
Publisher。正式发布前必须重新查询，HTTP 404 不代表包名已经被预留。

## 准备版本

1. 同时更新 `pyproject.toml` 与
   `src/codex_provider_switchboard/__init__.py` 中的版本。
2. 把 `CHANGELOG.md` 的 `Unreleased` 内容移动到带日期的版本节。
3. 同步检查 `README.md`、`README.zh-CN.md` 与 `docs/` 的用户可见说明。
4. 审查 `git status --short`、完整 Diff 和最终提交。

## 在指定 Python 版本构建

构建脚本接受 Python 版本或解释器路径。默认拒绝脏工作树；随后校验版本与 Tag，
只清理仓库内部指定的产物目录，构建一个 sdist 和一个通用 wheel，执行 Twine 与
压缩包安全检查，再用同一个 Python 请求创建全新虚拟环境、安装 wheel，并验证
CLI 的版本与帮助输出。

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_repository.py
uv run pytest --cov
uv run python scripts/build_release.py --python 3.11
```

只想验证当前未提交修改时，可以显式增加 `--allow-dirty`；带这个参数生成的产物
不得上传包索引。

## TestPyPI 与正式发布

1. 先推送经过审查、尚未打 Tag 的发布提交。
2. 在 GitHub Actions 手动运行 **Release**，选择目标 Python，保持默认
   `testpypi`。
3. 在干净环境从 TestPyPI 安装，检查包说明渲染；依赖包可能仍需从正式 PyPI 获取。
4. 在同一个已审查提交上创建精确的 `v<project-version>` Tag 并推送；工作流会
   拒绝 Tag 与项目版本不一致的情况。
5. 人工批准受保护的 `pypi` Environment，再核对包页面与不可变文件哈希。

工作流只把 `dist/*` 从隔离的构建 Job 交给发布 Job。发布 Job 不检出源码，只把
`id-token` 权限设为 `write`，通过包索引认可的短期 OpenID Connect 身份上传。

## 最终验收

- sdist 同时包含中英文文档，并排除 `AGENTS.md`、凭据、缓存、运行映射与日志。
- wheel 标签为 `py3-none-any`，并声明 `Requires-Python: >=3.11`。
- 在全新环境中，`codex-provider-switchboard --version` 与 `--help` 均可运行。
- 从同一 Tag 创建 GitHub Release，并附加或引用包索引中的同一批产物。
