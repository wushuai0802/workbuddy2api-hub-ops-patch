修复此前 v1.6.20 补丁的认证超时、历史别名预算和拒答恢复边界，并扩大前置 thinking 正文的混合复读检测。仍只支持官方 v1.6.20，提交 `cb6a844588705ea01388be2b9df1f0cbbbcde357`。

- 补丁 ID：`v1.6.20-ops.20261012.1`
- 下载：`workbuddy2api-hub-v1.6.20-wushuai0802-ops-20261012-r1.zip` 与 `SHA256SUMS.txt`
- 请求触发的401刷新只有一次网络尝试，共享最多30秒的原始期限与1MiB响应上限；刷新连接独立不归池，避免旧watchdog影响下一请求。后台刷新原策略保留。
- 相同推理别名计一次，不同别名都计预算；修复空/空白主字段掩盖巨大`reasoning`的情况。用户原历史、正文及工具配对保持。
- 三协议流式/非流式保留非空`refusal`，禁止把拒答当空答重试；前置thinking正文命中混合复读时明确失败、禁止重放。
- 适配器明确支持整数`n=1`；其他候选数在请求上游前返回400，异常上游候选帧明确失败。
- 保留原有图片兼容、混合/单流守卫、最多一次安全恢复、首帧、连接池与安装事务；新增发布来源及源码指纹，补齐漏跑回归和Windows检查。

先在新目录准备准确官方源码再应用、构建；已有旧v1.6.20补丁也不能直接叠加。安装器只替换三个模块、新增两个模块，不改账号、Key、配置、用量或路由，不自动部署。旧[20261011 Release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261011.1)与更早版本继续保留。

具体测试结果见[验证记录](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261012.1/docs/VERIFICATION.md)及[GitHub Actions](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/actions/workflows/tests.yml)。回归只使用合成数据与本机HTTP；没有模型请求、NAS部署或线上长会话重放，不宣称官方完整测试套件全绿。

操作系统DNS不能由标准库强制中断，迟到返回后会阻止继续发送过期刷新。检测仍以单次上游生成为范围，没有新增通用跨工具回合业务进展熔断，不能保证所有短口头禅或任务反复都消失。

[中文说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261012.1/README.md) · [English](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261012.1/README.en.md) · [安装与回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.6.20-ops.20261012.1/docs/INSTALL.md)

基于[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，遵循MIT并保留上游归属。官方签到/调度、429治理、面板、余量估算与连接池属于上游功能。
