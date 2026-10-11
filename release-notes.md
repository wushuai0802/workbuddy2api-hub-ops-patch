DeepSeek v4.1 Flash 在工作中可能把 “Let me go.” / “Hmm.” 大量夹在其他推理里，导致原有检测漏检。本包增加有界的混合复读保护，并整理已有的循环、历史、工具图片、401 与流式协议补丁，方便相同问题的使用者检查和安装。

这是非官方源码补丁包，**只支持官方 v1.6.19，commit `55dbb9ebcac3d55b26442d9bbc8b2e11ece637da`**。已有不明补丁或其他版本会被拒绝，建议另建准确的干净官方源码目录，并保留原镜像与数据。

- 补丁 ID：`v1.6.19-ops.20261011.2`
- 下载：`workbuddy2api-hub-v1.6.19-ops-20261011-r2.zip` 与 `SHA256SUMS.txt`
- 只替换 `wb_proxy.py` / `wb_webtools.py` / `wb_accounts.py`，新增 `wb_ops.py` / `wb_stall.py`；安装器不改账号数据、Key、配置、用量或路由。
- 补齐 401 刷新所需的账号辅助方法及网页 deflate 解压、搜索 RSS 解析的标准库依赖。
- 安装前先核对包与基线，备份自己的部署和数据，暂停目标服务；Docker 用户从准确的已补丁源码构建，并只重建自己的 Hub Compose 服务。

[中文说明与快速入口](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.19-ops.20261011.2/README.md) · [English](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.19-ops.20261011.2/README.en.md) · [安装和回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.19-ops.20261011.2/docs/INSTALL.md)

匿名离线验收在 132 个真实推理块中命中 3 个异常块，包括两个最长的反复块；10 类反例、100 次独立短工具回合和 12 组 focused 回归通过。公开包不含真实历史、账号或凭据。

最终 r2 包的 21 组 focused 离线回归通过，包含 401 刷新 14 项与解压/搜索和流协议集成 56 项。安装器在 Windows 下有 4 项真实符号链接检查因权限跳过；Linux/Python 3.11、3.12 的结果见 [GitHub Actions](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/actions/workflows/tests.yml)。

本次没有付费模型请求或在线长会话重放，不宣称官方完整测试套件全绿，不保证所有 “Let me go.”、模型循环或工具停滞都消失。保护针对一次上游生成，正文/工具出现后禁止自动重放。[完整补丁范围](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.19-ops.20261011.2/docs/PATCHES.md) · [验证边界](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.19-ops.20261011.2/docs/VERIFICATION.md)

基于 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，遵循 MIT 并保留上游归属。账号签到、缓存亲和、429 切换、空 function 占位等原生能力不作为本地补丁的原创功能。
