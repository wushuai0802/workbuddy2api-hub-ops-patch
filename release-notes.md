升级官方v1.10.7，参考r1补丁迁移，保留全部既有运维保护。

- 基线 `d177043f873fd1e7132b3219be24e4f1f7abefda`，补丁 `v1.10.7-ops.20261103.1`，9个payload。
- 原生媒体、Key持久化、DeepSeek区域费率/出站路由、CodeArts并发/OAuth与有界可选自动签到沿用官方；媒体和自动签到默认关闭。
- 连接快照贯穿六接口成功、失败和恢复归因，换连接清旧出口；CodeArts跨池隔离保留。账本用官方原子追加；媒体额度拒绝/异常单次释放，非流式关闭仍由现有守卫拥有。
- 新增12项合成合同，清单250 Python（54补丁＋196官方）与65 Node。未移除保护或调整循环阈值、重试、期限、白名单、风控；完整保护合同见docs/PROTECTIONS.md。
- ZIP取自准确main提交通过的三平台CI artifact，SHA256SUMS随包发布。账号、Key、设置、历史和风控不由安装器修改。无真实模型、媒体、签到、活跃或消息验收，不据合成测试宣称云端可用。
- Pin official v1.10.7. Preserve native features and all existing protection contracts, add immutable per-attempt provenance, use native atomic ledger append and close the media quota slot leak. Inventory: 250 Python and 65 Node suites; no live-cloud availability claim.
