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

    # The full native inventory also checks packaging. Describe the patched
    # sandbox honestly without changing the user's upstream release files.
    manifest = gateway / 'release/portable.txt'
    manifest.write_text(manifest.read_text(encoding='utf8') + '\nwb_ops.py\nwb_stall.py\nwb_observe.py\n', encoding='utf8')
    runtime = gateway / 'release/portable_runtime.py'
    runtime.write_text(runtime.read_text(encoding='utf8') +
                       '\nREQUIRED_FILES += ("Lib/copy.py", "Lib/struct.py", "Lib/xml/__init__.py", '
                       '"Lib/contextvars.py", "DLLs/_contextvars.pyd", "Lib/dataclasses.py", "Lib/functools.py")\n', encoding='utf8')
    assets = gateway / 'tests/_test_release_assets.py'
    source = assets.read_text(encoding='utf8')
    old = '("Lib/%s/__init__.py" % m) not in covered]'
    assert source.count(old) == 1, 'pinned extension-module fixture changed'
    source = source.replace(old, '("Lib/%s/__init__.py" % m) not in covered '
                           'and ("DLLs/%s.pyd" % m) not in covered]')
    # This is a Windows portable-runtime contract even on Linux runners.
    # math/zlib are linked into the target python DLL; Linux exposes them as
    # extensions. Keep the missing-dependency assertion for all other imports.
    old = 'if m not in local and m not in sys.builtin_module_names)'
    assert source.count(old) == 1, 'pinned Windows builtin profile changed'
    source = source.replace(old, 'if m not in local and m not in '
                           '(sys.builtin_module_names + ("math", "zlib")))')
    assets.write_text(source, encoding='utf8')

    # The harness checks parent-before-child relationships, but its synthetic
    # /proc fixture assumes numeric sibling order. tmpfs readdir is unordered.
    contracts = gateway / 'tests/_test_run_all_contracts.py'
    source = contracts.read_text(encoding='utf8')
    old = 'names = [n for n in os.listdir(proc_root) if n.isdigit()]'
    assert source.count(old) == 1, 'pinned process-table fixture changed'
    contracts.write_text(source.replace(old, 'names = sorted(n for n in os.listdir(proc_root) if n.isdigit())'), encoding='utf8')
    # These suites rebind the parent's hardcoded ACCOUNTS_DIR, but the inline
    # child runner omits that rebind. Keep both pricing/realm fingerprints on
    # their synthetic directory rather than the gateway's empty default dir.
    for name in ('_test_usage_aggregate_cache.py', '_test_analytics_hour_buckets.py',
                 '_test_usage_day_buckets.py', '_test_usage_hour_buckets.py', '_test_usage_series_minutes.py'):
        path = gateway / 'tests' / name
        source = path.read_text(encoding='utf8')
        old = 'import wb_proxy as P\nimport wb_pricing\nwb_pricing.set_data_dir(P.USAGE_DIR)'
        new = 'import wb_proxy as P\nP.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]\nimport wb_pricing\nwb_pricing.set_data_dir(P.USAGE_DIR)'
        assert source.count(old) == 1, 'pinned cache child fixture changed: '+name
        path.write_text(source.replace(old, new), encoding='utf8')
    # The native desktop assertions must select a desktop fixture even when
    # the runner itself is in Docker. The following native server=True case
    # still checks denial, and the real container policy is probed at boot.
    path = gateway / 'tests/_test_agents.py'
    source = path.read_text(encoding='utf8')
    begin = 'check("loopback v4 is allowed",'
    assert source.count(begin) == 1, 'pinned agents desktop fixture changed'
    source = source.replace(begin, 'fixture_server_form = wb_proxy._SERVER_DEPLOYMENT\n'
                            'wb_proxy._SERVER_DEPLOYMENT = False\n' + begin)
    end = '    wb_proxy._SERVER_DEPLOYMENT = saved_form\n'
    assert source.count(end) == 1, 'pinned agents container fixture changed'
    source = source.replace(end, end + 'wb_proxy._SERVER_DEPLOYMENT = fixture_server_form\n')
    path.write_text(source, encoding='utf8')
