# LazyCat Trigger

统一触发器项目，用于触发懒猫移植项目的自动构建。

## 使用方法

手动触发工作流，填写以下参数：

| 参数 | 说明 | 示例 |
|------|------|------|
| target_repo | 目标仓库 | CodeEagle/cutia |
| target_workflow | 工作流文件名 | update-image.yml |
| force_build | 强制构建 | false |
| target_version | 指定版本 | |
| publish_to_store | 发布到商店 | false |

## 工作原理

1. 触发 lzcat-trigger 仓库的工作流
2. 使用 GitHub API 调用目标仓库的 workflow_dispatch
3. 目标仓库的 update-image.yml 会执行完整的构建流程

## 优势

- 只需要在 lzcat-trigger 项目中配置一次 secrets
- 所有移植项目共享基础设施
- 集中管理构建触发
