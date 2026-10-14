# 验证与边界

当前 `v1.7.3-ops.20261014.1`，固定官方提交 `c7dad8fe32316d47ce4f0ff260bb7ac7802b8a94`。

2026-10-14，Windows/Python3.14.7下56组focused回归通过：

- 保留此前循环/混合、历史/图片、401、首帧、缓冲、拒答、六路径和安装器回归。
- 新的分块守卫5项：前缀/剩余块准确、首块在下一读取前交付、客户端断开不消费下一块、循环块已接收用量保持、写出队列有界。
- 附件迁移12项和v1.7.2边界13项：DSML完整/截断/原生优先、Thinking、候选、拒答及认证。
- 官方分块、共享TLS、命名空间/自定义工具、stop归一化、失败去重/出口退避、权重/备注、活跃任务、闭/冷窗口、价表指纹、bytes预过滤和最近索引合同。

第一次移植回归暴露首帧/守卫辅助函数接线、连接提前归池、保留Responses内容索引/content_filter语义及旧测试接口变化，均在部署前处理。Messages原生thinking的存在由新协议合同保留，未为了旧测试移除。没有移除有效失败断言。

CI矩阵为Linux/Python3.11、3.12和Windows/Python3.11，使用同一runner；[实际状态](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/actions/workflows/tests.yml)以本版提交对应run为准。

~~~sh
python3 -B apply.py --verify-package
python3 -B scripts/test_offline.py --upstream /absolute/path/to/clean-official-v1.7.3 --output .work/verification
~~~

runner验证基线，在临时副本安装，使用空账号/用量、合成数据及本机HTTP，Python子进程阻止非loopback联网。退出码、summary.json与每组日志是该次证据；未运行完整上游145套件，不声称全部官方测试全绿。

新版权限/链接/回滚检查包括实际Windows父目录junction；受系统权限限制的symlink测试可能跳过。两项真实TLS测试用临时合成证书，缺OpenSSL命令行时明确跳过，不等同TLS功能不可用。个别平台可用外部生成证书单独补跑原断言，需记录这一区别。

安装/回滚只恢复当前固定官方源码，保留原字节/CRLF及合成业务数据。发布ZIP来自manifest白名单，需与Git最终提交和公开下载SHA对照，不包含真实会话、账号或生产配置。

[历史v1.6.20版](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/tree/v1.6.20-ops.20261012.1/docs/VERIFICATION.md)的38组及部署结果仍在其tag，不能替代当前验收；此前真实132块混合检测的匿名结果亦仅为历史组件评价。

公开测试与健康/模块指纹不测量实际长会话恢复率，不保证所有模型或跨工具回合停滞消失。部署后须核对当前进程版本/指纹、鉴权、账号、配置、历史及正常业务；真实模型请求可能消耗自身额度。
