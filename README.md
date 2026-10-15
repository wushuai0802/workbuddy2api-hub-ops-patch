# WorkBuddy2API-Hub 运维补丁

[English](README.en.md) · [安装与回滚](docs/INSTALL.md) · [补丁范围](docs/PATCHES.md) · [验证记录](docs/VERIFICATION.md)

为 WorkBuddy 网关提供 DeepSeek 复读保护、受限认证续期、推理历史与工具图片兼容，以及流式协议边界修复。基于[官方项目](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，可检查、可回退，遵循 MIT。

当前版本：**v1.7.6-ops.20261015.1**。

- [下载 ZIP 与 SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.7.6-ops.20261015.1)
- 文件名：`workbuddy2api-hub-v1.7.6-wushuai0802-ops-20261015-r1.zip`
- 仅支持官方 **v1.7.6**，提交 `7dbe11564c0c74a1d2cdb05791b313e339dddbf5`。
- Python 3.11+。Docker 用户从打过完整补丁的准确源码重新构建镜像。
- 安装器只替换四个上游模块（proxy、accounts、webtools、upstream_pool），新增 ops、stall、observe 三个模块；不操作账号、Key、配置、历史或部署。

本版把已有补丁迁移到官方 v1.7.6，保留混合复读、推理别名预算、工具图片、拒答、用量结算和连接所有权保护。来源与许可见 [NOTICE.md](NOTICE.md) 和 [THIRD-PARTY-zh.md](THIRD-PARTY-zh.md)。

本次修复响应头预算被固定45秒覆盖的问题：响应头尊重面板设置，返回后才开始45秒首帧预算；初始连接、恢复和网页工具后续生成共享原300秒总期限。纯推理循环的唯一一次安全恢复默认加入简短接续提示，可用 `WB_OPS_LOOP_RECOVERY_HINT=0` 关闭提示。连续已知失败前缀最多清理16段/65536字符，保留未知后缀和客户端原历史。持久观察新增匿名规则、窗口计数和恢复停止原因；检测阈值保持。

参考新提供的v1.7.5-r2包，将引号保护细化到引用片段，短工具名不再让周围整行复读漏检；累计用量先校验数值、保留独立快照，支持无total字段。保留原混合/期限/租约保护，见[来源对照](THIRD-PARTY-zh.md)。

| 功能 | 行为 |
| --- | --- |
| 循环与混合复读 | 检测单次生成的高密度推理/正文重复，支持确认的前置 thinking 视图。 |
| 生成期限与恢复 | 默认共享300秒期限；符合条件的空答/纯推理循环最多恢复一次，正文、拒答或工具后禁止重放。 |
| 推理历史和图片 | 两种推理别名共享65536字符预算；只处理出站副本，保留工具配对和用户图片。 |
| 401续期 | 请求触发刷新至多一次网络尝试，原截止时间与30秒封顶，1MiB响应上限，独立连接。 |
| 协议边界 | 六条流/非流路径保留拒答、原生Thinking与工具语义；单候选n=1，异常候选明确失败。 |
| 新版工具兼容 | 保留额外工具注入、命名空间自由格式工具回程、DSML打印调用恢复及空finish_reason归一化。 |
| 分块流与租约 | 保留v1.7.3的分块读取/写出和共享TLS；缓冲帧验证后释放连接，失败时丢弃，保留已接收用量。 |

上游 v1.7.4/1.7.5 增加默认关闭的SQLite日志及保留限制、checkpoint修复、凭据手动导入、新主题/统计面板、每日及手动模型目录刷新、来源标记和原始目录归档。模型目录/倍率现在优先使用实际API结果，显示可能变化；这些原生功能不代替防复读。SQLite日志是否启用沿用用户设置，公开包不自动开启。

## v1.7.6 行为变化

上游默认把`gpt-5.6-terra / gpt-5.6-luna / gpt-5.3-codex`重定向到`deepseek-v4.1-flash`。它只按模型名匹配，主动请求这些模型也会被改写，不识别请求是否来自后台。`WB_BACKGROUND_MODEL_NAMES=""`可关闭；名单和目标均可自定义。

`/v1/messages`、`/messages`及其count_tokens端点默认在转发前返回400；原API Key鉴权仍先执行。原因是转换层仍不透传`cache_control`，该更新没有修复缓存等价性。显式`WB_ENABLE_ANTHROPIC_MESSAGES=1`保留旧接口行为。升级前按已有调用决定兼容设置；本公开包保留上游默认，不自动启用这项协议。

## 安装

先保存旧镜像、源码、配置和数据，在新目录获取两个准确版本：

~~~sh
git clone --depth 1 --branch v1.7.6-ops.20261015.1 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v176
git clone --depth 1 --branch v1.7.6 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.7.6
git -C workbuddy2api-hub-v1.7.6 rev-parse HEAD
python3 -B ops-patch-v176/apply.py --verify-package
python3 -B ops-patch-v176/apply.py --target ./workbuddy2api-hub-v1.7.6 --dry-run
~~~

提交必须匹配上面的完整SHA。预检通过后，按[安装指南](docs/INSTALL.md)暂停唯一目标服务、应用完整补丁、构建并定向切换。已有旧补丁（包括v1.7.3）不能直接叠加；不要删除状态/备份绕过检查。旧Release/tag继续保留。

公共默认请求体上限保持官方16MiB；确有大历史需求时显式设置 `WB_MAX_PAYLOAD_BYTES=52428800`，评估并发内存。新版可能重建用量checkpoint缓存，历史JSONL应保留。调度、出口退避、权重、轻量签到模型等仍由使用者的既有设置决定，本补丁不代你启用。

## 验证与边界

离线runner包含70组Python回归，新增阶段期限、连续前缀、默认恢复提示、匿名证据持久化及可选日志库测试；面板使用官方Node合成DOM测试。CI检查Linux3.11/3.12与Windows3.11，实际结果见[验证记录](docs/VERIFICATION.md)。

测试使用合成账号、空数据和本机HTTP，不含真实会话。检测仍以单次生成为范围，尚无通用跨工具回合业务进展熔断。健康、模块指纹或离线测试不等于真实长会话输出质量验收。

`/health/retry` 报告版本、来源与固定源码指纹，并提供 `observations` 请求/尝试分组。默认窗口从本进程启动开始，可用 `since/until` Unix时间戳和 `model` 筛选。`legacy_window` 明确原历史统计范围；合法输出恢复与业务完成分别说明。新观察文件上限约30MiB，接口最多扫描20000行，报告保留范围和截断状态；只保存固定原因码、数值、版本和网关生成的请求ID。`WB_OPS_OBSERVE=0` 关闭新文件写入。

端点遵循原API-key鉴权；没有设置Key时不会额外添加鉴权。提交问题时提供脱敏版本、协议、错误码和最小合成例子，不上传账号、完整配置、历史或备份镜像。
