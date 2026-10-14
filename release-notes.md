修复引用示例误拦、失败用量丢失和健康统计口径，固定官方 **v1.7.3** 基线并保留已有安全保护。

- 补丁ID：`v1.7.3-ops.20261014.2`
- 官方提交：`c7dad8fe32316d47ce4f0ff260bb7ac7802b8a94`
- 下载：`workbuddy2api-hub-v1.7.3-wushuai0802-ops-20261014-r2.zip` 与 `SHA256SUMS.txt`
- 普通/智能/转义引号中的复读示例不误杀，未闭合保护有界，闭合后真实复读仍检测。
- 六条协议路径保留正文/推理循环、异常候选、超时、网络失败和客户端取消时已接收的provider用量；不排空上游或重复计费。
- 健康接口区分历史/进程范围与失败/取消；独立持久化观察按请求/尝试、版本/指纹/模型/协议/时间分组，合法响应恢复不称为业务完成。
- DS4.1F工具历史只产生跨回合观察信号，不默认硬拦截、共享会话缓存、加重试或改模型/提示词。SSE单行/事件默认4/8MiB预算，原入站限制保持。
- 保留混合复读、共享期限/一次安全恢复、推理别名预算、工具图片、拒答和独立401刷新。
- 保留官方分块SSE、共享TLS、额外/命名空间自定义工具、Thinking、DSML调用恢复及新的账号/统计/面板功能。
- 首帧/守卫正确衔接块读取；三个协议批量写出、阻塞读取前交付；异常流验证前不归池，已收到的用量不丢失。
- 四个替换模块、三个新增模块；安装/回滚精确固定干净官方v1.7.3，不覆盖已有旧补丁（含r1）。
- 58组定向回归在本地Windows/Python3.14.7通过；Linux3.11/3.12及Windows3.11的实际CI以Actions记录为准。

保留原配置与数据，在新目录准备准确基线、应用完整补丁并构建固定镜像；只重建唯一目标服务。公共入站默认16MiB，必要时自行显式设置50MiB。原用量JSONL保持，checkpoint缓存升级可能重建；出口退避、权重、调度与轻量模型沿用已有配置。

没有用真实模型请求验证长会话恢复率，单流保护不包含通用跨工具回合业务进展熔断。测试为合成数据/loopback；OpenSSL证书生成前提缺失时明确跳过，平台补测需如实记录。

本次发布未部署使用者的生产服务。新观察文件约30MiB轮换保留，接口最多扫描20000行；报告保留范围/截断/读写故障，不是无限期全历史统计。`WB_OPS_OBSERVE=0`关闭写入，旧Release/tag保留。

[中文说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.3-ops.20261014.2/README.md) · [安装与回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.3-ops.20261014.2/docs/INSTALL.md) · [验证记录](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.3-ops.20261014.2/docs/VERIFICATION.md) · [GitHub Actions](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/actions/workflows/tests.yml)

MIT，保留[上游](https://github.com/ardeyouxipianyi/workbuddy2api-hub)归属与附件来源说明；旧Release/tag继续保留。
