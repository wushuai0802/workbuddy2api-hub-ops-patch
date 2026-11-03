# WorkBuddy2API-Hub 运维补丁

[English](README.en.md) · [保护清单](docs/PROTECTIONS.md) · [安装与回滚](docs/INSTALL.md) · [验证边界](docs/VERIFICATION.md)

为[官方 WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)提供累积运维保护。当前补丁 `v1.10.7-ops.20261103.1` 仅适用于官方 v1.10.7，提交 `d177043f873fd1e7132b3219be24e4f1f7abefda`，需要 Python 3.11+。许可和来源见 [NOTICE](NOTICE.md)、[THIRD-PARTY-zh](THIRD-PARTY-zh.md)。

## 安装与回滚

从[正式 Release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.10.7-ops.20261103.1)下载 ZIP 与 SHA256SUMS.txt，或获取准确 tag；保存原镜像、源码和业务数据，在干净的固定官方源码上预检：

~~~sh
python3 -B apply.py --verify-package
python3 -B apply.py --target /absolute/path/to/clean-official-source --dry-run
~~~

按[安装指南](docs/INSTALL.md)应用完整补丁并定向部署；已有旧补丁不能直接叠加。回滚使用安装器生成的备份和原镜像，保留现用账号、配置与用量历史，不删除状态绕过预检。

## 验证边界

当前 main 清单为 250 Python（54 补丁＋196 官方）与 65 Node 套件；CI 覆盖 Linux Python 3.11/3.12 与 Windows Python 3.11。合成测试、健康与指纹不证明真实长会话完成、CodeArts 登录或云端解封。安装器不修改账号、Key、路由与调度。保护合同以 [PROTECTIONS](docs/PROTECTIONS.md) 为准，详情见 [PATCHES](docs/PATCHES.md)。

## 维护节奏与历史

安全问题或真实线上故障及时处理，其他更新每 1–2 周审查；新增保护需要真实证据或可复现反例。main 和新 ZIP 仅保留当前 patch；旧文件可用 `git show <tag>:patches/<patch-id>.patch` 取回，历史 Release/tag 保留。版本说明和验证原文见 [history](docs/history/README.md)，[CHANGELOG](CHANGELOG.md) 保留。
