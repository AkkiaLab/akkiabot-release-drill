# AkkiaBot 发布流程演练仓库

本仓库是 AkkiaLab/AkkiaBot 的公开副本，只用于在隔离环境中演练按版本开发、逐级上合、RC 及封版流程（AkkiaBot 仓库 `docs/release.md`，任务 BOT-38 / BOT-40）。

- 内容：AkkiaBot master `3c0ed15` 的公开代码，加上演练专用的合成提交（只改 `drill/` 下的文本文件）。不含真实数据、指标、凭据或内部验收证据。
- 本仓库**不部署任何环境**：已移除 Mini App 部署/提升、e2e 清理触发和 PR 标题检查工作流，以及 Renovate 配置；后端镜像名改为 `ghcr.io/akkialab/akkiabot-release-drill`，与生产镜像隔离。
- 这里的 tag 与 release 分支只在本仓库内有效，不会触发主仓库的任何流程；`production` 环境只用于验证审批阻断，演练中不批准生产运行。
- 演练 PR 和 issue 的标题、正文不要写真实的 `BOT-<编号>`，以免被 Multica 关联到真实任务；需要验证任务编号提取时使用不存在的编号（例如 `BOT-9001`），并在使用前确认该编号不存在。
- 演练资源保留，删除须另行确认。
