"""Managed-server controls, HTTP body ownership and upstream rate-window fixes."""
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest import mock

from test_integration import MODEL, wb


class V1712Tests(unittest.TestCase):
    def handler(self):
        h = wb.Handler.__new__(wb.Handler)
        h._json = mock.Mock(side_effect=lambda code, value: (code, value))
        h._error = mock.Mock(side_effect=lambda code, message, *_: (code, message))
        h._handle_expect_continue = mock.Mock()
        h._discard_body = mock.Mock()
        return h

    def test_web_logs_follow_all_platforms_and_only_shutdown_depends_on_management(self):
        for platform, managed in (('nt', False), ('posix', False), ('nt', True), ('posix', True)):
            with self.subTest(platform=platform, managed=managed), \
                 mock.patch.object(wb.os, 'name', platform), \
                 mock.patch.object(wb, 'server_deployment_form', return_value=managed):
                self.assertEqual(wb._console_capabilities(), {'supported': True, 'shutdown_supported': not managed})

    def test_server_state_does_not_probe_a_desktop_viewer(self):
        h = self.handler()
        with mock.patch.object(wb, '_SERVER_DEPLOYMENT', True), \
             mock.patch('subprocess.Popen') as running:
            code, value = h._get_console_state()
        self.assertEqual(code, 200)
        self.assertEqual(value, {'supported': True, 'mode': 'web', 'shutdown_supported': False, 'visible': False})
        running.assert_not_called()

    def test_server_rejects_controls_after_consuming_body_without_process_actions(self):
        for route in ('_route_shutdown',):
            h = self.handler()
            with self.subTest(route=route), mock.patch.object(wb, '_SERVER_DEPLOYMENT', True), \
                 mock.patch('subprocess.Popen') as launch, \
                 mock.patch.object(wb.threading, 'Thread') as thread:
                self.assertEqual(getattr(h, route)()[0], 403)
                h._handle_expect_continue.assert_called_once()
                h._discard_body.assert_called_once()
                self.assertTrue(h._body_consumed)
                launch.assert_not_called(); thread.assert_not_called()

    def test_panel_auth_is_required_before_either_control(self):
        for path in ('/shutdown', '/console/toggle'):
            h = self.handler(); h.path = path; h._panel_ok = mock.Mock(return_value=False)
            with self.subTest(path=path), mock.patch.object(wb, '_console_capabilities') as capabilities:
                self.assertEqual(wb.Handler.do_POST(h)[0], 401)
                capabilities.assert_not_called()

    def test_legacy_toggle_reports_the_web_log_page_without_spawning(self):
        h = self.handler()
        with mock.patch('subprocess.Popen') as launch:
            self.assertEqual(h._route_console_toggle(), (200, {'ok': True, 'mode': 'web',
                            'url': '/dashboard?tab=logs', 'visible': False}))
            self.assertTrue(h._body_consumed)
            launch.assert_not_called()

    def test_desktop_shutdown_remains_available_and_acknowledged(self):
        h = self.handler()
        with mock.patch.object(wb, '_console_capabilities', return_value={'shutdown_supported': True}), \
             mock.patch.object(wb.threading, 'Thread') as thread, \
             mock.patch.object(wb.time, 'sleep'), mock.patch.object(wb.os, '_exit') as exit_process:
            self.assertEqual(h._route_shutdown(), (200, {'ok': True, 'shutting_down': True}))
            thread.return_value.start.assert_called_once()
            exit_process.assert_not_called()
            thread.call_args.kwargs['target']()
            exit_process.assert_called_once_with(0)

    def test_server_denials_preserve_real_keepalive_connection_for_content_and_chunked_bodies(self):
        class WebHandler(wb.Handler):
            def _panel_ok(self): return True
            def log_message(self, *_): pass
            def _route_shutdown(self):
                # A regression must fail this test without actually exiting.
                with mock.patch.object(wb.threading, 'Thread') as thread:
                    value = super()._route_shutdown()
                    self.server.shutdown_threads += thread.call_count
                    return value
        server = ThreadingHTTPServer(('127.0.0.1', 0), WebHandler)
        server.shutdown_threads = 0
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        client = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            with mock.patch.object(wb, '_SERVER_DEPLOYMENT', True), \
                 mock.patch('subprocess.Popen') as launch:
                for route in ('/console/toggle', '/shutdown'):
                    for chunked in (False, True):
                        body = [b'{', b'}'] if chunked else b'{}'
                        headers = {'Content-Type': 'application/json'}
                        if chunked: headers['Transfer-Encoding'] = 'chunked'
                        client.request('POST', route, body=body, headers=headers, encode_chunked=chunked)
                        response = client.getresponse(); data = json.loads(response.read())
                        self.assertEqual(response.status, 200 if route == '/console/toggle' else 403, data)
                        client.request('GET', '/console/state')
                        response = client.getresponse(); value = json.loads(response.read())
                        self.assertEqual(response.status, 200)
                        self.assertTrue(value['supported']); self.assertFalse(value['shutdown_supported'])
                launch.assert_not_called()
                self.assertEqual(server.shutdown_threads, 0)
        finally:
            client.close(); server.shutdown(); server.server_close(); worker.join(3)

    def test_upstream_soft_rate_window_resets_both_ladders_without_extending_old_history(self):
        account = wb.wb_accounts.Account({'uid': 'synthetic', 'realm': 'intl', 'accessToken': 'synthetic'})
        account.soft_streak = 4; account.unscoped_streak = 3; account.last_soft_at = 1000
        with mock.patch.object(wb.wb_accounts, 'FAIL_WINDOW', 600), \
             mock.patch.object(wb.wb_accounts.time, 'time', return_value=2000):
            self.assertEqual(account.note_soft_rate('synthetic'), wb.wb_accounts.soft_backoff(1))
            self.assertEqual((account.soft_streak, account.unscoped_streak), (1, 0))
            self.assertEqual(account.note_unscoped_rate(MODEL), wb.wb_accounts.unverified_soft_backoff(1))
            self.assertEqual((account.soft_streak, account.unscoped_streak), (1, 1))


if __name__ == '__main__': unittest.main()
