# Attribution

This unofficial patch bundle derives from
[WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub),
distributed under the MIT License. Its original copyright notice is preserved
in [LICENSE](LICENSE).

Supported upstream release: v1.6.19.
Pinned upstream commit: `55dbb9ebcac3d55b26442d9bbc8b2e11ece637da`.

The modified gateway, account and web-tool files include upstream code. `wb_ops.py`
contains retained operations safeguards, and `wb_stall.py` adds bounded mixed
reasoning-stall detection. This repository is maintained independently and is
not an official upstream release.

The unified patch under `patches/` makes the changes against the pinned
upstream source reviewable. Test inputs are synthetic and contain no account
records or private conversations.
