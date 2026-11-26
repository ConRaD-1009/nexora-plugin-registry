# nexora-plugin-registry

Nexora 官方插件目录与签名插件发布仓库，供控制台浏览、安装和更新插件。控制台读取默认分支根目录的 [index.v1.json](index.v1.json)。目录不存放私钥。

## 发布或补发平台

1. 在本地构建并签名 `.nxp`。保留打包工具输出的 JSON 凭据。
2. 在本仓库创建公开、可编辑的正式 GitHub Release，上传签名包和声明的图标。新 tag 使用 `<插件 ID>-v<版本>`。已上架版本补发其他平台时沿用原 tag，只上传新平台的包，不覆盖现有附件。
3. 在 **Actions → Publish plugin → Run workflow** 上传发布信息。先选 `preview` 查看变更和校验结果，再选 `publish` 自动上架和清理。

`submission` 输入为以下结构。`packages` 是一份或多份本地打包工具输出的完整 JSON。首次上架还需提供包含 `name`、`description`、`publisher` 的 `plugin` 对象。补发平台时，最低 Core 版本、签名者、图标和 tag 必须与原版本相同。

```json
{
  "tag": "nexora.filedock-v0.1.2",
  "min_core_version": "0.1.0",
  "packages": [
    {
      "id": "nexora.filedock",
      "version": "0.1.2",
      "target": "x86_64-unknown-linux-gnu",
      "key_id": "<签名公钥指纹>",
      "name": "nexora-filedock-0.1.2-linux-x86_64.nxp",
      "archive_sha256": "<包的 SHA-256>",
      "icon": {"name": "nexora-filedock.svg", "sha256": "<图标的 SHA-256>"}
    }
  ]
}
```

工作流固定使用 [Nexora 主仓库](https://github.com/ConRaD-1009/nexora) 的一个完整提交 SHA 校验包。升级验证器时，在 workflow 中更新该 SHA。工作流必须能推送到 `main` 和管理本仓库的 Release。发布时不要并行手工修改索引、Release 或 tag。

主仓库是私有仓库。本仓库需要名为 `NEXORA_CORE_READ_TOKEN` 的 GitHub Actions 仓库 Secret。使用细粒度 GitHub token，只选择 `ConRaD-1009/nexora` 并授予 `Contents: Read`。它只用于拉取验证器源代码，不是签包私钥，也不需要写权限。

留空提交信息并选择 `preview`，可以审计当前索引的所有包和保留数量。发布工作流或脚本更新到 `main` 时，也会自动运行一次只读审计。

## 保留与清理

每个插件的每种平台各保留最近 5 个稳定 SemVer。工作流先提交索引，再自动删除淘汰的本仓库附件。仍有其他平台使用的 Release 和图标会保留。没有使用方的专用 Release 和 tag 才会一起删除。工作流不会删除外部仓库的附件，也不会清理从未上架的上传文件。

如果清理中断，在 Actions 选择 `retry-cleanup`，无需填写发布信息。清理会继续处理已提交的待删清单，并重新检查索引中的引用。
