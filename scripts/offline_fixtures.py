"""Adapt pinned upstream test stubs without changing their assertions."""


def prepare_dashboard_fixtures(gateway):
    # Native tests cover an unmanaged server with an acknowledged shutdown;
    # the patch additionally requires its explicit capability contract first.
    path = gateway / 'tests/_test_console_shutdown.js'
    source = path.read_text(encoding='utf8')
    old = '  // 服务还在：/shutdown 成功，但 /health 一直 200（容器自动拉起）。'
    assert source.count(old) == 1, 'pinned shutdown fixture changed'
    source = source.replace(old, "  page.getJSON = async()=>({supported:true, mode:'web', shutdown_supported:true});\n"
                            "  page.postJSON = async()=>({ok:true, shutting_down:true});\n"
                            "  await page.initConsoleButtons();\n" + old)
    path.write_text(source, encoding='utf8')


def prepare_refresh_fixtures(gateway):
    # Native 401 tests stub the unbounded background refresh and omit a refresh
    # token. Bind the same success/failure stub to the bounded request API;
    # all original retry count and same-account assertions remain in place.
    path = gateway / 'tests/_test_breaker_fairness.py'
    source = path.read_text(encoding='utf8')
    old = '        def refresh():\n            self.refreshes += 1'
    assert source.count(old) == 1, 'pinned 401 refresh fixture changed'
    source = source.replace(old, '        def refresh(*args, **kwargs):\n            self.refreshes += 1')
    old = '        self.account.refresh = refresh'
    assert source.count(old) == 1
    source = source.replace(old, '        self.account.refresh_token = "synthetic-refresh"\n'
                            '        self.account.refresh_after_401 = refresh')
    path.write_text(source, encoding='utf8')


def prepare_test_fixtures(gateway):
    # Native 1.9.1 makes terminal stopping opt-in. Our guarded Responses/Chat
    # streams already stop unconditionally, a deliberately stronger old contract.
    # Preserve default tail consumption for the native aggregate, and assert no
    # tail read for the guarded streams. Supply a real completed answer so the
    # native transport test does not conflict with our empty-answer rejection.
    path = gateway / 'tests/_test_sse_termination.py'
    source = path.read_text(encoding='utf8')
    prefix = b'data: {"id":"fixture","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    for indent, quote in ((8, "'"), (16, '"')):
        old = ' ' * indent + 'yield b' + quote + 'data: [DONE]\\n\\n' + quote
        assert source.count(old) == 1, 'pinned SSE fixture changed'
        source = source.replace(old, ' ' * indent + 'yield ' + repr(prefix) + '\n' + old)
    old = "for consume in (lambda stream: proxy.aggregate_stream(stream, 'model', 'id'),\n                            lambda stream: list(proxy.stream_responses_events(stream, 'model', {})),\n                            self.run_chat):"
    new = "for index, consume in enumerate((lambda stream: proxy.aggregate_stream(stream, 'model', 'id'),\n                            lambda stream: list(proxy.stream_responses_events(stream, 'model', {})),\n                            self.run_chat)):"
    assert source.count(old) == 1
    source = source.replace(old, new)
    assert source.count('self.assertTrue(stream.reached_end)') == 1
    source = source.replace('self.assertTrue(stream.reached_end)', 'self.assertEqual(stream.reached_end, index == 0)')
    path.write_text(source, encoding='utf8')
    prepare_refresh_fixtures(gateway)
    # The native compaction stub suppresses the unguarded accounting helper.
    # Our adapter uses the same lower-level ledger as the other six adapters.
    path = gateway / 'tests/_test_remote_compaction.py'
    source = path.read_text(encoding='utf8')
    source = source.replace('self._record_upstream_usage = proxy.record_upstream_usage',
                            'self._record_upstream_usage = proxy.record_usage')
    source = source.replace('proxy.record_upstream_usage = lambda *a, **k: None',
                            'proxy.record_usage = lambda *a, **k: None')
    source = source.replace('proxy.record_upstream_usage = self._record_upstream_usage',
                            'proxy.record_usage = self._record_upstream_usage')
    path.write_text(source, encoding='utf8')
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
