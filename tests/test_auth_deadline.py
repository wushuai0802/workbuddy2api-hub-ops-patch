"""Absolute 401 budgets, bounded fresh responses and watchdog socket ownership.

Run with the isolated source/state and socket audit supplied by test_offline.
All accounts and credentials are synthetic; actual sockets use 127.0.0.1 only.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_accounts as accounts


TOKEN_BODY = json.dumps({"data": {"accessToken": "synthetic-new",
                                   "refreshToken": "synthetic-rotated"}}).encode()


def account():
    return accounts.Account({"uid": "synthetic-deadline", "realm": "intl",
                             "accessToken": "synthetic-old", "refreshToken": "synthetic-refresh"})


class DeadlineBudgetTests(unittest.TestCase):
    def test_cold_tls_context_past_deadline_never_connects(self):
        clock = [100.0]
        def cold_context():
            clock[0] += .3
            return mock.Mock()
        with mock.patch.object(accounts.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(accounts.wb_upstream_pool, 'shared_ssl_context', side_effect=cold_context), \
                mock.patch.object(accounts.socket, 'getaddrinfo') as resolve:
            credential = account()
            self.assertFalse(credential.refresh_after_401('synthetic-old', timeout=.2, deadline_at=100.2))
        resolve.assert_not_called()
        self.assertEqual(credential.access_token, 'synthetic-old')

    def test_transient_failure_has_one_attempt_and_no_backoff(self):
        clock, calls = [100.0], []

        def fail(_request, timeout, **_kwargs):
            calls.append(timeout)
            clock[0] += timeout
            raise urllib.error.URLError("synthetic transport failure")

        opener = mock.Mock(open=fail)
        with mock.patch.object(accounts.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(accounts.time, "sleep") as sleep, \
                mock.patch.object(accounts, "urlopen", side_effect=fail), \
                mock.patch.object(accounts.urllib.request, "build_opener", return_value=opener):
            credential = account()
            self.assertFalse(credential.refresh_after_401("synthetic-old", timeout=.2))
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(clock[0] - 100, .2)
        sleep.assert_not_called()
        self.assertEqual(credential.access_token, "synthetic-old")

    def test_shared_deadline_includes_lock_wait(self):
        clock = [100.0]

        class Lock:
            def acquire(self, timeout):
                self.timeout = timeout
                clock[0] += .3
                return True

            def release(self):
                self.released = True

        credential = account()
        lock = credential._refresh_lock = Lock()
        with mock.patch.object(accounts.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(accounts, "http_json", return_value={"data": {
                    "accessToken": "synthetic-new"}}) as refresh:
            self.assertTrue(credential.refresh_after_401("synthetic-old", timeout=30, deadline_at=101))
        self.assertEqual(lock.timeout, 1)
        self.assertTrue(lock.released)
        self.assertAlmostEqual(refresh.call_args.kwargs["timeout"], .7)
        self.assertEqual(refresh.call_args.kwargs["deadline_at"], 101)
        self.assertEqual(refresh.call_args.kwargs["retries"], 1)

    def test_lock_consumes_entire_deadline_without_network_or_token_reuse(self):
        clock = [100.0]

        class Lock:
            def acquire(self, timeout):
                clock[0] += timeout
                return True

            def release(self):
                self.released = True

        for already_refreshed in (False, True):
            with self.subTest(already_refreshed=already_refreshed):
                clock[0] = 100
                credential = account()
                if already_refreshed:
                    credential.access_token = "synthetic-new"
                lock = credential._refresh_lock = Lock()
                with mock.patch.object(accounts.time, "monotonic", side_effect=lambda: clock[0]), \
                        mock.patch.object(accounts, "http_json") as refresh:
                    self.assertFalse(credential.refresh_after_401("synthetic-old", deadline_at=100.2))
                refresh.assert_not_called()
                self.assertTrue(lock.released)

    def test_refresh_budget_is_capped_at_thirty_seconds(self):
        credential = account()
        with mock.patch.object(accounts.time, "monotonic", return_value=100), \
                mock.patch.object(accounts, "http_json", return_value={"data": {
                    "accessToken": "synthetic-new"}}) as refresh:
            self.assertTrue(credential.refresh_after_401("synthetic-old", timeout=90, deadline_at=200))
        self.assertEqual(refresh.call_args.kwargs["deadline_at"], 130)
        self.assertEqual(refresh.call_args.kwargs["timeout"], 30)

    def test_late_success_does_not_rotate_credentials(self):
        clock, credential = [100.0], account()

        def late_response(*_args, **_kwargs):
            clock[0] = 100.3
            return {"data": {"accessToken": "synthetic-new", "refreshToken": "synthetic-rotated"}}

        with mock.patch.object(accounts.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(accounts, "http_json", side_effect=late_response):
            self.assertFalse(credential.refresh_after_401("synthetic-old", timeout=.2))
        self.assertEqual(credential.access_token, "synthetic-old")
        self.assertEqual(credential.refresh_token, "synthetic-refresh")

    def test_late_dns_lookup_never_connects_or_sends(self):
        owner = accounts._RefreshDeadline(time.monotonic() + 1)

        def late_lookup(*_args):
            owner.abort()
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]

        with mock.patch.object(accounts.socket, "getaddrinfo", side_effect=late_lookup), \
                mock.patch.object(accounts.socket, "socket") as new_socket:
            with self.assertRaises(TimeoutError):
                owner.connect(("127.0.0.1", 1))
        new_socket.assert_not_called()

    def test_late_connect_return_does_not_send_credentials(self):
        for connection_class in (accounts._DeadlineHTTPConnection, accounts._DeadlineHTTPSConnection):
            with self.subTest(connection_class=connection_class.__name__):
                clock, sock = [100.0], mock.Mock()
                connection = connection_class("127.0.0.1")
                connection._refresh_deadline = accounts._RefreshDeadline(101)

                def late_connect():
                    connection.sock = sock
                    clock[0] = 102

                with mock.patch.object(accounts.time, "monotonic", side_effect=lambda: clock[0]), \
                        mock.patch.object(connection, "connect", side_effect=late_connect):
                    with self.assertRaises(TimeoutError):
                        connection.send(b"synthetic credential request")
                sock.sendall.assert_not_called()

    def test_background_refresh_retains_three_attempts_and_backoff(self):
        credential, calls = account(), []

        def eventually_ready(_req, timeout, proxy):
            calls.append((timeout, proxy))
            if len(calls) < 3:
                raise urllib.error.URLError("synthetic transient failure")
            return io.BytesIO(TOKEN_BODY)

        with mock.patch.object(accounts, "urlopen", side_effect=eventually_ready), \
                mock.patch.object(accounts.time, "sleep") as sleep:
            self.assertTrue(credential.refresh())
        self.assertEqual(calls, [(30, "")] * 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1.0), mock.call(2.0)])
        self.assertEqual(credential.access_token, "synthetic-new")


class _Peer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def handle(self):
        try:
            super().handle()
        except OSError:
            self.close_connection = True

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.do_GET()

    def do_CONNECT(self):
        self.server.requests.append(("CONNECT", self.path, self.client_address[1], None))
        self.send_response(200, "Connection established")
        self.end_headers()
        self.wfile.flush()
        # Hold the TLS handshake after a valid tunnel response. No HTTP
        # refresh request or synthetic credential bytes should arrive.
        self.server.stop.wait(2)
        self.close_connection = True

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        scenario = self.server.scenarios[path]
        self.server.requests.append((path, self.path, self.client_address[1], self.headers.get("Connection")))
        body = scenario.get("body", TOKEN_BODY)
        try:
            if scenario.get("drip_headers"):
                header = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body)).encode()
                for start in range(0, len(header), 2):
                    self.wfile.write(header[start:start + 2])
                    self.wfile.flush()
                    if self.server.stop.wait(.025):
                        return
            else:
                status = scenario.get("status", 200)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if "redirect" in scenario:
                    self.send_header("Location", scenario["redirect"])
                if scenario.get("chunked"):
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(scenario.get("length", len(body))))
                self.end_headers()
            if scenario.get("drip_body"):
                for start in range(0, len(body), 2):
                    self.wfile.write(body[start:start + 2])
                    self.wfile.flush()
                    if self.server.stop.wait(.025):
                        return
            elif scenario.get("chunked"):
                self.wfile.write(("%x\r\n" % len(body)).encode() + body + b"\r\n0\r\n\r\n")
            else:
                self.wfile.write(body)
            self.wfile.flush()
        except (OSError, ValueError):
            self.close_connection = True


class DeadlineSocketTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"WB_UPSTREAM_KEEPALIVE": "1",
                                                      "NO_PROXY": "127.0.0.1,localhost"}))
        self.pool_module = accounts.wb_upstream_pool
        self.pool = self.pool_module._KeepAlivePool()
        self.enterContext(mock.patch.object(self.pool_module, "_POOL", self.pool))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Peer)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.server.scenarios, self.server.requests = {}, []
        self.server.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.stop.set()
        self.pool.close_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.server.server_port, path)

    def refresh(self, path, budget=.15, proxy=""):
        credential = account()
        credential.proxy = proxy
        cfg = dict(accounts.get_realm_config("intl"), chat_upstream=self.url(path))
        with mock.patch.object(accounts, "REFRESH_PATH", ""), \
                mock.patch.object(accounts, "get_realm_config", return_value=cfg):
            started = time.monotonic()
            result = credential.refresh_after_401("synthetic-old", timeout=budget,
                                                 deadline_at=started + budget)
        return credential, result, time.monotonic() - started

    def test_body_drip_stops_by_absolute_deadline_without_token_update(self):
        self.server.scenarios["/drip"] = {"drip_body": True}
        credential, result, elapsed = self.refresh("/drip")
        self.assertFalse(result)
        self.assertLess(elapsed, .5)
        self.assertEqual(credential.access_token, "synthetic-old")
        self.assertEqual(credential.refresh_token, "synthetic-refresh")
        self.assertEqual(len(self.server.requests), 1)

    def test_html_401_is_bounded_and_does_not_expire_a_healthy_credential(self):
        self.server.scenarios['/html-401'] = {'status': 401, 'body': b'<html>proxy 401</html>'}
        credential, result, elapsed = self.refresh('/html-401')
        self.assertFalse(result)
        self.assertFalse(credential.token_expired)
        self.assertLess(elapsed, .5)
        self.assertEqual(credential.access_token, 'synthetic-old')

    def test_slow_401_error_body_keeps_original_absolute_deadline(self):
        self.server.scenarios['/slow-401'] = {'status': 401, 'drip_body': True}
        credential, result, elapsed = self.refresh('/slow-401')
        self.assertFalse(result)
        self.assertFalse(credential.token_expired)
        self.assertLess(elapsed, .5)
        self.assertEqual(credential.access_token, 'synthetic-old')

    def test_header_drip_stops_by_the_same_deadline(self):
        self.server.scenarios["/headers"] = {"drip_headers": True}
        credential, result, elapsed = self.refresh("/headers")
        self.assertFalse(result)
        self.assertLess(elapsed, .5)
        self.assertEqual(credential.access_token, "synthetic-old")
        self.assertEqual(len(self.server.requests), 1)

    def test_content_length_and_chunked_body_have_one_mib_limit(self):
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                path = "/large-%s" % chunked
                self.server.scenarios[path] = {"chunked": chunked,
                    "body": TOKEN_BODY + b" " * (1024 * 1024)}
                credential, result, elapsed = self.refresh(path, budget=2)
                self.assertFalse(result)
                self.assertIn("1MiB", credential.last_error)
                self.assertFalse(credential.token_expired)
                self.assertLess(elapsed, 1)
                self.assertEqual(credential.access_token, "synthetic-old")
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.pool.idle_counts(), {})

    def test_one_mib_response_is_allowed_and_never_pooled(self):
        self.server.scenarios["/limit"] = {"chunked": True,
            "body": TOKEN_BODY + b" " * (1024 * 1024 - len(TOKEN_BODY))}
        credential, result, _elapsed = self.refresh("/limit", budget=2)
        self.assertTrue(result)
        self.assertEqual(credential.access_token, "synthetic-new")
        self.assertEqual(self.pool.stats["created"], 0)
        self.assertEqual(self.pool.idle_counts(), {})
        self.assertEqual(self.server.requests[0][3], "close")

    def test_unexpected_redirect_is_not_replayed(self):
        self.server.scenarios["/redirect"] = {"status": 307, "redirect": "/ready", "body": b""}
        self.server.scenarios["/ready"] = {}
        credential, result, _elapsed = self.refresh("/redirect", budget=1)
        self.assertFalse(result)
        self.assertEqual([row[0] for row in self.server.requests], ["/redirect"])
        self.assertEqual(credential.access_token, "synthetic-old")

    def test_explicit_account_http_proxy_is_retained(self):
        self.server.scenarios["/proxy"] = {}
        with mock.patch.object(accounts.urllib.request, "proxy_bypass", return_value=False):
            credential, result, _elapsed = self.refresh("/proxy", budget=1, proxy=self.url(""))
        self.assertTrue(result)
        self.assertEqual(credential.refresh_token, "synthetic-rotated")
        self.assertEqual(self.server.requests[0][1], self.url("/proxy"))

    def test_empty_account_proxy_retains_active_transport_exit(self):
        for keepalive in ("1", "0"):
            with self.subTest(keepalive=keepalive):
                path = "/route-" + keepalive
                self.server.scenarios[path] = {}
                start = len(self.server.requests)
                with mock.patch.dict(os.environ, {"WB_UPSTREAM_KEEPALIVE": keepalive,
                                                  "HTTP_PROXY": self.url(""),
                                                  "HTTPS_PROXY": self.url("")}), \
                        mock.patch.object(accounts.urllib.request, "proxy_bypass", return_value=False):
                    with accounts.urlopen(urllib.request.Request(self.url(path)), timeout=1, proxy="") as ordinary:
                        self.assertEqual(ordinary.read(), TOKEN_BODY)
                    credential, result, _elapsed = self.refresh(path, budget=1, proxy="")
                self.assertTrue(result)
                self.assertEqual(credential.access_token, "synthetic-new")
                routes = [row[1] for row in self.server.requests[start:]]
                self.assertEqual(len(routes), 2)
                expected = path if keepalive == "1" else self.url(path)
                self.assertEqual(routes, [expected, expected])

    def test_tls_handshake_after_proxy_connect_uses_original_deadline(self):
        # Measure the socket/TLS handshake phase. CA-store initialization is
        # shared and may be slow on Windows; the separate cold-context test
        # ensures it cannot lead to a late connection or credential send.
        accounts.wb_upstream_pool.shared_ssl_context()
        credential = account()
        credential.proxy = self.url("")
        cfg = dict(accounts.get_realm_config("intl"), chat_upstream="https://127.0.0.1:443")
        started = time.monotonic()
        with mock.patch.object(accounts, "get_realm_config", return_value=cfg), \
                mock.patch.object(accounts.urllib.request, "proxy_bypass", return_value=False):
            self.assertFalse(credential.refresh_after_401("synthetic-old", timeout=.15,
                                                         deadline_at=started + .15))
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(credential.access_token, "synthetic-old")
        self.assertEqual([row[0] for row in self.server.requests], ["CONNECT"])

    def test_eof_while_watchdog_is_paused_cannot_abort_next_pool_lease(self):
        at_eof, selected, eof_done, finish_abort = (threading.Event() for _ in range(4))
        self.addCleanup(finish_abort.set)
        self.server.scenarios["/race"] = {}
        self.server.scenarios["/after"] = {"body": b"independent next response"}
        original_close = http.client.HTTPResponse._close_conn
        selected_sockets, results = [], []

        def paused_close(response):
            if str(getattr(response, "url", "")).endswith("/race") and response.length == 0:
                at_eof.set()
                if not selected.wait(2):
                    raise AssertionError("watchdog did not select its socket")
                original_close(response)
                eof_done.set()
            else:
                original_close(response)

        def paused_abort(owner):
            with owner.lock:
                owner.expired = True
                sock = owner.sock
            selected_sockets.append(sock)
            selected.set()
            if not finish_abort.wait(2):
                return
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        with mock.patch.object(http.client.HTTPResponse, "_close_conn", paused_close), \
                mock.patch.object(accounts._RefreshDeadline, "abort", paused_abort):
            worker = threading.Thread(target=lambda: results.append(self.refresh("/race", budget=.08)))
            worker.start()
            try:
                self.assertTrue(at_eof.wait(2), "response did not reach natural EOF")
                self.assertTrue(selected.wait(2), "deadline callback did not run")
                self.assertTrue(eof_done.wait(2), "old response did not finish EOF")
                next_response = accounts.urlopen(urllib.request.Request(self.url("/after")), timeout=1)
                try:
                    self.assertIsNot(next_response._pool_conn.sock, selected_sockets[0])
                    finish_abort.set()
                    self.assertEqual(next_response.read(), b"independent next response")
                finally:
                    next_response.close()
            finally:
                finish_abort.set()
                worker.join(2)
            self.assertFalse(worker.is_alive(), "refresh did not join its watchdog")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][1])
        self.assertEqual(results[0][0].access_token, "synthetic-old")
        self.assertEqual(self.pool.stats["created"], 1)
        self.assertEqual(self.pool.stats["reused"], 0)
        self.assertNotEqual(self.server.requests[0][2], self.server.requests[1][2])


class DeadlineTLSTests(unittest.TestCase):
    stop_server = DeadlineSocketTests.stop_server
    refresh = DeadlineSocketTests.refresh

    def url(self, path):
        return "https://127.0.0.1:%d%s" % (self.server.server_port, path)

    def setUp(self):
        openssl = shutil.which("openssl")
        git_openssl = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/openssl.exe"
        if not openssl and git_openssl.is_file():
            openssl = str(git_openssl)
        if not openssl:
            self.skipTest("OpenSSL is needed only for the ephemeral loopback TLS certificate")
        self.certs = tempfile.TemporaryDirectory(prefix="wb-auth-tls-", dir=os.environ.get("WB_OPS_TEST_STATE"))
        self.addCleanup(self.certs.cleanup)
        cert, key = Path(self.certs.name) / "cert.pem", Path(self.certs.name) / "key.pem"
        subprocess.run([openssl, "req", "-new", "-x509", "-nodes", "-days", "1", "-newkey", "rsa:2048",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
                        "-out", str(cert), "-keyout", str(key)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=15)
        DeadlineSocketTests.setUp(self)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert), str(key))
        self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
        self.client_context = ssl.create_default_context(cafile=str(cert))

    def trust_loopback(self):
        self.enterContext(mock.patch.object(accounts.ssl, "_create_default_https_context",
                                           return_value=self.client_context))
        self.enterContext(mock.patch.object(accounts.http.client, "_create_https_context",
                                           return_value=self.client_context, create=True))

    def test_owned_tls_body_is_interrupted_without_token_update(self):
        self.trust_loopback()
        self.server.scenarios["/tls-drip"] = {"drip_body": True}
        credential, result, elapsed = self.refresh("/tls-drip", budget=.3)
        self.assertFalse(result)
        self.assertLess(elapsed, .7)
        self.assertEqual(credential.access_token, "synthetic-old")
        self.assertEqual(credential.refresh_token, "synthetic-refresh")
        self.assertEqual(len(self.server.requests), 1)

    def test_tls_uses_normal_certificate_verification(self):
        self.server.scenarios["/tls-ready"] = {}
        credential, result, _elapsed = self.refresh("/tls-ready", budget=1)
        self.assertFalse(result)
        self.assertEqual(credential.access_token, "synthetic-old")
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
