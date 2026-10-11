# WorkBuddy2API-Hub 运维补丁

[English](README.en.md) · [下载安装](docs/INSTALL.md) · [补丁说明](docs/PATCHES.md) · [验证记录](docs/VERIFICATION.md)

给使用 [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) 时遇到 DeepSeek 推理复读、工具图片报错、认证过期或流式响应异常的人提供一个可检查、可回退的源码补丁包。

例如，DeepSeek v4.1 Flash 可能在其他推理文字中大量重复 “Let me go.” 和 “Hmm.”，原有单流检测未必命中。本补丁增加有限窗口的混合复读检测，并保留已有的安全恢复和协议保护。它拦截高密度异常，不会把正常工作中偶尔一句 “Let me go.” 当作故障。

## 下载与适用版本

当前发布：**v1.6.20-ops.20261012.1**（2026-10-12）。

- [下载补丁 ZIP 与 SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261012.1)
- ZIP 文件名：`workbuddy2api-hub-v1.6.20-wushuai0802-ops-20261012-r1.zip`
- 仅支持官方 **v1.6.20**，提交 **`cb6a844588705ea01388be2b9df1f0cbbbcde357`**。
- 需要 Python **3.11+**；Docker 安装需要从打过补丁的准确官方源码重新构建镜像。

这是**非官方补丁包**，不是完整 Hub、账号包、现成 Docker 镜像或适用于任意版本的通用覆盖包。安装器检查源码基线与补丁校验和；已有其他补丁、混合源码或不匹配的版本会被拒绝。

本次修复 401 刷新的共享期限、推理字段别名绕过预算、拒答误触发空答重试，并将混合复读检测扩展到确认的前置 thinking 正文。适配器明确支持单候选 `n=1`；请求更多候选会在上游调用前返回错误。健康诊断新增发布来源与运行源码指纹，避免与其他同名补丁混淆。

从此前 v1.6.20 补丁升级，同样使用新目录中的准确官方源码重新应用、构建；保留[旧 Release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261011.1)及旧部署，不在旧补丁上强行叠加。

仍使用官方 v1.6.19 的用户请使用[对应的历史 r2 Release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.19-ops.20261011.2)及其 tag 内的说明。两个补丁包的基线不同，不能交叉安装。

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
git clone --depth 1 --branch v1.6.20-ops.20261012.1 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v1620
git clone --depth 1 --branch v1.6.20 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.6.20
git -C workbuddy2api-hub-v1.6.20 rev-parse HEAD
python3 -B ops-patch-v1620/apply.py --verify-package
python3 -B ops-patch-v1620/apply.py --target ./workbuddy2api-hub-v1.6.20 --dry-run
~~~

提交必须与上面的完整 SHA 一致。`--dry-run` 通过只表示文件可以应用；**还没有修改源码或部署**。下一步按照 [安装与回滚指南](docs/INSTALL.md) 保存自己的配置、数据和旧镜像，暂停目标服务，再应用补丁和定向重建。

安装只替换 `wb_proxy.py`、`wb_webtools.py`、`wb_accounts.py`，新增 `wb_ops.py`、`wb_stall.py`。安装器不改账号数据、设置、用量、Key、端口或路由，不自动启动、更新或重建服务。

不要在运行中的容器里临时复制单个文件；容器重建可能丢掉这种修改。也不要删除安装状态或备份来绕过拒绝检查。已有未知补丁时，另建准确的干净官方源码目录，保留原部署后再准备新镜像。

## 官方 v1.6.20 的变化与升级注意

本次迁移保留官方 v1.6.20 的面板排序、今日活动与签到记录、更新检查、剩余用量估算和限额持久化，以及连接复用、轮询和统计性能改进。官方的面板 401 轮询修复与本补丁的模型接口 401 凭据续期解决不同问题；推理复读、空答安全恢复、工具图片和推理历史等核心保护仍需本补丁。完整原生更新见[上游 v1.6.20 记录](https://github.com/ardeyouxipianyi/workbuddy2api-hub/blob/v1.6.20/docs/CHANGELOG.md)。

- 上游请求体上限从 50 MiB 收紧到 **16 MiB**，本补丁保留该默认值。需要发送更大 JSON 历史时，可自行显式设置 `WB_MAX_PAYLOAD_BYTES=52428800` 恢复 50 MiB，并评估并发请求的内存占用。
- 国内版连登管家可自动补签、兑换和抽奖，补签或抽奖会消耗相应卡券/次数；沿用已有调度配置，升级前核对自己启用的任务。
- 「剩余用量优先调度」默认关闭；开启会让接近用完的账号多接请求，可能更早撞限。
- 客户端「一键配置」在 Docker、OpenWrt 等服务端部署中不可用；它用于网关本机的客户端配置。
- 从 v1.6.19 补丁升级时，在新目录准备准确的官方 v1.6.20 后应用本包；保留旧镜像、配置和数据，不叠加覆盖旧源码。

## 效果与边界

混合复读规则在此前 v1.6.19 组件验收中覆盖匿名汇总的 132 个真实推理块，命中 3 个异常块，包括两个最长的反复块；10 类反例、100 次独立短工具回合和 12 组 focused 回归通过。这些历史结果不能代替 v1.6.20 迁移验收；本次结果与连接池兼容范围见 [验证记录](docs/VERIFICATION.md)。公开包不含真实会话原文、账号或凭据。

本补丁不能保证模型不再循环、所有 “Let me go.” 消失或所有任务都取得业务进展。检测针对一次上游生成；正常结束后客户端继续发起的多个工具回合，不会因为使用同一个工具而被一概熔断。出站历史处理不修改客户端会话数据库，也不能解决账号余额耗尽。

本发布没有进行付费模型请求或在线长会话重放，也不宣称官方完整测试套件全部通过。健康检查和离线通过不等于你的部署已完成实际对话验收。

## 上游与许可证

原项目：[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，MIT 许可证。本仓库遵循 [MIT](LICENSE)，保留上游许可与归属。

账号签到、缓存/账号亲和、429 账号治理与切换、空 function 占位处理等已有上游功能，不作为本补丁的原创能力。升级官方版本后，需要重新核对基线；直接换回官方镜像不会保留本补丁。

提交问题时请附补丁 ID、官方版本、协议、错误码及脱敏后的触发步骤。不要上传账号文件、Key、完整配置、真实会话历史或含凭据的镜像。
