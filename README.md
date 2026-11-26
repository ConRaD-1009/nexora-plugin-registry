# nexora-plugin-registry

Nexora 官方签名插件仓库。控制台读取默认分支的 [index.v1.json](index.v1.json) 安装和更新插件。

## 手工发布或补发平台

1. 在本地构建并签名 `.nxp`，保存打包工具输出的 JSON 凭据。签名私钥不进入此仓库。
2. 创建公开、可编辑的正式 GitHub Release，上传签名包和声明的图标。新 tag 使用 `<插件 ID>-v<版本>`。给已有版本补发新平台时，沿用原 tag 和图标，只上传新平台包，不覆盖旧附件。
3. 手工更新 `index.v1.json`。新版本从凭据填写版本、签名者指纹和平台的文件名、SHA-256，另填 tag 和最低 Core 版本。补发只在原版本的 `assets` 中新增目标平台。可参考 [现有索引](index.v1.json) 的格式。

上架不会触发清理。需要校验时，可以在本地使用 [Nexora 的目录校验命令](https://github.com/ConRaD-1009/nexora/blob/main/docs/plugin-marketplace.md#发布目录和制品)。

## 定时清理

**Prune plugin versions** 每周日北京时间 **03:15** 运行，按插件和完整目标平台各保留最近 5 个稳定 SemVer。在下次清理前，超过 5 版属于正常情况。

清理先提交新索引，再删除确认不再被索引引用的本仓库附件。还有其他平台在用的 Release 会保留图标。仅在不再被引用且没有其他用途的专用 Release 才会连同 tag 删除。不会删除外部仓库的附件或尚未上架的上传文件。

可以在 [Actions → Prune plugin versions](https://github.com/ConRaD-1009/nexora-plugin-registry/actions/workflows/prune-plugins.yml) 手动运行。选 `main`，保留 `apply` 未勾选即可预览。勾选 `apply` 才会实际清理。清理中断后，下次定时或手工清理会继续处理待删清单。

工作流只用本仓库的 `GITHUB_TOKEN`，不读取私有 Core。若之前保存了 `NEXORA_CORE_READ_TOKEN`，可以删掉这个 Secret。公开仓库若连续 60 天没有活动，GitHub 会自动停用定时任务，可在 Actions 重新启用。
