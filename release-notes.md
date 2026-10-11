迁移至官方 **v1.6.20**，保留 DeepSeek v4.1 Flash 的推理/正文复读、混合 “Let me go.” / “Hmm.”、安全恢复、历史保护、工具图片、401 续期、首帧和流式协议等运维补丁，并保留新上游的连接复用和面板功能。

这是非官方源码补丁包，**只支持官方 v1.6.20，commit `cb6a844588705ea01388be2b9df1f0cbbbcde357`**。从 v1.6.19 补丁升级时，请在新目录准备准确的官方 v1.6.20 后应用本包，保留旧镜像、配置与数据。仍使用 v1.6.19 的用户请下载[历史 r2 包](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.19-ops.20261011.2)，两个包不能交叉安装。

- 补丁 ID：`v1.6.20-ops.20261011.1`
- 下载：`workbuddy2api-hub-v1.6.20-ops-20261011-r1.zip` 与 `SHA256SUMS.txt`
- 只替换 `wb_proxy.py` / `wb_webtools.py` / `wb_accounts.py`，新增 `wb_ops.py` / `wb_stall.py`；安装器不改账号数据、Key、配置、用量或路由，不自动部署。
- `wb_upstream_pool.py` 加入精确基线依赖核对，新增连接池与首帧/生成期限/关闭行为的兼容检查。
- 保留官方 v1.6.20 的面板排序、今日活动与签到记录、更新检查、剩余用量估算、限额持久化和性能改进。面板 401 轮询修复不能代替模型接口的凭据续期。

升级注意：公共补丁保留官方 **16 MiB** 请求体默认上限；大 JSON 历史需要使用者显式设置 `WB_MAX_PAYLOAD_BYTES=52428800`（50 MiB），并考虑并发内存占用。国内连登管家的补签/抽奖会使用对应卡券或次数；余量优先调度默认关；Docker/OpenWrt 服务端的客户端一键配置不可用。

安装前先核对包与基线，备份自己的部署和数据，暂停目标服务。Docker 用户从准确的已补丁源码构建，并只重建自己的 Hub Compose 服务。

[中文说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261011.1/README.md) · [English](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261011.1/README.en.md) · [安装和回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261011.1/docs/INSTALL.md) · [完整补丁范围](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261011.1/docs/PATCHES.md) · [本次验证与边界](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261011.1/docs/VERIFICATION.md)

此前 v1.6.19 混合复读组件的匿名离线验收覆盖 132 个真实推理块，命中 3 个异常块，包括两个最长的反复块；历史验收不能代替 v1.6.20 迁移检查。本次 Windows/Python 3.14 与无网络 Linux/Python 3.11 镜像均通过 26 组 focused 回归，包括真实连接池兼容、401、协议和上游新增热路径。候选隔离启动及实际部署的健康、鉴权、模块和看板字节核对通过。具体结果见验证记录与 [GitHub Actions](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/actions/workflows/tests.yml)。公开包不含真实历史、账号或凭据。

本次没有付费模型请求或在线长会话重放，不宣称官方完整测试套件全绿，不保证所有 “Let me go.”、模型循环或工具停滞都消失。保护针对一次上游生成，正文/工具出现后禁止自动重放。

基于 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，遵循 MIT 并保留上游归属。账号签到、缓存亲和、429 切换、空 function 占位和 v1.6.20 原生更新不作为本地补丁的原创功能。
