升级到官方 **v1.7.3**，保留并重基现有运维补丁，同时吸收用户提供的v1.7.2补丁中的DSML、原生Thinking和认证迁移边界。

- 补丁ID：`v1.7.3-ops.20261014.1`
- 官方提交：`c7dad8fe32316d47ce4f0ff260bb7ac7802b8a94`
- 下载：`workbuddy2api-hub-v1.7.3-wushuai0802-ops-20261014-r1.zip` 与 `SHA256SUMS.txt`
- 保留混合复读、共享期限/一次安全恢复、推理别名预算、工具图片、拒答和独立401刷新。
- 保留官方分块SSE、共享TLS、额外/命名空间自定义工具、Thinking、DSML调用恢复及新的账号/统计/面板功能。
- 首帧/守卫正确衔接块读取；三个协议批量写出、阻塞读取前交付；异常流验证前不归池，已收到的用量不丢失。
- 四个替换模块、两个新增模块；安装/回滚精确固定官方v1.7.3，不覆盖已有v1.6.20/v1.7.2补丁。
- 56组定向回归在本地Windows/Python3.14.7通过；Linux3.11/3.12及Windows3.11的实际CI以Actions记录为准。

保留原配置与数据，在新目录准备准确基线、应用完整补丁并构建固定镜像；只重建唯一目标服务。公共入站默认16MiB，必要时自行显式设置50MiB。原用量JSONL保持，checkpoint缓存升级可能重建；出口退避、权重、调度与轻量模型沿用已有配置。

没有用真实模型请求验证长会话恢复率，单流保护不包含通用跨工具回合业务进展熔断。测试为合成数据/loopback；OpenSSL证书生成前提缺失时明确跳过，平台补测需如实记录。

[中文说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.3-ops.20261014.1/README.md) · [安装与回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.3-ops.20261014.1/docs/INSTALL.md) · [验证记录](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.3-ops.20261014.1/docs/VERIFICATION.md) · [GitHub Actions](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/actions/workflows/tests.yml)

MIT，保留[上游](https://github.com/ardeyouxipianyi/workbuddy2api-hub)归属与附件来源说明；旧Release/tag继续保留。
