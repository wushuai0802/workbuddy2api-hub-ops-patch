升级官方 **v1.7.5**，修复阶段超时冲突并完善纯推理循环的安全恢复。

- 补丁：`v1.7.5-ops.20261015.1`
- 官方提交：`762982531fae2d58aacfaa78c989a873f06206df`
- 下载：`workbuddy2api-hub-v1.7.5-wushuai0802-ops-20261015-r1.zip` 与 `SHA256SUMS.txt`
- 响应头尊重面板设置，返回后开始45秒首帧预算；初始请求、恢复和网页工具后续生成共享原总期限，默认300秒。
- 唯一一次安全纯推理循环恢复默认加入简短接续提示，只改出站副本；正文、拒答或工具后禁止重放。`WB_OPS_LOOP_RECOVERY_HINT=0`关闭提示。
- 连续精确失败前缀最多清理16段/65536字符，最长匹配优先，保留未知后缀与客户端原历史。
- 持久化匿名规则、窗口计数和恢复停止原因，不存原推理/提示词/工具参数/凭据。检测阈值保持。
- 参考用户提供的v1.7.5-r2，细化引用片段保护以避免短工具名旁的复读漏检，并校验/复制累计用量，支持无total字段。
- 保留混合守卫、引用反例、六路径用量、401期限、拒答、历史预算、工具图片、SSE限制及连接租约保护。
- 保留官方默认关闭的SQLite日志、checkpoint修复、新主题/统计面板、每日/手动模型目录刷新、来源归档和实际倍率。模型列表/倍率显示遵循新目录行为。
- 四个替换模块、三个新增模块；新增官方`wb_logstore.py`依赖需随完整基线构建。仅支持干净固定v1.7.5，旧补丁树不能直接覆盖。
- 68组Python定向回归与39组官方Node面板测试；Linux3.11/3.12及Windows3.11最终结果以本版Actions为准。

先保留旧镜像、源码、配置和数据，再在新目录应用完整补丁，定向重建唯一目标服务。公共请求体默认16MiB；较大需求需显式配置。Docker应持久化`/app/catalog`，或设置`WB_PROXY_CATALOG_DIR=/app/usage/catalog`复用usage挂载。SQLite日志沿用用户选择，不自动开启。checkpoint可能重建，原JSONL保持。

没有真实模型请求验证恢复率，无法保证模型不再复读；第二次循环仍会结束。通用跨工具回合业务进展熔断未实现，相关信号保持只观察。原Release/tag继续保留。

[中文说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.5-ops.20261015.1/README.md) · [安装与回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.5-ops.20261015.1/docs/INSTALL.md) · [验证记录](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.5-ops.20261015.1/docs/VERIFICATION.md) · [GitHub Actions](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/actions/workflows/tests.yml)

MIT，保留官方归属与附件来源说明。
