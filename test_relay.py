import base64
import contextlib
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


class FakeForge(BaseHTTPRequestHandler):
    last = {}

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/sdapi/v1/samplers":
            body = b'[{"name":"Euler"}]'
        elif self.path.split("?", 1)[0] == "/sdapi/v1/progress":
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

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
        self.relay = ThreadingHTTPServer(("127.0.0.1", 0), relay.RelayHandler)
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


if __name__ == "__main__":
    unittest.main()
