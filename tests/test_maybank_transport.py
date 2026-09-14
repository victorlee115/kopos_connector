"""Real loopback HTTPS through Requests/urllib3, never an external provider.

OpenSSL generates an ephemeral test certificate; no private key is checked in.
Run with pytest locally/CI, or stdlib unittest against the installed Bench wheel.
Only ERP configuration/cache/auth are substituted; sockets, TLS, redirect/retry
handling and transport-evidence capture are real.
"""

import base64
import gzip
import hashlib
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
import zlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, call, patch

import requests

try:
    import frappe
except ModuleNotFoundError as error:
    if error.name != "frappe":
        raise
    from tests.fake_frappe import install_fake_frappe_modules
    install_fake_frappe_modules()

from kopos_connector.services.maybank import client as maybank


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.server.received.append({
            "path": self.path, "method": self.command,
            "body": self.rfile.read(int(self.headers.get("Content-Length", "0"))),
            "authorization": self.headers.get("Authorization"),
        })
        status, headers, body = self.server.responses.popleft() if self.server.responses else (
            200, {}, b'{"status":"QR000","access_token":"synthetic"}')
        self.send_response(status)
        for key, value in headers.items():
            if value is not None:
                self.send_header(key, value)
        if "Content-Length" not in headers and "Transfer-Encoding" not in headers:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if headers.get("Transfer-Encoding") == "chunked":
                for offset in range(0, len(body), 4096):
                    chunk = body[offset:offset + 4096]
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
            # Expected when the client deliberately rejects an oversized body.
            return

    do_GET = do_POST

    def log_message(self, *_args):
        pass


class TestMaybankTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="kopos-test-tls-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cert = Path(cls.temporary.name) / "cert.pem"
        key = Path(cls.temporary.name) / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], check=True, capture_output=True, timeout=30)
        cls.cert = str(cert)
        cls.cert_hash = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.servers = []
        for secure in (True, True, False):
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            if secure:
                server.socket = context.wrap_socket(server.socket, server_side=True)
            server.received, server.responses = [], deque()
            server.origin = f"{'https' if secure else 'http'}://127.0.0.1:{server.server_port}"
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            cls.servers.append(server)
            cls.addClassCleanup(cls._stop_server, server, thread)

    @staticmethod
    def _stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "loopback server did not stop"

    def setUp(self):
        for server in self.servers:
            server.received.clear()
            server.responses.clear()
        self.config = patch.object(maybank.frappe, "conf", {"maybank_allowed_origins": [self.servers[0].origin]}, create=True)
        self.config.start()
        self.addCleanup(self.config.stop)

    def client(self, user_type="merchant"):
        client = maybank.MaybankClient(
            username="synthetic-merchant", encrypted_pin="1234", user_type=user_type,
            outlet_id="SYNTHETIC-OUTLET", base_url=self.servers[0].origin + "/api/",
            provider_device_id="0123456789abcdef0123456789abcdef",
            provider_device_name=maybank.DEFAULT_DEVICE_NAME, provider_device_os=maybank.DEFAULT_DEVICE_OS,
        )
        self.addCleanup(client.session.close)
        # Trust only this test's certificate. No verify=False, external DNS or proxy.
        client.session.verify = self.cert
        client.session.trust_env = False
        client._jwt_cache_key = Mock(return_value="synthetic-jwt-key")
        client._outlet_token_cache_key = Mock(return_value="synthetic-outlet-key")
        client._cache_get = Mock(return_value="")
        client._cache_set = Mock()
        client._clear_auth_cache = Mock()
        return client

    def test_redirects_never_replay_provider_requests(self):
        destinations = ["/redirected", self.servers[1].origin + "/redirected", self.servers[2].origin + "/redirected"]
        for status in (301, 302, 303, 307, 308):
            for destination in destinations:
                for operation in ("login", "outlet", "create", "status", "evidence"):
                    with self.subTest(status=status, destination=destination, operation=operation):
                        for server in self.servers:
                            server.received.clear()
                        client = self.client("corporate")
                        self.servers[0].responses.append((status, {"Location": destination}, b"redirect"))
                        if operation != "login":
                            client._get_jwt = Mock(return_value="synthetic-jwt")
                        if operation in ("status", "evidence"):
                            client._get_outlet_token = Mock(return_value="synthetic-outlet-token")
                        actions = {
                            "login": client._get_jwt, "outlet": client._get_outlet_token,
                            "create": lambda: client.generate_qr("10.00"),
                            "status": lambda: client.check_status("SYNTHETIC-REF"),
                            "evidence": lambda: client.check_status_with_transport_evidence("SYNTHETIC-REF"),
                        }
                        with self.assertRaises(requests.TooManyRedirects):
                            actions[operation]()
                        self.assertEqual([len(s.received) for s in self.servers], [1, 0, 0])
                        client._cache_set.assert_not_called()
                        client.session.close()

    def test_status_and_evidence_refresh_once_and_bind_exact_https_exchange(self):
        body = b'{"status":"QR000","data":[{"transaction_refno":"SYNTHETIC-REF"}]}'
        for method in ("check_status", "check_status_with_transport_evidence"):
            for user_type in ("cashier", "merchant", "corporate"):
                with self.subTest(method=method, user_type=user_type):
                    self.servers[0].received.clear()
                    self.servers[0].responses.extend([(401, {}, b"unauthorized"), (200, {}, body)])
                    client = self.client(user_type)
                    client._get_outlet_token = Mock(side_effect=["synthetic-stale", "synthetic-refreshed"])
                    result = getattr(client, method)("SYNTHETIC-REF")
                    requests_seen = self.servers[0].received
                    self.assertEqual(len(requests_seen), 2)
                    endpoint = "cashier" if user_type == "cashier" else "merchant"
                    self.assertEqual([row["path"] for row in requests_seen], [f"/api/v1/mobile/{endpoint}/transactionById"] * 2)
                    self.assertEqual([row["method"] for row in requests_seen], ["POST", "POST"])
                    self.assertEqual([json.loads(row["body"]) for row in requests_seen], [{"transaction_refno": "SYNTHETIC-REF"}] * 2)
                    self.assertEqual([row["authorization"] for row in requests_seen], ["Bearer synthetic-stale", "Bearer synthetic-refreshed"])
                    self.assertEqual(client._get_outlet_token.call_args_list, [call(), call(force_refresh=True)])
                    client._clear_auth_cache.assert_called_once_with()
                    if method.endswith("transport_evidence"):
                        result, evidence = result
                        self.assertEqual(evidence["request_body_sha256"], hashlib.sha256(requests_seen[-1]["body"]).hexdigest())
                        self.assertEqual(evidence["response_body_sha256"], hashlib.sha256(body).hexdigest())
                        self.assertEqual(evidence["tls_peer_certificate_sha256"], self.cert_hash)
                        self.assertIs(evidence["tls_verified"], True)
                    self.assertEqual(result, json.loads(body))
                    client.session.close()

    def test_status_unauthorized_retries_only_once(self):
        for method in ("check_status", "check_status_with_transport_evidence"):
            with self.subTest(method=method):
                self.servers[0].received.clear()
                self.servers[0].responses.extend([(401, {}, b"unauthorized")] * 2)
                client = self.client()
                client._get_outlet_token = Mock(return_value="synthetic-token")
                with self.assertRaises(requests.HTTPError):
                    getattr(client, method)("SYNTHETIC-REF")
                self.assertEqual(len(self.servers[0].received), 2)
                client._clear_auth_cache.assert_called_once_with()
                client.session.close()

    def test_qr_creation_does_not_replay_401_or_server_errors(self):
        for status in (401, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.servers[0].received.clear()
                self.servers[0].responses.append((status, {}, b"provider failure"))
                client = self.client()
                client._get_jwt = Mock(return_value="synthetic-token")
                with self.assertRaises(requests.HTTPError):
                    client.generate_qr("10.00")
                self.assertEqual(len(self.servers[0].received), 1)
                client.session.close()

    def test_status_server_errors_do_not_replay_post(self):
        for method in ("check_status", "check_status_with_transport_evidence"):
            for status in (500, 502, 503, 504):
                with self.subTest(method=method, status=status):
                    self.servers[0].received.clear()
                    self.servers[0].responses.append((status, {}, b"provider failure"))
                    client = self.client()
                    client._get_outlet_token = Mock(return_value="synthetic-token")
                    with self.assertRaises(requests.HTTPError):
                        getattr(client, method)("SYNTHETIC-REF")
                    self.assertEqual(len(self.servers[0].received), 1)
                    client.session.close()

    def test_next_request_recovers_after_redirect(self):
        self.servers[0].responses.extend([(307, {"Location": self.servers[1].origin + "/forbidden"}, b"redirect"), (200, {}, b'{"status":"QR000","access_token":"synthetic-recovered"}')])
        client = self.client()
        with self.assertRaises(requests.TooManyRedirects):
            client._get_jwt()
        self.assertEqual(client._get_jwt(), "synthetic-recovered")
        self.assertEqual([len(s.received) for s in self.servers], [2, 0, 0])

    def test_auth_refresh_does_not_follow_redirects(self):
        for method in ("check_status", "check_status_with_transport_evidence", "_get_outlet_token"):
            with self.subTest(method=method):
                for server in self.servers:
                    server.received.clear()
                self.servers[0].responses.extend([(401, {}, b"unauthorized"), (307, {"Location": self.servers[1].origin + "/forbidden"}, b"redirect")])
                client = self.client("corporate")
                client._get_jwt = Mock(return_value="synthetic-jwt")
                if method != "_get_outlet_token":
                    client._get_outlet_token = Mock(return_value="synthetic-outlet-token")
                with self.assertRaises(requests.TooManyRedirects):
                    getattr(client, method)(*([] if method == "_get_outlet_token" else ["SYNTHETIC-REF"]))
                self.assertEqual([len(s.received) for s in self.servers], [2, 0, 0])
                client._cache_set.assert_not_called()
                client.session.close()

    def test_invalid_login_payload_is_not_cached_or_echoed_and_recovers(self):
        tokens = [None, False, 123, 1.5, [], {}, "", " ", "\t", "a\nb", "x\r\nInjected: value",
            "x\x00", "é", "x:y", "=x", "x=y", "x\n"]
        payloads = [None, [], 1, True, "SENSITIVE-PROVIDER-TEXT", {},
            {"status": "QR999", "text": "SENSITIVE-PROVIDER-TEXT"}, {"status": "QR000"}]
        payloads += [{"status": "QR000", "access_token": token} for token in tokens]
        bodies = [json.dumps(payload).encode() for payload in payloads] + [b"SENSITIVE-PROVIDER-TEXT {", b""]
        for index, body in enumerate(bodies):
            with self.subTest(index=index):
                self.servers[0].received.clear()
                self.servers[0].responses.clear()
                self.servers[0].responses.extend([(200, {}, body),
                    (200, {}, b'{"status":"QR000","access_token":"synthetic-recovered"}')])
                client = self.client()
                with self.assertRaisesRegex(maybank.frappe.ValidationError, "Maybank login") as raised:
                    client._get_jwt()
                self.assertNotIn("SENSITIVE-PROVIDER-TEXT", str(raised.exception))
                self.assertIsNone(raised.exception.__context__)
                client._cache_set.assert_not_called()
                self.assertEqual(len(self.servers[0].received), 1)
                self.assertEqual(client._get_jwt(), "synthetic-recovered")
                self.assertEqual(len(self.servers[0].received), 2)
                client.session.close()

    @staticmethod
    def _outlet_response(token):
        return json.dumps({"status": "QR000", "data": [{"outletaccesstoken": {"access_token": token}}]}).encode()

    @staticmethod
    def _encrypt_outlet_token(plaintext):
        # Independent OpenSSL encoder; CryptoJS-compatible MD5 KDF and salt.
        result = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-md", "md5",
            "-S", "0102030405060708", "-pass", "pass:synthetic-provider-passphrase"],
            input=plaintext.encode(), capture_output=True, check=True, timeout=10)
        raw = result.stdout
        if not raw.startswith(b"Salted__"):
            raw = b"Salted__" + bytes.fromhex("0102030405060708") + raw
        return base64.b64encode(raw).decode()

    def test_invalid_outlet_payload_is_not_cached_or_echoed_and_recovers(self):
        payloads = [None, [], 1, True, "SENSITIVE-PROVIDER-TEXT", {},
            {"status": "QR999", "text": "SENSITIVE-PROVIDER-TEXT"}, {"status": "QR000"}]
        payloads += [{"status": "QR000", "data": data} for data in
            (None, [], {}, "x", [None], [1], [[]], [{}], [{"outletaccesstoken": None}],
             [{"outletaccesstoken": []}], [{"outletaccesstoken": "x"}], [{"outletaccesstoken": {}}])]
        bodies = [json.dumps(payload).encode() for payload in payloads]
        bodies += [self._outlet_response(token) for token in (None, False, 123, [], {}, "", " ", "SENSITIVE-PROVIDER-TEXT")]
        bodies += [b"SENSITIVE-PROVIDER-TEXT {", b""]
        valid = self._outlet_response(self._encrypt_outlet_token("synthetic-recovered"))
        for index, body in enumerate(bodies):
            with self.subTest(index=index):
                self.servers[0].received.clear()
                self.servers[0].responses.clear()
                self.servers[0].responses.extend([(200, {}, body), (200, {}, valid)])
                client = self.client("corporate")
                client._get_jwt = Mock(return_value="synthetic-provider-passphrase")
                with self.assertRaisesRegex(maybank.frappe.ValidationError, "Maybank outlet token") as raised:
                    client._get_outlet_token()
                self.assertNotIn("SENSITIVE-PROVIDER-TEXT", str(raised.exception))
                self.assertIsNone(raised.exception.__context__)
                client._cache_set.assert_not_called()
                self.assertEqual(len(self.servers[0].received), 1)
                self.assertEqual(client._get_outlet_token(), "synthetic-recovered")
                self.assertEqual(len(self.servers[0].received), 2)
                client.session.close()

    def test_decrypted_outlet_tokens_must_be_bearer_tokens(self):
        for token in ("", " ", "\t", "a\nb", "x\r\nInjected: value", "x\x00", "é", "x:y", "=x", "x=y", "x\n"):
            with self.subTest(token=repr(token)):
                self.servers[0].received.clear()
                self.servers[0].responses.append((200, {}, self._outlet_response(self._encrypt_outlet_token(token))))
                client = self.client("corporate")
                client._get_jwt = Mock(return_value="synthetic-provider-passphrase")
                with self.assertRaisesRegex(maybank.frappe.ValidationError, "Maybank outlet token"):
                    client._get_outlet_token()
                client._cache_set.assert_not_called()
                self.assertEqual(len(self.servers[0].received), 1)
                client.session.close()

    def test_valid_opaque_bearer_tokens_and_additive_fields_are_preserved(self):
        for validator in maybank._AUTH_RESPONSE_VALIDATORS.values():
            validator.check_schema(validator.schema)
        for token in ("x", "A-._~+/012==", "eyJhbGciOiJub25lIn0.e30.signature"):
            for operation in ("login", "outlet"):
                with self.subTest(token=token, operation=operation):
                    client = self.client("corporate")
                    if operation == "login":
                        body = json.dumps({"status": "QR000", "access_token": token, "future": {"x": 1}}).encode()
                        action, key = client._get_jwt, client._jwt_cache_key()
                    else:
                        # Preserve supported base64 line wrapping and first-item semantics.
                        encrypted = self._encrypt_outlet_token(token)
                        body = self._outlet_response(encrypted[:20] + "\n" + encrypted[20:])
                        payload = json.loads(body)
                        payload["data"].append({"future": "ignored"})
                        body = json.dumps(payload).encode()
                        client._get_jwt = Mock(return_value="synthetic-provider-passphrase")
                        action, key = client._get_outlet_token, client._outlet_token_cache_key()
                    self.servers[0].responses.append((200, {}, body))
                    self.assertEqual(action(), token)
                    client._cache_set.assert_called_once_with(key, token, 3540)
                    client.session.close()

    def _operation(self, operation):
        client = self.client("corporate")
        if operation != "login":
            client._get_jwt = Mock(return_value="synthetic-provider-passphrase")
        if operation in ("status", "evidence"):
            client._get_outlet_token = Mock(return_value="synthetic-outlet-token")
        action = {
            "login": client._get_jwt, "outlet": client._get_outlet_token,
            "create": lambda: client.generate_qr("10.00"),
            "status": lambda: client.check_status("SYNTHETIC-REF"),
            "evidence": lambda: client.check_status_with_transport_evidence("SYNTHETIC-REF"),
        }[operation]
        return client, action

    def test_oversized_responses_are_rejected_for_every_operation_and_framing(self):
        limit = 1_048_576
        payload = {"status": "QR000", "access_token": "synthetic-valid",
                   "data": [{"outletaccesstoken": {"access_token": self._encrypt_outlet_token("synthetic-valid")}}],
                   "future": "x" * limit}
        plain = json.dumps(payload).encode()
        variants = [({}, plain), ({"Transfer-Encoding": "chunked"}, plain),
                    ({"Content-Length": None, "Connection": "close"}, plain),
                    ({"Content-Encoding": "gzip"}, gzip.compress(plain)),
                    ({"Content-Encoding": "deflate", "Transfer-Encoding": "chunked"}, zlib.compress(plain))]
        for operation in ("login", "outlet", "create", "status", "evidence"):
            for index, (headers, body) in enumerate(variants):
                with self.subTest(operation=operation, framing=index):
                    self.servers[0].received.clear()
                    self.servers[0].responses.clear()
                    self.servers[0].responses.append((200, headers, body))
                    client, action = self._operation(operation)
                    with self.assertRaisesRegex(requests.RequestException, "Maybank.*response.*limit"):
                        action()
                    self.assertEqual(len(self.servers[0].received), 1)
                    client._cache_set.assert_not_called()
                    client.session.close()

    def test_supported_compression_preserves_decoded_evidence_bytes(self):
        body = b'{ "status": "QR000", "data": [{"transaction_refno":"SYNTHETIC-REF"}], "note":"caf\xc3\xa9" }\n'
        for headers, encoded in [({}, body), ({"Content-Encoding": "gzip"}, gzip.compress(body)),
                                 ({"Content-Encoding": "deflate", "Transfer-Encoding": "chunked"}, zlib.compress(body))]:
            with self.subTest(headers=headers):
                self.servers[0].responses.append((200, headers, encoded))
                client, action = self._operation("evidence")
                result, evidence = action()
                self.assertEqual(result, json.loads(body))
                self.assertEqual(evidence["response_body_sha256"], hashlib.sha256(body).hexdigest())
                self.assertEqual(evidence["tls_peer_certificate_sha256"], self.cert_hash)
                self.assertIs(evidence["tls_verified"], True)
                client.session.close()

    def test_invalid_json_is_fixed_error_for_every_operation(self):
        bodies = [b"SENSITIVE-PROVIDER-TEXT {", b"[]", b"null", b""]
        for operation in ("login", "outlet", "create", "status", "evidence"):
            for index, body in enumerate(bodies):
                with self.subTest(operation=operation, invalid=index):
                    self.servers[0].received.clear()
                    self.servers[0].responses.clear()
                    self.servers[0].responses.append((200, {}, body))
                    client, action = self._operation(operation)
                    with self.assertRaisesRegex(maybank.frappe.ValidationError, "Maybank.*response.*invalid") as raised:
                        action()
                    self.assertIsNone(raised.exception.__context__)
                    self.assertNotIn("SENSITIVE-PROVIDER-TEXT", str(raised.exception))
                    self.assertEqual(len(self.servers[0].received), 1)
                    client._cache_set.assert_not_called()
                    client.session.close()

    def test_parser_recursion_failure_is_fixed_error_for_every_operation(self):
        # The actual nesting threshold differs between CPython versions and
        # stacks. Inject the parser failure, not an arbitrary depth policy.
        for operation in ("login", "outlet", "create", "status", "evidence"):
            with self.subTest(operation=operation):
                self.servers[0].received.clear()
                self.servers[0].responses.append((200, {}, b'{"status":"QR000"}'))
                client, action = self._operation(operation)
                with patch.object(requests.Response, "json", side_effect=RecursionError("SENSITIVE-PROVIDER-TEXT")):
                    with self.assertRaisesRegex(maybank.frappe.ValidationError, "Maybank.*response.*invalid") as raised:
                        action()
                self.assertIsNone(raised.exception.__context__)
                self.assertNotIn("SENSITIVE-PROVIDER-TEXT", str(raised.exception))
                self.assertEqual(len(self.servers[0].received), 1)
                client._cache_set.assert_not_called()
                client.session.close()

    def test_deep_json_preserves_the_installed_parser_limit(self):
        body = b'{"future":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
        reference = requests.Response()
        reference._content = body
        try:
            reference.json()
            accepted = True
        except RecursionError:
            accepted = False
        self.servers[0].responses.append((200, {}, body))
        client, action = self._operation("status")
        if accepted:
            self.assertIsInstance(action()["future"], list)
        else:
            with self.assertRaisesRegex(maybank.frappe.ValidationError, "Maybank.*response.*invalid"):
                action()
        self.assertEqual(len(self.servers[0].received), 1)

    def test_wire_and_decoded_limits_close_the_real_response_and_next_request_recovers(self):
        limit = maybank.MAX_PROVIDER_RESPONSE_BYTES
        plain = b'{"future":"' + b"x" * (limit * 8) + b'"}'
        variants = [gzip.compress(plain), gzip.compress(b"") * (limit // 20 + 1)]
        native_send = maybank.HTTPAdapter.send
        for index, body in enumerate(variants):
            with self.subTest(limit_kind=index):
                self.servers[0].received.clear()
                self.servers[0].responses.extend([(200, {"Content-Encoding": "gzip"}, body),
                                                (200, {}, b'{"status":"QR000"}')])
                client, action = self._operation("status")
                responses = []
                def observe(adapter, *args, **kwargs):
                    response = native_send(adapter, *args, **kwargs)
                    responses.append(response)
                    return response
                with patch.object(maybank.HTTPAdapter, "send", observe):
                    with self.assertRaisesRegex(requests.RequestException, "response exceeds byte limit") as raised:
                        action()
                self.assertIsNone(raised.exception.__context__)
                self.assertEqual(len(responses), 1)
                self.assertTrue(responses[0].raw.closed)
                self.assertLessEqual(responses[0].raw.tell(), limit + 1)
                self.assertIs(responses[0]._content, False, "Do not retain a partial accepted response")
                self.assertEqual(len(self.servers[0].received), 1)
                self.assertEqual(action(), {"status": "QR000"})
                self.assertEqual(len(self.servers[0].received), 2)
                client.session.close()

    def test_exact_byte_boundary_is_accepted_without_truncating_json(self):
        limit = maybank.MAX_PROVIDER_RESPONSE_BYTES
        for length in (limit - 1, limit):
            for compressed in (False, True):
                with self.subTest(length=length, compressed=compressed):
                    body = b'{"x":"' + b"a" * (length - 8) + b'"}'
                    self.assertEqual(len(body), length)
                    headers, encoded = ({"Content-Encoding": "gzip"}, gzip.compress(body)) if compressed else ({}, body)
                    self.servers[0].responses.append((200, headers, encoded))
                    client = self.client()
                    response = client.session.post(client.base_url + "bounded", timeout=2)
                    self.assertEqual(response.content, body)
                    self.assertTrue(response.raw.closed)
                    client.session.close()

    def test_bad_compression_and_truncated_framing_fail_without_raw_exception_context(self):
        layered = b'{"status":"QR000"}'
        for _ in range(6):
            layered = gzip.compress(layered)
        variants = [({"Content-Encoding": "gzip"}, b"SENSITIVE-PROVIDER-TEXT"),
                    ({"Content-Encoding": ", ".join(["gzip"] * 6)}, layered),
                    ({"Content-Length": "1000", "Connection": "close"}, b'{"status":"QR000","text":"SENSITIVE-PROVIDER-TEXT"}')]
        for index, (headers, body) in enumerate(variants):
            with self.subTest(invalid=index):
                self.servers[0].received.clear()
                self.servers[0].responses.append((200, headers, body))
                client, action = self._operation("status")
                with self.assertRaisesRegex(requests.RequestException, "Maybank response could not be read") as raised:
                    action()
                self.assertIsNone(raised.exception.__context__)
                self.assertNotIn("SENSITIVE-PROVIDER-TEXT", str(raised.exception))
                self.assertEqual(len(self.servers[0].received), 1)
                client.session.close()

    def test_oversized_401_is_not_an_authority_to_refresh_or_replay(self):
        body = b"x" * (maybank.MAX_PROVIDER_RESPONSE_BYTES + 1)
        for operation in ("outlet", "create", "status", "evidence"):
            with self.subTest(operation=operation):
                self.servers[0].received.clear()
                self.servers[0].responses.append((401, {}, body))
                client, action = self._operation(operation)
                with self.assertRaisesRegex(requests.RequestException, "response exceeds byte limit"):
                    action()
                self.assertEqual(len(self.servers[0].received), 1)
                client._clear_auth_cache.assert_not_called()
                client._cache_set.assert_not_called()
                client.session.close()


if __name__ == "__main__":
    unittest.main()
