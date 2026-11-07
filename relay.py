"""Relay in front of Forge Neo for agents that reach this PC over Tailscale.

Listens on loopback and this node's Tailscale addresses only. Never binds
0.0.0.0. txt2img / img2img return image URLs and seeds. The JSON has no
prompt text. Saved files are the bytes Forge returned, metadata included.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
GEN_PATHS = {"/sdapi/v1/txt2img", "/sdapi/v1/img2img"}
TAILSCALE_V4 = ip_network("100.64.0.0/10")
TAILSCALE_V6 = ip_network("fd7a:115c:a1e0::/48")
PROMPT_FIELDS = (
    ("prompt", "positive", False),
    ("negative_prompt", "negative", False),
    ("hr_prompt", "positive", True),
    ("hr_negative_prompt", "negative", True),
)
DROP_REQUEST_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-type",
    "accept-encoding",
}
# Leave image payloads alone so a base64 blob is not damaged by prompt redaction.
IMAGE_KEYS = {"current_image", "image", "images", "init_images", "mask"}
MAX_BODY = 512 * 1024 * 1024
# shortcut: a generation longer than this is aborted. Raise it for huge batches.
UPSTREAM_TIMEOUT = 6 * 60 * 60
IMAGE_NAME = re.compile(r"[0-9a-f]{32}\.(png|jpg|webp|bin)")
MAGICDNS = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net")

# Rewritten prompts that differ from the agent's text. Scrubbed from JSON
# echoes. Not removed from saved image metadata.
secrets: deque[str] = deque(maxlen=32)
secrets_lock = threading.Lock()


class RelayServer4(ThreadingHTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True


class RelayServer6(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = True
    daemon_threads = True


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    server_cls = RelayServer6 if ip_address(host).version == 6 else RelayServer4
    try:
        return server_cls((host, port), RelayHandler)
    except OSError as e:
        raise SystemExit(f"failed to listen on {host}:{port}: {e}") from e


def assert_this_machine(host: str, own_tailscale: set[str], what: str) -> None:
    try:
        addr = ip_address(host)
    except ValueError:
        raise SystemExit(f"refusing {what} host {host}; use an IP on this machine") from None
    if addr.is_loopback or str(addr) in own_tailscale:
        return
    raise SystemExit(f"refusing {what} {host}; only loopback or this node's Tailscale addresses")


def parse_magicdns(name: str) -> str:
    cleaned = name.strip().rstrip(".").lower()
    if not MAGICDNS.fullmatch(cleaned):
        raise SystemExit(f"refusing image host {name!r}; expected this node's MagicDNS name")
    return cleaned


def parse_tailscale_ip(text: str, network) -> str:
    ip = text.strip().splitlines()[0].strip()
    try:
        addr = ip_address(ip)
    except ValueError:
        raise SystemExit(f"tailscale returned {ip!r}") from None
    if addr not in network:
        raise SystemExit(f"refusing Tailscale address {ip}")
    return str(addr)


def tailscale_identity() -> tuple[str, str | None, str]:
    try:
        ip4_raw = subprocess.check_output(["tailscale", "ip", "-4"], text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise SystemExit("tailscale ip -4 failed; not starting a listener") from e
    ip4 = parse_tailscale_ip(ip4_raw, TAILSCALE_V4)
    ip6 = None
    try:
        ip6_raw = subprocess.check_output(["tailscale", "ip", "-6"], text=True, timeout=10)
        ip6 = parse_tailscale_ip(ip6_raw, TAILSCALE_V6)
    except (OSError, subprocess.SubprocessError, SystemExit):
        ip6 = None
    host = ip4
    try:
        raw = subprocess.check_output(["tailscale", "status", "--json"], text=True, timeout=10)
        dns = json.loads(raw)["Self"]["DNSName"]
        host = parse_magicdns(dns)
    except (
        OSError,
        subprocess.SubprocessError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        SystemExit,
    ):
        host = ip4
    return ip4, ip6, host


def tidy_commas(text: str) -> str:
    text = re.sub(r"\s*,(?:\s*,)+\s*", ", ", text)
    text = re.sub(r"^\s*,\s*", "", text)
    text = re.sub(r"\s*,\s*$", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


SIDE_KEYS = ("replace", "delete", "prefix", "suffix")


def _string_list(value, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item != "" for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return list(value)


def _replace_list(value, label: str) -> list[tuple[re.Pattern[str], str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    compiled = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{i}] must be an object with pattern and text")
        pattern = item.get("pattern")
        text = item.get("text")
        if not isinstance(pattern, str) or pattern == "" or not isinstance(text, str):
            raise ValueError(f"{label}[{i}] needs a non-empty pattern and a text string")
        try:
            compiled.append((re.compile(pattern), text))
        except re.error as e:
            print(f"config error: {label}[{i}] {e}", flush=True)
            raise ValueError(f"{label}[{i}] pattern is invalid") from e
    return compiled


def parse_side(value, label: str) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - set(SIDE_KEYS)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")
    return {
        "replace": _replace_list(value.get("replace"), f"{label}.replace"),
        "delete": _string_list(value.get("delete"), f"{label}.delete"),
        "prefix": _string_list(value.get("prefix"), f"{label}.prefix"),
        "suffix": _string_list(value.get("suffix"), f"{label}.suffix"),
    }


def side_active(side: dict) -> bool:
    return any(side[key] for key in SIDE_KEYS)


def join_tags(text: str, tags: list[str], *, prefix: bool) -> str:
    extra = ", ".join(tags)
    if extra == "":
        return text
    if text.strip() == "":
        return extra
    if prefix:
        return f"{extra}, {text}"
    return f"{text}, {extra}"


def apply_rules(text: str, side: dict) -> str:
    for pattern, repl in side["replace"]:
        text = pattern.sub(repl, text)
    if side["delete"]:
        for find in side["delete"]:
            text = text.replace(find, "")
        text = tidy_commas(text)
    text = join_tags(text, side["prefix"], prefix=True)
    return join_tags(text, side["suffix"], prefix=False)


def parse_rules(data: dict) -> dict[str, dict]:
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return {
        "positive": parse_side(data.get("positive"), "positive"),
        "negative": parse_side(data.get("negative"), "negative"),
    }


def load_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"config error: {e}", flush=True)
        raise ValueError("prompt rules file is invalid") from e
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    image_dir = Path(str(data.get("image_dir", "images")))
    if not image_dir.is_absolute():
        image_dir = ROOT / image_dir
    return {
        "forge_port": int(data.get("forge_port", 7860)),
        "listen_port": int(data.get("listen_port", 7861)),
        "image_dir": image_dir,
        "image_ttl_hours": float(data.get("image_ttl_hours", 168)),
        "rules": parse_rules(data),
    }


def load_rules(path: Path) -> dict[str, dict]:
    try:
        return load_config(path)["rules"]
    except ValueError as e:
        print(f"config error: {e}", flush=True)
        raise ValueError("prompt rules file is invalid") from e


def rewrite_field(value, rules: dict, hr: bool):
    if isinstance(value, list):
        return [rewrite_field(item, rules, hr) for item in value]
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("prompt fields must be strings")
    if hr and value == "":
        return ""
    return apply_rules(value, rules)


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_strings(item))
        return found
    return []


def remember_secrets(before: dict, after: dict) -> None:
    fresh = []
    for field, _side, _hr in PROMPT_FIELDS:
        old = _strings(before.get(field))
        for item in _strings(after.get(field)):
            if item and item not in old:
                fresh.append(item)
    with secrets_lock:
        for item in fresh:
            if item not in secrets:
                secrets.append(item)


def rewrite_payload(payload: dict, rules: dict[str, dict]) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("generation body must be a JSON object")
    out = dict(payload)
    before = {field: out.get(field) for field, _side, _hr in PROMPT_FIELDS}
    for field, side, hr in PROMPT_FIELDS:
        if field not in out and not side_active(rules[side]):
            continue
        if hr and out.get(field) in (None, ""):
            continue
        out[field] = rewrite_field(out.get(field, ""), rules[side], hr)
    out["send_images"] = True
    remember_secrets(before, out)
    return out


def sniff_ext(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8"):
        return "jpg"
    if raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WEBP":
        return "webp"
    return "bin"


def align_seeds(count: int, info: dict, key: str) -> list:
    # shortcut: index_of_first_image covers a leading grid only.
    # return_mask inserts images between seeds; those slots stay wrong until that option is handled.
    aligned: list = [None] * count
    start = int(info.get("index_of_first_image") or 0)
    values = info.get(key) or []
    if not isinstance(values, list):
        return aligned
    for offset, value in enumerate(values):
        pos = start + offset
        if 0 <= pos < count and isinstance(value, int) and not isinstance(value, bool):
            aligned[pos] = value
    return aligned


def generation_result(
    upstream_body: bytes, base_url: str, image_dir: Path, ttl_seconds: float
) -> bytes:
    try:
        data = json.loads(upstream_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("upstream generation response was not JSON") from e
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list):
        raise ValueError("upstream images field is not a list")
    info = data.get("info")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except json.JSONDecodeError:
            info = {}
    if not isinstance(info, dict):
        info = {}
    sweep(image_dir, ttl_seconds)
    image_dir.mkdir(parents=True, exist_ok=True)
    urls = []
    for item in images:
        if not isinstance(item, str) or item == "":
            urls.append(None)
            continue
        blob = item.split(",", 1)[1] if item.startswith("data:") else item
        try:
            raw = base64.b64decode(blob, validate=False)
        except binascii.Error as e:
            raise ValueError("upstream image was not decodable") from e
        name = uuid.uuid4().hex + "." + sniff_ext(raw)
        (image_dir / name).write_bytes(raw)
        urls.append(f"{base_url}/images/{name}")
    result = {
        "images": urls,
        "seeds": align_seeds(len(urls), info, "all_seeds"),
        "subseeds": align_seeds(len(urls), info, "all_subseeds"),
    }
    return json.dumps(result).encode("utf-8")


def sweep(directory: Path, ttl_seconds: float) -> None:
    if ttl_seconds <= 0 or not directory.exists():
        return
    cutoff = time.time() - ttl_seconds
    for path in directory.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _secret_items() -> list[str]:
    with secrets_lock:
        return [item for item in secrets if item]


def redact_obj(obj, items: list[str], parent_key: str | None = None):
    if parent_key in IMAGE_KEYS:
        return obj
    if isinstance(obj, str):
        for item in items:
            obj = obj.replace(item, "")
        return obj
    if isinstance(obj, list):
        return [redact_obj(item, items, parent_key) for item in obj]
    if isinstance(obj, dict):
        return {key: redact_obj(value, items, key) for key, value in obj.items()}
    return obj


def maybe_redact(raw: bytes) -> bytes:
    items = _secret_items()
    if not items:
        return raw
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = raw.decode("utf-8", errors="ignore")
        if not any(item in text for item in items):
            return raw
        for item in items:
            text = text.replace(item, "")
        return text.encode("utf-8")
    redacted = redact_obj(data, items)
    if redacted == data:
        return raw
    return json.dumps(redacted, ensure_ascii=False).encode("utf-8")


def http_request_line(raw: bytes) -> bool:
    line = raw.split(b"\r\n", 1)[0].split(b"\n", 1)[0]
    if not line or len(line) > 8192:
        return False
    return all(32 <= byte <= 126 for byte in line)


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ""
    image_dir = ROOT / "images"
    base_url = ""
    config_path = CONFIG_PATH
    ttl_seconds = 168 * 3600

    def parse_request(self):
        raw = getattr(self, "raw_requestline", b"") or b""
        if raw and not http_request_line(raw):
            # BaseHTTPRequestHandler would decode this as Latin-1 and log it.
            self.close_connection = True
            self.requestline = ""
            if raw.startswith(b"\x16\x03"):
                note = "ignored TLS handshake on the HTTP port"
            else:
                note = "ignored non-HTTP request"
            print(f"{self.log_date_time_string()} {note}", flush=True)
            return False
        return super().parse_request()

    def log_message(self, format, *args):
        # Request line only. Bodies can contain prompts.
        text = format % args
        if any(ord(ch) > 126 or (ord(ch) < 32 and ch != "\t") for ch in text):
            text = "ignored non-HTTP request"
        print(f"{self.log_date_time_string()} {text}", flush=True)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/images/"):
            self.serve_image(path)
            return
        self.proxy()

    def do_POST(self):
        self.proxy()

    do_PUT = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST

    def serve_image(self, path: str) -> None:
        name = unquote(path.removeprefix("/images/"))
        if not IMAGE_NAME.fullmatch(name):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        file = self.image_dir / name
        if not file.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        kind = name.rsplit(".", 1)[1]
        content_type = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(
            kind, "application/octet-stream"
        )
        self._send(200, file.read_bytes(), content_type)

    def proxy(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._send(400, b'{"error":"bad content-length"}', "application/json")
            return
        if length < 0 or length > MAX_BODY:
            self._send(413, b'{"error":"body too large"}', "application/json")
            return
        body = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path
        # shortcut: extension generation routes are proxied as-is and can still return base64.
        # Add the path to GEN_PATHS when it needs the same treatment.
        is_gen = self.command == "POST" and path in GEN_PATHS
        if is_gen:
            try:
                payload = json.loads(body.decode("utf-8"))
                rewritten = rewrite_payload(payload, load_rules(self.config_path))
                body = json.dumps(rewritten, ensure_ascii=False).encode("utf-8")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, b'{"error":"generation body must be JSON"}', "application/json")
                return
            except ValueError as e:
                message = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(400, message, "application/json")
                return
        status, raw, content_type = self.forward(body)
        if status == 0:
            return
        if is_gen and status == 200:
            try:
                raw = generation_result(raw, self.base_url, self.image_dir, self.ttl_seconds)
                content_type = "application/json"
            except ValueError as e:
                message = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(502, message, "application/json")
                return
        raw = maybe_redact(raw)
        self._send(status, raw, content_type)

    def forward(self, body: bytes) -> tuple[int, bytes, str]:
        request = Request(self.upstream + self.path, data=body or None, method=self.command)
        for key, value in self.headers.items():
            if key.lower() not in DROP_REQUEST_HEADERS:
                request.add_header(key, value)
        if body:
            request.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
            request.add_header("Content-Length", str(len(body)))
        try:
            with urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                return response.status, response.read(), content_type
        except HTTPError as e:
            content_type = (
                e.headers.get("Content-Type", "application/octet-stream")
                if e.headers
                else "application/octet-stream"
            )
            return e.code, e.read(), content_type
        except URLError:
            self._send(502, b'{"error":"upstream unreachable"}', "application/json")
            return 0, b"", ""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def listen_hosts(ip4: str, ip6: str | None) -> list[str]:
    hosts = ["127.0.0.1", ip4]
    if ip6:
        hosts.append(ip6)
    return hosts


def main() -> None:
    settings = load_config(CONFIG_PATH)
    ip4, ip6, image_host = tailscale_identity()
    own = {ip4, ip6} if ip6 else {ip4}
    upstream = f"http://127.0.0.1:{settings['forge_port']}"
    port = settings["listen_port"]
    base_url = f"http://{image_host}:{port}"
    settings["image_dir"].mkdir(parents=True, exist_ok=True)
    RelayHandler.upstream = upstream
    RelayHandler.image_dir = settings["image_dir"]
    RelayHandler.base_url = base_url
    RelayHandler.config_path = CONFIG_PATH
    RelayHandler.ttl_seconds = settings["image_ttl_hours"] * 3600
    hosts = listen_hosts(ip4, ip6)
    for host in hosts:
        assert_this_machine(host, own, "listen")
    servers = []
    try:
        for host in hosts:
            servers.append(make_server(host, port))
    except SystemExit:
        for server in servers:
            server.server_close()
        raise
    print(f"loopback  http://127.0.0.1:{port}", flush=True)
    print(f"tailscale {base_url}", flush=True)
    print(f"ipv4      http://{ip4}:{port}", flush=True)
    if ip6:
        print(f"ipv6      http://[{ip6}]:{port}", flush=True)
    print(f"upstream  {upstream}", flush=True)
    print("listening on loopback and the relay host's Tailscale addresses only", flush=True)
    for server in servers[:-1]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        servers[-1].serve_forever()
    except KeyboardInterrupt:
        for server in servers:
            server.shutdown()


if __name__ == "__main__":
    main()
