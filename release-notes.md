升级官方v1.10.9，保留现有运维保护和用户配置。

- 准确基线`077fb2fcfc273e29bd1266e527afc4a3d548b516`，补丁`v1.10.9-ops.20261105.1`。
- 采用官方CodeArts工具归一化/空函数修复、配置并发锁、账号任务/备注/保活、图片/视觉及推理参数、429状态码保留，另有日志轮转、namespace和面板更新。
- 国际Flash取消旧免费/强制-sg切换，遵循官方目录费率及原模型身份。保留全部循环、期限、认证、名额、记账、白名单和风控保护；六模块字节/29关键AST保持。
- 新增5项跨模块回归，完整清单253 Python（55补丁＋198官方）与66 Node。候选和main三平台分别验证，正式ZIP只取通过的准确main CI artifact。
- 安装器不改账号、Key、任务或自动领取设置。没有额外真实模型、图片生成、手动签到/活跃或云端解封验收，正常授权排程可能自然运行。
- Pin official v1.10.9. Preserve native CodeArts tools/configuration/tasks/vision/429 behavior and all existing protection contracts. Five added cross-component cases bring the inventory to 253 Python and 66 Node suites. Retain user settings and publish exact passing main-CI bytes; no live-provider availability claim.
