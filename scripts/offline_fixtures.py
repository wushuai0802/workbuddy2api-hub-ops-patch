"""Adapt pinned upstream test stubs without changing their assertions."""


def prepare_test_fixtures(gateway):
    path = gateway / 'tests/_test_local_web_tools.py'
    source = path.read_text(encoding='utf8')
    for name in ('fake_open_upstream', 'always_tool_body', 'capture_open', 'passthrough_open'):
        old = 'def ' + name + '(body, session_key=None, target_realm=None):'
        new = 'def ' + name + '(body, session_key=None, target_realm=None, deadline_at=None):'
        assert source.count(old) == 1, 'pinned web-tool fixture changed: '+name
        source = source.replace(old, new)
    path.write_text(source, encoding='utf8')
    # These suites rebind the parent's hardcoded ACCOUNTS_DIR, but the inline
    # child runner omits that rebind. Keep both pricing/realm fingerprints on
    # their synthetic directory rather than the gateway's empty default dir.
    for name in ('_test_usage_aggregate_cache.py', '_test_analytics_hour_buckets.py'):
        path = gateway / 'tests' / name
        source = path.read_text(encoding='utf8')
        old = 'import wb_proxy as P\nimport wb_pricing\nwb_pricing.set_data_dir(P.USAGE_DIR)'
        new = 'import wb_proxy as P\nP.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]\nimport wb_pricing\nwb_pricing.set_data_dir(P.USAGE_DIR)'
        assert source.count(old) == 1, 'pinned cache child fixture changed: '+name
        path.write_text(source.replace(old, new), encoding='utf8')
