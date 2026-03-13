# LazyCat Trigger

集中检查、构建和发布所有 LazyCat 移植仓库。

## 当前模型

- `lzcat-trigger` 是唯一的定时检查入口
- 所有默认检查和构建信息来自单独的配置仓库 `lzcat-registry`
- 目标仓库不再运行自己的定时 workflow，只保留项目文件
- `_template/scripts/trigger-build.sh` 可以通过 `repository_dispatch` 主动请求构建
- `update-image.yml` 是 `lzcat-trigger` 内部复用的通用镜像更新模板

## 触发方式

### 1. 定时检查

- 每 12 小时执行一次
- 遍历配置仓库里所有 `enabled: true` 的 repo
- 检查是否有新版本可构建

### 2. 手动触发

支持以下输入：

| 参数 | 说明 |
|------|------|
| `target_repo` | 指定单个目标仓库，不填则处理所有启用仓库 |
| `target_version` | 指定要构建的上游版本 |
| `force_build` | 即使没有新版本也强制构建 |
| `publish_to_store` | 构建成功后发布到应用商店 |
| `check_only` | 只检查，不执行构建 |
| `config_repo` | 配置仓库，默认 `CodeEagle/lzcat-registry` |
| `config_ref` | 配置仓库分支，默认 `main` |
| `artifact_repo` | 集中存放 `.lpk` 和 `build-report.json` 的仓库，默认 `CodeEagle/lzcat-artifacts` |

手动参数优先级高于配置仓库中的默认值。

### 3. 仓库主动请求

`_template` 里的脚本会向本仓库发送 `repository_dispatch` 事件，请求立即处理指定仓库。

## 职责

`lzcat-trigger` 统一负责：

1. 拉取配置仓库并解析 repo 默认配置
2. 检查上游 release、tag 或 commit 是否变化
3. 通过通用 `update-image.yml` 模板构建或复用主镜像并推送到 `ghcr.io`
4. 复制主镜像和依赖镜像到 `registry.lazycat.cloud`
5. 回写目标仓库的 `lzc-manifest.yml` 和 `.lazycat-build.json`
6. 构建 `.lpk`
7. 把 `.lpk` 和 `build-report.json` 发布到统一制品仓库
8. 按需发布到应用商店

## Secrets

本仓库需要：

- `GH_TOKEN`
- `GHCR_TOKEN`
- `GHCR_USERNAME`
- `LZC_CLI_TOKEN`

## 构建总结

每次构建都会产出结构化 `build-report.json`，内容包括：

- 当前阶段和失败位置
- 解析到的 `source_version` / `build_version`
- 主镜像和 LazyCat 加速镜像地址
- 产物 release 地址
- `.lpk` 文件名和 SHA256
