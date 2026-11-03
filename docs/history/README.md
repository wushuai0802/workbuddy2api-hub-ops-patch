# 历史材料索引

以下材料保留原始说明，不代表当前线上状态或 main 最新清单。

- [历版补丁范围原文](PATCHES-before-round2-20261102.md)
- [历版验证原文](VERIFICATION-versions-before-round2-20261102.md)
- [旧 Release notes 原文](release-notes-before-v1101.md)
- [v1.10.1 Release notes](release-v1101-20261102.md)、[迁移前保护清单](PROTECTIONS-v1101-20261102.md)
- [v1.10.1](V1101.md)、[v1.9.2](V192.md)、[v1.9.0](V190.md)

main 只留当前 patch，历史文件仍在 git/tag/Release 中，例如：

~~~sh
git show v1.9.2-ops.20261031.1:patches/v1.9.2-ops.20261031.1.patch > v1.9.2-ops.20261031.1.patch
~~~

完整旧文档可从精简前提交 `5befbc1` 获取，例如 `git show 5befbc1:README.md`。旧 tag、Release 和 CHANGELOG 未改写。历史计数、默认值和验证结果仅描述当时版本；当前合同以 [PROTECTIONS](../PROTECTIONS.md) 为准。
