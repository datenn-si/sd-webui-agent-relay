import base64
import contextlib
import http.client
import io
import json
import socket
import struct
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import relay

TEST_TAILSCALE_IPV4 = "100.64.0.1"
TEST_TAILSCALE_IPV6 = "fd7a:115c:a1e0::1"
TEST_MAGICDNS = "relay.example.ts.net"


def png_with_text(text: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    text_chunk = chunk(b"tEXt", b"parameters\x00" + text)
    iend = chunk(b"IEND", b"")
    return sig + ihdr + text_chunk + idat + iend


class RuleTests(unittest.TestCase):
    def setUp(self):
        relay.secrets.clear()

    def test_delete_add_replace_and_empty_hr(self):
        rules = relay.parse_rules(
            {
                "positive": {
                    "replace": [{"pattern": r"c.t", "text": "fox"}],
                    "delete": ["bad"],
                    "suffix": ["SECRET_ADDED_TAG"],
                },
                "negative": {"prefix": ["SECRET_NEG"]},
            }
        )
        out = relay.rewrite_payload(
            {"prompt": "a cat, bad, b", "negative_prompt": "blurry", "hr_prompt": "", "seed": -1},
            rules,
        )
        self.assertEqual(out["prompt"], "a fox, b, SECRET_ADDED_TAG")
        self.assertEqual(out["negative_prompt"], "SECRET_NEG, blurry")
        self.assertEqual(out["hr_prompt"], "")
        self.assertEqual(out["seed"], -1)
        self.assertTrue(out["send_images"])

    def test_list_prompts_and_missing_negative(self):
        rules = relay.parse_rules(
            {
                "positive": {"prefix": ["sharp", "crisp"]},
                "negative": {"suffix": ["blurry"]},
            }
        )
        out = relay.rewrite_payload({"prompt": ["cat", "dog"], "width": 512}, rules)
        self.assertEqual(out["prompt"], ["sharp, crisp, cat", "sharp, crisp, dog"])
        self.assertEqual(out["negative_prompt"], "blurry")
        self.assertEqual(out["width"], 512)

    def test_bad_key_and_regex_hide_rule_text(self):
        with self.assertRaises(ValueError) as caught:
            relay.parse_rules({"positive": {"nope": ["SECRET_RULE"]}})
        self.assertNotIn("SECRET_RULE", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            relay.parse_rules({"positive": {"replace": [{"pattern": "SECRET(", "text": "x"}]}})
        self.assertNotIn("SECRET", str(caught.exception))


class AddressTests(unittest.TestCase):
    def test_rejects_public_lan_and_wildcard(self):
        own = {TEST_TAILSCALE_IPV4}
        if TEST_TAILSCALE_IPV6:
            own.add(TEST_TAILSCALE_IPV6)
        for host in ("0.0.0.0", "203.0.113.1", "192.168.1.20", "fd00::1", "::"):
            with self.assertRaises(SystemExit):
                relay.assert_this_machine(host, own, "listen")
        relay.assert_this_machine("127.0.0.1", own, "listen")
        relay.assert_this_machine(TEST_TAILSCALE_IPV4, own, "listen")
        if TEST_TAILSCALE_IPV6:
            relay.assert_this_machine(TEST_TAILSCALE_IPV6, own, "listen")

    def test_magicdns_only(self):
        self.assertEqual(relay.parse_magicdns(f"{TEST_MAGICDNS.upper()}."), TEST_MAGICDNS.lower())
        for name in ("example.com", "relay.ts.net.evil.com", "funnel.example.ts.net.extra"):
            with self.assertRaises(SystemExit):
                relay.parse_magicdns(name)

    def test_listen_set_includes_tailscale(self):
        expected = ["127.0.0.1", TEST_TAILSCALE_IPV4]
        if TEST_TAILSCALE_IPV6:
            expected.append(TEST_TAILSCALE_IPV6)
        self.assertEqual(
            relay.listen_hosts(TEST_TAILSCALE_IPV4, TEST_TAILSCALE_IPV6),
            expected,
        )


class ConfigTests(unittest.TestCase):
    def test_resource_limit_defaults_and_validation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            config = relay.load_config(path)
            self.assertEqual(config["max_body_bytes"], 512 * 1024 * 1024)
            self.assertEqual(config["max_connections"], 16)
            self.assertEqual(config["max_generations"], 2)
            path.write_text('{"max_connections": 0}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "max_connections"):
                relay.load_config(path)


class FakeForge(BaseHTTPRequestHandler):
    last = {}
    slow_release = threading.Event()

    def log_message(self, format, *args):
        return

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/headers":
            self._send(
                202,
                b"upstream",
                "text/plain",
                [
                    ("X-Upstream", "preserved"),
                    ("Connection", "X-Hop"),
                    ("X-Hop", "removed"),
                    ("Keep-Alive", "timeout=5"),
                ],
            )
            return
        if path == "/api/error":
            self._send(
                418,
                b'{"error":"forge rejected"}',
                "application/json",
                [("X-Upstream", "error")],
            )
            return
        if path == "/api/slow":
            FakeForge.slow_release.wait(2)
        if path == "/sdapi/v1/samplers":
            body = b'[{"name":"Euler"}]'
        elif path == "/sdapi/v1/progress":
            body = json.dumps(
                {
                    "progress": 0.5,
                    "current_image": "abc",
                    "textinfo": FakeForge.last.get("prompt", ""),
                }
            ).encode()
        else:
            body = b"{}"
        self._send(200, body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        FakeForge.last = payload
        png = png_with_text(b"SECRET_ADDED_TAG")
        info = {
            "prompt": payload.get("prompt"),
            "all_prompts": [payload.get("prompt")],
            "negative_prompt": payload.get("negative_prompt"),
            "all_seeds": [42, 43],
            "all_subseeds": [7, 8],
            "index_of_first_image": 1,
            "infotexts": [payload.get("prompt")],
        }
        body = json.dumps(
            {
                "images": [
                    base64.b64encode(png).decode(),
                    base64.b64encode(png).decode(),
                    base64.b64encode(png).decode(),
                ],
                "parameters": payload,
                "info": json.dumps(info),
            }
        ).encode()
        self._send(200, body)

    def _send(self, status, body, content_type="application/json", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class RelayHttpTests(unittest.TestCase):
    def setUp(self):
        relay.secrets.clear()
        self.tmp = TemporaryDirectory()
        self.forge = ThreadingHTTPServer(("127.0.0.1", 0), FakeForge)
        self.forge_thread = threading.Thread(target=self.forge.serve_forever, daemon=True)
        self.forge_thread.start()
        forge_port = self.forge.server_address[1]
        self.config = Path(self.tmp.name) / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "positive": {"suffix": ["SECRET_ADDED_TAG"]},
                    "negative": {"prefix": ["SECRET_NEG"]},
                }
            ),
            encoding="utf-8",
        )
        self.image_dir = Path(self.tmp.name) / "images"
        relay.RelayHandler.upstream = f"http://127.0.0.1:{forge_port}"
        relay.RelayHandler.image_dir = self.image_dir
        relay.RelayHandler.base_url = f"http://{TEST_MAGICDNS}:7861"
        relay.RelayHandler.config_path = self.config
        relay.RelayHandler.ttl_seconds = 0
        relay.RelayHandler.max_body_bytes = relay.DEFAULT_MAX_BODY_BYTES
        relay.RelayHandler.generation_slots = threading.BoundedSemaphore(
            relay.DEFAULT_MAX_GENERATIONS
        )
        relay.RelayHandler.upstream_timeout = relay.UPSTREAM_TIMEOUT
        self.relay = relay.RelayServer4(("127.0.0.1", 0), relay.RelayHandler, max_connections=8)
        self.relay_thread = threading.Thread(target=self.relay.serve_forever, daemon=True)
        self.relay_thread.start()
        self.relay_port = self.relay.server_address[1]

    def tearDown(self):
        self.relay.shutdown()
        self.forge.shutdown()
        self.relay.server_close()
        self.forge.server_close()
        self.tmp.cleanup()
        relay.secrets.clear()

    def _post(self, path, payload):
        body = json.dumps(payload).encode()
        request = Request(
            f"http://127.0.0.1:{self.relay_port}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            return response.status, response.read()

    def test_generation_hides_prompt_keeps_metadata_and_seeds(self):
        status, raw = self._post(
            "/sdapi/v1/txt2img",
            {"prompt": "cat", "negative_prompt": "ugly", "seed": -1, "steps": 4},
        )
        self.assertEqual(status, 200)
        self.assertNotIn(b"SECRET_ADDED_TAG", raw)
        self.assertNotIn(b"SECRET_NEG", raw)
        self.assertNotIn(b"parameters", raw)
        body = json.loads(raw)
        self.assertEqual(body["seeds"], [None, 42, 43])
        self.assertEqual(body["subseeds"], [None, 7, 8])
        self.assertTrue(body["images"][1].startswith(f"http://{TEST_MAGICDNS}:7861/images/"))
        self.assertEqual(FakeForge.last["prompt"], "cat, SECRET_ADDED_TAG")
        self.assertEqual(FakeForge.last["negative_prompt"], "SECRET_NEG, ugly")
        self.assertEqual(FakeForge.last["steps"], 4)
        self.assertTrue(FakeForge.last["send_images"])
        name = body["images"][1].rsplit("/", 1)[1]
        saved = (self.image_dir / name).read_bytes()
        self.assertIn(b"SECRET_ADDED_TAG", saved)
        with urlopen(f"http://127.0.0.1:{self.relay_port}/images/{name}", timeout=5) as response:
            fetched = response.read()
            self.assertEqual(response.headers.get("Content-Type"), "image/png")
        self.assertEqual(fetched, saved)

    def test_binary_request_log_stays_ascii(self):
        buf = io.StringIO()
        payload = bytes([0x6E, 0x6A, 0xB4, 0x47, 0x20, 0x0E, 0x55, 0xCF, 0xA8])
        with contextlib.redirect_stdout(buf):
            with socket.create_connection(("127.0.0.1", self.relay_port), timeout=5) as sock:
                sock.sendall(payload)
                sock.shutdown(socket.SHUT_WR)
                sock.settimeout(2)
                sock.recv(64)
        log = buf.getvalue()
        self.assertIn("ignored non-HTTP request", log)
        self.assertNotIn("Bad request version", log)
        self.assertTrue(all(ord(ch) < 127 or ch in "\n\r" for ch in log))

    def test_other_routes_pass_through(self):
        with urlopen(
            f"http://127.0.0.1:{self.relay_port}/sdapi/v1/samplers", timeout=5
        ) as response:
            self.assertEqual(response.read(), b'[{"name":"Euler"}]')
        self._post("/sdapi/v1/txt2img", {"prompt": "cat", "negative_prompt": "ugly"})
        with urlopen(
            f"http://127.0.0.1:{self.relay_port}/sdapi/v1/progress", timeout=5
        ) as response:
            progress = json.loads(response.read())
        self.assertEqual(progress["current_image"], "abc")
        self.assertNotIn("SECRET_ADDED_TAG", progress["textinfo"])
        with urlopen(
            f"http://127.0.0.1:{self.relay_port}/sdapi/v1/samplers", timeout=5
        ) as response:
            self.assertEqual(response.read(), b'[{"name":"Euler"}]')

    def test_only_api_favicon_and_images_are_routed(self):
        for path in ("/", "/assets/index.js", "/file=sample.png"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                urlopen(f"http://127.0.0.1:{self.relay_port}{path}", timeout=5)
            self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self._post("/", {"prompt": "must not reach Forge"})
        self.assertEqual(caught.exception.code, 404)
        with urlopen(f"http://127.0.0.1:{self.relay_port}/favicon.ico", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"{}")

    def test_upstream_response_headers_and_errors(self):
        with urlopen(f"http://127.0.0.1:{self.relay_port}/api/headers", timeout=5) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(response.read(), b"upstream")
            self.assertEqual(response.headers.get("X-Upstream"), "preserved")
            self.assertIsNone(response.headers.get("X-Hop"))
            self.assertIsNone(response.headers.get("Keep-Alive"))
            self.assertEqual(response.headers.get("Content-Length"), "8")
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"http://127.0.0.1:{self.relay_port}/api/error", timeout=5)
        self.assertEqual(caught.exception.code, 418)
        self.assertEqual(caught.exception.read(), b'{"error":"forge rejected"}')
        self.assertEqual(caught.exception.headers.get("X-Upstream"), "error")

    def test_upstream_connection_failure_and_timeout(self):
        relay.RelayHandler.upstream = "http://127.0.0.1:1"
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"http://127.0.0.1:{self.relay_port}/sdapi/v1/samplers", timeout=5)
        self.assertEqual(caught.exception.code, 502)
        self.assertEqual(json.loads(caught.exception.read())["error"], "upstream unreachable")

        relay.RelayHandler.upstream = f"http://127.0.0.1:{self.forge.server_address[1]}"
        relay.RelayHandler.upstream_timeout = 0.05
        FakeForge.slow_release.clear()
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(f"http://127.0.0.1:{self.relay_port}/api/slow", timeout=5)
            self.assertEqual(caught.exception.code, 504)
            self.assertEqual(json.loads(caught.exception.read())["error"], "upstream timeout")
        finally:
            FakeForge.slow_release.set()

    def test_request_size_host_and_image_path_validation(self):
        relay.RelayHandler.max_body_bytes = 16
        request = Request(
            f"http://127.0.0.1:{self.relay_port}/sdapi/v1/samplers",
            data=b"x" * 17,
            headers={"Content-Type": "application/octet-stream"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 413)
        relay.RelayHandler.max_body_bytes = relay.DEFAULT_MAX_BODY_BYTES

        with socket.create_connection(("127.0.0.1", self.relay_port), timeout=5) as sock:
            sock.sendall(
                (
                    f"GET /sdapi/v1/samplers HTTP/1.1\r\n"
                    f"Host: attacker.invalid:{self.relay_port}\r\n\r\n"
                ).encode()
            )
            self.assertIn(b"400", sock.recv(512))

        with self.assertRaises(HTTPError) as caught:
            urlopen(f"http://127.0.0.1:{self.relay_port}/images/..%2fconfig.json", timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_generation_capacity_rejects_before_forwarding(self):
        slots = relay.RelayHandler.generation_slots
        acquired = [slots.acquire(blocking=False) for _ in range(relay.DEFAULT_MAX_GENERATIONS)]
        self.assertTrue(all(acquired))
        try:
            with self.assertRaises(HTTPError) as caught:
                self._post("/sdapi/v1/txt2img", {"prompt": "cat"})
            self.assertEqual(caught.exception.code, 503)
            self.assertEqual(
                json.loads(caught.exception.read())["error"], "generation capacity reached"
            )
        finally:
            for _ in acquired:
                slots.release()


class BlockingHandler(relay.RelayHandler):
    entered = threading.Event()
    release = threading.Event()

    def do_GET(self):
        if self.path == "/hold":
            self.entered.set()
            self.release.wait(5)
        self._send(200, b"ok", "text/plain")


class ConnectionLimitTests(unittest.TestCase):
    def _server(self, max_connections):
        BlockingHandler.entered.clear()
        BlockingHandler.release.clear()
        server = relay.RelayServer4(
            ("127.0.0.1", 0), BlockingHandler, max_connections=max_connections
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _hold_connection(self, port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/hold")
        response = connection.getresponse()
        response.read()
        connection.close()

    def test_limit_returns_503_and_available_slot_serves_request(self):
        server, thread = self._server(1)
        held = threading.Thread(
            target=self._hold_connection, args=(server.server_port,), daemon=True
        )
        try:
            held.start()
            self.assertTrue(BlockingHandler.entered.wait(2))
            rejected = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            rejected.request("GET", "/ok")
            response = rejected.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(response.headers.get("Connection"), "close")
            response.read()
            rejected.close()
        finally:
            BlockingHandler.release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

        server, thread = self._server(2)
        held = threading.Thread(
            target=self._hold_connection, args=(server.server_port,), daemon=True
        )
        try:
            held.start()
            self.assertTrue(BlockingHandler.entered.wait(2))
            available = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            available.request("GET", "/ok")
            response = available.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")
            available.close()
        finally:
            BlockingHandler.release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
