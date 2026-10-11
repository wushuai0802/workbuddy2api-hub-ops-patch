# WorkBuddy2API-Hub 运维补丁

[English](README.en.md) · [下载安装](docs/INSTALL.md) · [补丁说明](docs/PATCHES.md) · [验证记录](docs/VERIFICATION.md)

给使用 [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) 时遇到 DeepSeek 推理复读、工具图片报错、认证过期或流式响应异常的人提供一个可检查、可回退的源码补丁包。

例如，DeepSeek v4.1 Flash 可能在其他推理文字中大量重复 “Let me go.” 和 “Hmm.”，原有单流检测未必命中。本补丁增加有限窗口的混合复读检测，并保留已有的安全恢复和协议保护。它拦截高密度异常，不会把正常工作中偶尔一句 “Let me go.” 当作故障。

## 下载与适用版本

当前发布：**v1.6.19-ops.20261011.2**（2026-10-11）。

- [下载补丁 ZIP 与 SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.19-ops.20261011.2)
- ZIP 文件名：`workbuddy2api-hub-v1.6.19-ops-20261011-r2.zip`
- 仅支持官方 **v1.6.19**，提交 **`55dbb9ebcac3d55b26442d9bbc8b2e11ece637da`**。
- 需要 Python **3.11+**；Docker 安装需要从打过补丁的准确官方源码重新构建镜像。

这是**非官方补丁包**，不是完整 Hub、账号包、现成 Docker 镜像或适用于任意版本的通用覆盖包。安装器检查源码基线与补丁校验和；已有其他补丁、混合源码或不匹配的版本会被拒绝。

## 有哪些补丁

| 功能组 | 解决的问题 |
| --- | --- |
| 防循环与生成期限 | 检测单次生成中的推理/正文复读，默认共用 300 秒生成期限。 |
| 混合复读保护 | 为 DeepSeek v4.1 Flash 检测夹在其他文字里的高密度行动短句与重复 Hmm。 |
| 最多一次安全恢复 | 空回答或符合条件的纯推理循环最多恢复一次；正文或工具出现后禁止自动重放。 |
| 推理历史保护 | 出站旧推理默认 65,536 字符预算；精确隔离失败前缀，清理已确认窗口中的重复 Hmm。 |
| 工具图片兼容 | 将发给 DeepSeek v4.1 Flash 的工具结果图片转成说明文本，处理相应的 `11133` 报错。 |
| 401 续期 | 对上游 HTTP 401 做受限的凭据刷新与重试。 |
| 回答与流协议修复 | 有可信边界的答案恢复、DSML 清理、首帧检查、明确截断错误及有界推理缓冲。 |
| 提示词、网页与诊断保护 | 默认不因 403 改写 passthrough 指令；限制网页解压大小，提供可配置搜索兜底和有界诊断。 |

详见 [PATCHES.md](docs/PATCHES.md)，包含触发范围、默认开关与上游功能归属。

## 快速开始：先检查，再安装

在你自己的工作目录中获取固定版本的两个仓库：

~~~sh
git clone --depth 1 --branch v1.6.19-ops.20261011.2 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-20261011
git clone --depth 1 --branch v1.6.19 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.6.19
git -C workbuddy2api-hub-v1.6.19 rev-parse HEAD
python3 -B ops-patch-20261011/apply.py --verify-package
python3 -B ops-patch-20261011/apply.py --target ./workbuddy2api-hub-v1.6.19 --dry-run
~~~

提交必须与上面的完整 SHA 一致。`--dry-run` 通过只表示文件可以应用；**还没有修改源码或部署**。下一步按照 [安装与回滚指南](docs/INSTALL.md) 保存自己的配置、数据和旧镜像，暂停目标服务，再应用补丁和定向重建。

安装只替换 `wb_proxy.py`、`wb_webtools.py`、`wb_accounts.py`，新增 `wb_ops.py`、`wb_stall.py`。安装器不改账号数据、设置、用量、Key、端口或路由，不自动启动、更新或重建服务。

不要在运行中的容器里临时复制单个文件；容器重建可能丢掉这种修改。也不要删除安装状态或备份来绕过拒绝检查。已有未知补丁时，另建准确的干净官方源码目录，保留原部署后再准备新镜像。

## 效果与边界

混合复读规则的离线验收覆盖匿名汇总的 132 个真实推理块，命中 3 个异常块，包括两个最长的反复块；10 类反例、100 次独立短工具回合和 12 组 focused 回归已通过。公开包不含真实会话原文、账号或凭据。详见 [验证范围](docs/VERIFICATION.md)。

本补丁不能保证模型不再循环、所有 “Let me go.” 消失或所有任务都取得业务进展。检测针对一次上游生成；正常结束后客户端继续发起的多个工具回合，不会因为使用同一个工具而被一概熔断。出站历史处理不修改客户端会话数据库，也不能解决账号余额耗尽。

本发布没有进行付费模型请求或在线长会话重放，也不宣称官方完整测试套件全部通过。健康检查和离线通过不等于你的部署已完成实际对话验收。

## 上游与许可证

原项目：[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，MIT 许可证。本仓库遵循 [MIT](LICENSE)，保留上游许可与归属。

账号签到、缓存/账号亲和、429 账号治理与切换、空 function 占位处理等已有上游功能，不作为本补丁的原创能力。升级官方版本后，需要重新核对基线；直接换回官方镜像不会保留本补丁。

提交问题时请附补丁 ID、官方版本、协议、错误码及脱敏后的触发步骤。不要上传账号文件、Key、完整配置、真实会话历史或含凭据的镜像。
