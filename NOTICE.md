# Attribution

This unofficial patch bundle derives from
[WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub),
distributed under the MIT License. Its original copyright notice is preserved
in [LICENSE](LICENSE).

Supported upstream release: v1.6.20.
Pinned upstream commit: `cb6a844588705ea01388be2b9df1f0cbbbcde357`.

The historical v1.6.19 bundle remains available at the
[v1.6.19-ops.20261011.2 release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.19-ops.20261011.2).
Use its pinned tag and documentation for that baseline.

The modified gateway, account and web-tool files include upstream code. `wb_ops.py`
contains retained operations safeguards, and `wb_stall.py` adds bounded mixed
reasoning-stall detection. This repository is maintained independently and is
not an official upstream release.

Upstream v1.6.20's connection pool, dashboard, sign-in/streak manager,
remaining-usage estimation, account-limit handling and performance changes
retain their upstream attribution. They are not claimed as original patch work.

The unified patch under `patches/` makes the changes against the pinned
upstream source reviewable. Test inputs are synthetic and contain no account
records or private conversations.
