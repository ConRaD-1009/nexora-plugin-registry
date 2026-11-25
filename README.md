# nexora-plugin-registry
Nexora 官方插件目录与签名插件发布仓库，供控制台浏览、安装和更新插件。

## 插件目录

Nexora 控制台自动读取默认分支根目录下的 `index.v1.json`。当前目录收录 [Nexora FileDock](https://github.com/ConRaD-1009/nexora-plugin-registry/releases/tag/filedock-v0.1.0)，支持网页上传与文件分发。

后续发布时，先将签名的 `.nxp` 附件上传到对应的 GitHub Release，再更新索引。目录只记录版本、平台、签名者指纹和包的 SHA-256 等发布信息，不存放私钥。

[Nexora 主项目](https://github.com/ConRaD-1009/nexora)
