"""Legacy suites must run against the runner's isolated source and state."""
import importlib
import os
from pathlib import Path
import sys

source = os.environ.get("WB_TEST_SOURCE")
state = os.environ.get("WB_OPS_TEST_STATE")
if not source or not state:
    raise SystemExit("Use: python tests/run_all.py --source /path/to/workbuddy2api-hub")
sys.path.insert(0, source)
wb = importlib.import_module("wb_proxy")
root = Path(state) / str(os.getpid())
(root / "usage").mkdir(parents=True, exist_ok=True)
(root / "accounts").mkdir(parents=True, exist_ok=True)
wb.USAGE_DIR = str(root / "usage")
wb.USAGE_LOG = str(root / "usage" / "usage.jsonl")
wb.ACCOUNTS_DIR = str(root / "accounts")
wb.POOL = None
