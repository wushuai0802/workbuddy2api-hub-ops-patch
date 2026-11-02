# WorkBuddy2API-Hub Ops Patch

[中文](README.md) · [Protection contracts](docs/PROTECTIONS.md) · [Install and rollback](docs/INSTALL.md) · [Verification limits](docs/VERIFICATION.md)

Cumulative operational safeguards for [WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub). Patch `v1.10.1-ops.20261102.1` requires Python 3.11+ and official v1.10.1 at `28527f48cfaea0e456612eb8922cb51b5d804a22`. MIT attribution remains in [NOTICE](NOTICE.md) and [THIRD-PARTY-zh](THIRD-PARTY-zh.md).

## Install and rollback

Download the ZIP and SHA256SUMS.txt from the [formal release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.10.1-ops.20261102.1), or obtain its exact tag. Preserve the old image, source and data, then preflight a clean pinned official source tree:

~~~sh
python3 -B apply.py --verify-package
python3 -B apply.py --target /absolute/path/to/clean-official-source --dry-run
~~~

Follow [INSTALL](docs/INSTALL.md) for complete application and targeted deployment. Earlier patched trees are not overlays. Roll back with the installer's backup and previous image while preserving current accounts, configuration and usage history; do not remove state to bypass checks.

## Verification limits

Current main inventories 236 Python suites (53 patch and 183 official) and 65 Node suites across Linux Python 3.11/3.12 and Windows Python 3.11. Synthetic tests, health and fingerprints do not establish real long-session completion, CodeArts login or removal of cloud restrictions. The installer does not change accounts, keys, routing or schedules. [PROTECTIONS](docs/PROTECTIONS.md) is the contract reference; [PATCHES](docs/PATCHES.md) links to details.

## Maintenance and history

Address security issues or observed production faults promptly; review other changes every 1–2 weeks. New safeguards require real evidence or a reproducible counterexample. Main and new ZIPs contain only the current patch. Recover an old patch with `git show <tag>:patches/<patch-id>.patch`; existing release tags and assets remain available. See [history](docs/history/README.md) for original version notes and verification, and [CHANGELOG](CHANGELOG.md).
