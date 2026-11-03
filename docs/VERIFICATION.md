# 验证与边界

## 维护与升级节奏（2026-11-02）

只有安全问题或线上真实遇到的问题才立即跟版；其他上游改动先汇总，每 1–2 周合并处理。新增保护须有真实故障证据或可复现反例，并登记在 [PROTECTIONS.md](PROTECTIONS.md)；不能只因猜测添加运行时分支。

官方 Python 清单由 `BASELINE.json.official_python_tests` 绑定固定提交、明确数量和文件名/内容摘要；`generate_metadata.py` 还要求该提交对应的预期数量和干净官方测试。升版必须显式审查元数据，清单增加、删除、同名改内容均不能静默放行。

429日期套件现由官方v1.10.7按运行时钟生成未来样本，原字节/原断言直接执行，不再套用旧冻结时钟。适配器验证已审查的动态时钟AST形状；旧固定日期兼容路径仅供维护回归。其他既有夹具适配维持原合同。

维护回归 `test_maintenance.py` 保留；当前清单为250 Python（54补丁＋196官方）与65 Node。`test_v1107.py` 新增12项合同，覆盖连接快照、六路径失败/恢复归因、CodeArts跨池/忙时所有权、原生区域映射、媒体关闭/URL边界/额度名额、跨线程进程大Unicode账本及写失败诊断、有界自动签到。Actions保持完整commit SHA。

ZIP由manifest白名单打包，仅收录当前patch；历史补丁保留在git和原tag/Release。正式发布只使用准确main提交通过的CI artifact；候选和main均须三平台成功并核对日志计数。运行身份由BASELINE与实际payload哈希绑定。

v1.10.7的Windows便携依赖测试新增了官方Windows内建模块表；测试适配器沿该表补入Linux为扩展、目标Windows为内建的zlib，保留缺依赖断言。媒体URL官方用例的数字公网IP仅使用固定DNS元组，仍禁止公网连接；旧Key并发HTTP夹具补全请求realm状态，所有并发断言保留。便携运行时夹具仅用于依赖合同，不宣称已构建便携运行时。

## 当前验证方式与边界

~~~sh
python3 -B apply.py --verify-package
python3 -B scripts/test_offline.py --upstream /absolute/path/to/clean-official-source --output .work/verification
python3 -B scripts/test_dashboard.py --upstream /absolute/path/to/clean-official-source --output .work/dashboard
~~~

官方源码必须匹配 BASELINE.json。CI 在 Linux Python 3.11/3.12 和 Windows Python 3.11 运行同一完整清单。套件退出码、summary.json 和各套件日志才是对应提交的证据；未完成或平台跳过项须单独说明。

测试使用合成账号、空数据及本机 HTTP/socket，不触发真实模型、签到或活跃。公开测试和健康/模块指纹不测量真实长会话完成率，不证明 CodeArts 登录、云端解封或其他供应商线路可用。安装与回退保留现用业务数据；权限或依赖限制导致的跳过不等同功能已验收。

当前保护合同见 [PROTECTIONS.md](PROTECTIONS.md)；各轮原始验证记录已[原文归档](history/VERIFICATION-versions-before-round2-20261102.md)，旧清单和曾经失败仅属于对应版本。版本背景见 [V1107.md](V1107.md)，历史获取方法见 [history](history/README.md)。
