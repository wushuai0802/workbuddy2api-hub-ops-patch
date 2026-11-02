# 验证与边界

## 维护与升级节奏（2026-11-02）

只有安全问题或线上真实遇到的问题才立即跟版；其他上游改动先汇总，每 1–2 周合并处理。新增保护须有真实故障证据或可复现反例，并登记在 [PROTECTIONS.md](PROTECTIONS.md)；不能只因猜测添加运行时分支。

官方 Python 清单由 `BASELINE.json.official_python_tests` 绑定固定提交、明确数量和文件名/内容摘要；`generate_metadata.py` 还要求该提交对应的预期数量和干净官方测试。升版必须显式审查元数据，清单增加、删除、同名改内容均不能静默放行。

429 日期套件由适配器读取原样本与字面 epoch，选择早于所有有效样本且晚于过期样本的合成墙钟，仅在该子进程通过 runpy 执行。没有写死某天的“现在”，没有改写该官方测试文件，全部原断言保留。其他既有夹具适配维持原合同。

维护回归增加 `test_maintenance.py`（日期样本跨年、形状变化拒绝、官方源字节不改、清单内容绑定），本地/CI 总清单为 236 Python（53 补丁＋183 官方）与 65 Node。Actions 用实际查询的完整 commit SHA，并在旁注保留原版本号。

下次构建的 ZIP 由 manifest 白名单打包，仅收录当前 `patch_id` 的 `.patch`；历史补丁继续保留在 git 和原 tag/Release。构建器拒绝意外收录历史补丁的 manifest。本轮不新发 Release，不替换已发布资产，运行 `files/` 和 payload 指纹保持既有版本。

## 当前验证方式与边界

~~~sh
python3 -B apply.py --verify-package
python3 -B scripts/test_offline.py --upstream /absolute/path/to/clean-official-source --output .work/verification
python3 -B scripts/test_dashboard.py --upstream /absolute/path/to/clean-official-source --output .work/dashboard
~~~

官方源码必须匹配 BASELINE.json。CI 在 Linux Python 3.11/3.12 和 Windows Python 3.11 运行同一完整清单。套件退出码、summary.json 和各套件日志才是对应提交的证据；未完成或平台跳过项须单独说明。

测试使用合成账号、空数据及本机 HTTP/socket，不触发真实模型、签到或活跃。公开测试和健康/模块指纹不测量真实长会话完成率，不证明 CodeArts 登录、云端解封或其他供应商线路可用。安装与回退保留现用业务数据；权限或依赖限制导致的跳过不等同功能已验收。

当前保护合同见 [PROTECTIONS.md](PROTECTIONS.md)；各轮原始验证记录已[原文归档](history/VERIFICATION-versions-before-round2-20261102.md)，旧清单和曾经失败仅属于对应版本。版本背景见 [V1101.md](history/V1101.md)，历史获取方法见 [history](history/README.md)。
