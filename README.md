# WorkBuddy2API-Hub 运维补丁

[English](README.en.md) · [安装与回滚](docs/INSTALL.md) · [补丁范围](docs/PATCHES.md) · [验证记录](docs/VERIFICATION.md)

为 WorkBuddy 网关提供 DeepSeek 复读保护、受限认证续期、推理历史与工具图片兼容，以及流式协议边界修复。基于[官方项目](https://github.com/ardeyouxipianyi/workbuddy2api-hub)，可检查、可回退，遵循 MIT。

当前版本：**v1.7.3-ops.20261014.1**。

- [下载 ZIP 与 SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.7.3-ops.20261014.1)
- 文件名：`workbuddy2api-hub-v1.7.3-wushuai0802-ops-20261014-r1.zip`
- 仅支持官方 **v1.7.3**，提交 `c7dad8fe32316d47ce4f0ff260bb7ac7802b8a94`。
- Python 3.11+。Docker 用户从打过完整补丁的准确源码重新构建镜像。
- 安装器只替换四个上游模块（proxy、accounts、webtools、upstream_pool），新增 ops、stall 两个模块；不操作账号、Key、配置、历史或部署。

本版在官方 v1.7.3 上重基用户提供的 v1.7.2 补丁，保留我们现有混合复读、推理别名预算、工具图片、拒答及连接所有权保护。来源与许可见 [NOTICE.md](NOTICE.md) 和 [THIRD-PARTY-zh.md](THIRD-PARTY-zh.md)。

| 功能 | 行为 |
| --- | --- |
| 循环与混合复读 | 检测单次生成的高密度推理/正文重复，支持确认的前置 thinking 视图。 |
| 生成期限与恢复 | 默认共享300秒期限；符合条件的空答/纯推理循环最多恢复一次，正文、拒答或工具后禁止重放。 |
| 推理历史和图片 | 两种推理别名共享65536字符预算；只处理出站副本，保留工具配对和用户图片。 |
| 401续期 | 请求触发刷新至多一次网络尝试，原截止时间与30秒封顶，1MiB响应上限，独立连接。 |
| 协议边界 | 六条流/非流路径保留拒答、原生Thinking与工具语义；单候选n=1，异常候选明确失败。 |
| 新版工具兼容 | 保留额外工具注入、命名空间自由格式工具回程、DSML打印调用恢复及空finish_reason归一化。 |
| 分块流与租约 | 保留v1.7.3的分块读取/写出和共享TLS；缓冲帧验证后释放连接，失败时丢弃，保留已接收用量。 |

上游 v1.7.3 的原生改动包括流式性能、命名空间工具闭环、面板限高/吸顶、响应头超时、国际活跃轻量模型与最近请求显示修复。跨过 v1.7.0/1.7.2 还包含风控403轮换、单请求失败计账去重、默认关闭的出口退避、权重/备注、多引擎网页工具与用量缓存优化。完整归属与兼容说明见补丁范围。

## 安装

先保存旧镜像、源码、配置和数据，在新目录获取两个准确版本：

~~~sh
git clone --depth 1 --branch v1.7.3-ops.20261014.1 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v173
git clone --depth 1 --branch v1.7.3 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.7.3
git -C workbuddy2api-hub-v1.7.3 rev-parse HEAD
python3 -B ops-patch-v173/apply.py --verify-package
python3 -B ops-patch-v173/apply.py --target ./workbuddy2api-hub-v1.7.3 --dry-run
~~~

提交必须匹配上面的完整SHA。预检通过后，按[安装指南](docs/INSTALL.md)暂停唯一目标服务、应用完整补丁、构建并定向切换。已有v1.6.20或v1.7.2补丁不能直接叠加；不要删除状态/备份绕过检查。旧[20261012版本](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261012.1)及更早tag继续保留。

公共默认请求体上限保持官方16MiB；确有大历史需求时显式设置 `WB_MAX_PAYLOAD_BYTES=52428800`，评估并发内存。新版可能重建用量checkpoint缓存，历史JSONL应保留。调度、出口退避、权重、轻量签到模型等仍由使用者的既有设置决定，本补丁不代你启用。

## 验证与边界

Windows/Python3.14.7的56组离线回归通过，包括原有保护、附件的迁移/DSML合同、分块与TLS、命名空间/自定义工具、退避和统计；CI检查Linux3.11/3.12与Windows3.11。具体运行状态见[验证记录](docs/VERIFICATION.md)，配置存在不代表CI成功。

测试使用合成账号、空数据和本机HTTP，不含真实会话。检测仍以单次生成为范围，尚无通用跨工具回合业务进展熔断。健康、模块指纹或离线测试不等于真实长会话输出质量验收。

`/health/retry` 报告版本、来源与导入时固定源码指纹，与 `BASELINE.json` 的 `payload_fingerprint` 对照。它遵循原API-key鉴权；没有设置Key时不会额外添加鉴权。提交问题时提供脱敏的版本、协议、错误码和最小合成例子，不上传账号、完整配置、历史或备份镜像。
