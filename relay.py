"""Relay in front of Forge Neo for agents that reach this PC over Tailscale.

Listens on loopback and this node's Tailscale addresses only. Never binds
0.0.0.0. txt2img / img2img return image URLs and seeds. The JSON has no
prompt text. Saved files are the bytes Forge returned, metadata included.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import re
import shutil
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
from urllib.parse import unquote, urlparse, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
GEN_PATHS = {"/sdapi/v1/txt2img", "/sdapi/v1/img2img"}
API_PREFIXES = ("/sdapi/", "/api/", "/internal/")
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
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
DEFAULT_MAX_BODY_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_CONNECTIONS = 16
DEFAULT_MAX_GENERATIONS = 2
REQUEST_TIMEOUT = 30
# Leave image payloads alone so a base64 blob is not damaged by prompt redaction.
IMAGE_KEYS = {"current_image", "image", "images", "init_images", "mask"}
# shortcut: a generation longer than this is aborted. Raise it for huge batches.
UPSTREAM_TIMEOUT = 6 * 60 * 60
IMAGE_NAME = re.compile(r"[0-9a-f]{32}\.(png|jpg|webp|bin)")
MAGICDNS = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net")

# Rewritten prompts that differ from the agent's text. Scrubbed from JSON
# echoes. Not removed from saved image metadata.
secrets: deque[str] = deque(maxlen=32)
secrets_lock = threading.Lock()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        request_handler_class,
        max_connections=DEFAULT_MAX_CONNECTIONS,
        connection_slots=None,
    ):
        self.connection_slots = connection_slots or threading.BoundedSemaphore(max_connections)
        super().__init__(server_address, request_handler_class)

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self._discard_headers(request)
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 0\r\n"
                    b"Retry-After: 1\r\n\r\n"
                )
            except OSError:
                pass
            self.close_request(request)
            return
        try:
            thread = threading.Thread(
                target=self.process_request_thread,
                args=(request, client_address),
                daemon=True,
            )
            thread.start()
        except Exception:
            self.connection_slots.release()
            self.handle_error(request, client_address)
            self.close_request(request)

    @staticmethod
    def _discard_headers(request):
        request.settimeout(0.1)
        data = bytearray()
        while len(data) < 65536 and b"\r\n\r\n" not in data:
            try:
                chunk = request.recv(min(4096, 65536 - len(data)))
            except (OSError, TimeoutError):
                break
            if not chunk:
                break
            data.extend(chunk)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


class RelayServer4(BoundedThreadingHTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True


class RelayServer6(BoundedThreadingHTTPServer):
    address_family = socket.AF_INET6
    allow_reuse_address = True
    daemon_threads = True


def make_server(
    host: str,
    port: int,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    connection_slots=None,
) -> ThreadingHTTPServer:
    server_cls = RelayServer6 if ip_address(host).version == 6 else RelayServer4
    try:
        return server_cls(
            (host, port),
            RelayHandler,
            max_connections=max_connections,
            connection_slots=connection_slots,
        )
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


def hostname_for_host(host: str) -> str:
    try:
        return str(ip_address(host)).lower()
    except ValueError:
        return host.lower().rstrip(".")


def get_header(headers: list[tuple[str, str]], name: str) -> str | None:
    return next((value for key, value in headers if key.lower() == name.lower()), None)


def response_headers(
    headers: list[tuple[str, str]], body_changed: bool = False
) -> list[tuple[str, str]]:
    connection_tokens = {
        token.strip().lower()
        for key, value in headers
        if key.lower() == "connection"
        for token in value.split(",")
    }
    excluded = (
        HOP_BY_HOP_HEADERS
        | connection_tokens
        | {
            "content-length",
            "content-type",
            "cache-control",
            "date",
            "server",
            "x-content-type-options",
        }
    )
    if body_changed:
        excluded.update(
            {"content-encoding", "content-md5", "digest", "etag", "content-range", "last-modified"}
        )
    return [(key, value) for key, value in headers if key.lower() not in excluded]


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
    limits = {
        "max_body_bytes": data.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES),
        "max_connections": data.get("max_connections", DEFAULT_MAX_CONNECTIONS),
        "max_generations": data.get("max_generations", DEFAULT_MAX_GENERATIONS),
    }
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} must be a positive integer")
    return {
        "forge_port": int(data.get("forge_port", 7860)),
        "listen_port": int(data.get("listen_port", 7861)),
        "image_dir": image_dir,
        "image_ttl_hours": float(data.get("image_ttl_hours", 168)),
        **limits,
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
    max_body_bytes = DEFAULT_MAX_BODY_BYTES
    generation_slots = threading.BoundedSemaphore(DEFAULT_MAX_GENERATIONS)
    upstream_timeout = UPSTREAM_TIMEOUT

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT)

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
        if not super().parse_request():
            return False
        try:
            target = urlsplit(self.path)
        except ValueError:
            target = None
        if (
            target is None
            or target.scheme
            or target.netloc
            or not (target.path.startswith("/") or (self.command == "OPTIONS" and self.path == "*"))
        ):
            self._bad_request()
            return False
        if self.request_version == "HTTP/1.1" and not self._valid_host():
            self._bad_request()
            return False
        return True

    def _valid_host(self) -> bool:
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return False
        try:
            parsed = urlsplit("//" + values[0])
            hostname = parsed.hostname
            port = parsed.port if parsed.port is not None else 80
        except ValueError:
            return False
        if (
            not hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        if port != self.server.server_port:
            return False
        allowed = {hostname_for_host(self.connection.getsockname()[0])}
        advertised = urlparse(self.base_url).hostname
        if advertised:
            allowed.add(advertised.lower().rstrip("."))
        return hostname.lower().rstrip(".") in allowed

    def _bad_request(self) -> None:
        self.close_connection = True
        self._send(400, b'{"error":"invalid request"}', "application/json")

    def log_message(self, format, *args):
        print(f"{self.log_date_time_string()} HTTP request handled", flush=True)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/images/"):
            self.serve_image(path)
            return
        self.proxy_if_allowed()

    def do_POST(self):
        self.proxy_if_allowed()

    do_PUT = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST

    def proxy_if_allowed(self) -> None:
        path = urlparse(self.path).path
        if path == "/favicon.ico" and self.command == "GET":
            self.proxy()
        elif path.startswith(API_PREFIXES):
            self.proxy()
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def serve_image(self, path: str) -> None:
        name = unquote(path.removeprefix("/images/"))
        if not IMAGE_NAME.fullmatch(name):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            file = (self.image_dir / name).resolve(strict=True)
            if file.parent != self.image_dir.resolve() or not file.is_file():
                raise FileNotFoundError
            size = file.stat().st_size
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        kind = name.rsplit(".", 1)[1]
        content_type = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(
            kind, "application/octet-stream"
        )
        self._send_headers(200, content_type, size)
        try:
            with file.open("rb") as image:
                shutil.copyfileobj(image, self.wfile)
        except OSError:
            self.close_connection = True

    def proxy(self) -> None:
        path = urlparse(self.path).path
        is_gen = self.command == "POST" and path in GEN_PATHS
        if is_gen and not self.generation_slots.acquire(blocking=False):
            self.close_connection = True
            self._send(503, b'{"error":"generation capacity reached"}', "application/json")
            return
        try:
            if self.headers.get("Transfer-Encoding") is not None:
                self.close_connection = True
                self._send(400, b'{"error":"unsupported transfer-encoding"}', "application/json")
                return
            lengths = self.headers.get_all("Content-Length", [])
            raw_length = lengths[0].strip() if lengths else "0"
            if len(lengths) > 1 or not re.fullmatch(r"[0-9]+", raw_length):
                self.close_connection = True
                self._send(400, b'{"error":"bad content-length"}', "application/json")
                return
            normalized_length = raw_length.lstrip("0") or "0"
            max_length = str(self.max_body_bytes)
            if len(normalized_length) > len(max_length) or (
                len(normalized_length) == len(max_length) and normalized_length > max_length
            ):
                self.close_connection = True
                self._send(413, b'{"error":"body too large"}', "application/json")
                return
            length = int(normalized_length)
            try:
                body = self.rfile.read(length) if length else b""
            except (OSError, TimeoutError):
                self.close_connection = True
                self._send(408, b'{"error":"request body timeout"}', "application/json")
                return
            if len(body) != length:
                self.close_connection = True
                self._send(400, b'{"error":"incomplete request body"}', "application/json")
                return
            if is_gen:
                try:
                    payload = json.loads(body.decode("utf-8"))
                    rewritten = rewrite_payload(payload, load_rules(self.config_path))
                    body = json.dumps(rewritten, ensure_ascii=False).encode("utf-8")
                    if len(body) > self.max_body_bytes:
                        self._send(413, b'{"error":"body too large"}', "application/json")
                        return
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(400, b'{"error":"generation body must be JSON"}', "application/json")
                    return
                except ValueError as e:
                    message = json.dumps({"error": str(e)}).encode("utf-8")
                    self._send(400, message, "application/json")
                    return
            status, raw, headers = self.forward(body)
            if status == 0:
                return
            content_type = get_header(headers, "Content-Type") or "application/octet-stream"
            body_changed = False
            if is_gen and status == 200:
                try:
                    raw = generation_result(raw, self.base_url, self.image_dir, self.ttl_seconds)
                    content_type = "application/json"
                    body_changed = True
                except ValueError as e:
                    message = json.dumps({"error": str(e)}).encode("utf-8")
                    self._send(502, message, "application/json")
                    return
            original_raw = raw
            raw = maybe_redact(raw)
            body_changed = body_changed or raw != original_raw
            self._send(status, raw, content_type, response_headers(headers, body_changed))
        finally:
            if is_gen:
                self.generation_slots.release()

    def forward(self, body: bytes) -> tuple[int, bytes, list[tuple[str, str]]]:
        request = Request(self.upstream + self.path, data=body or None, method=self.command)
        for key, value in self.headers.items():
            if key.lower() not in DROP_REQUEST_HEADERS:
                request.add_header(key, value)
        if body:
            request.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
            request.add_header("Content-Length", str(len(body)))
        try:
            with urlopen(request, timeout=self.upstream_timeout) as response:
                return response.status, response.read(), list(response.headers.items())
        except HTTPError as e:
            try:
                return e.code, e.read(), list(e.headers.items()) if e.headers else []
            except TimeoutError:
                self._send(504, b'{"error":"upstream timeout"}', "application/json")
                return 0, b"", []
        except TimeoutError:
            self._send(504, b'{"error":"upstream timeout"}', "application/json")
            return 0, b"", []
        except URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                self._send(504, b'{"error":"upstream timeout"}', "application/json")
            else:
                self._send(502, b'{"error":"upstream unreachable"}', "application/json")
            return 0, b"", []
        except (OSError, http.client.HTTPException):
            self._send(502, b'{"error":"invalid upstream response"}', "application/json")
            return 0, b"", []

    def _send(self, status: int, body: bytes, content_type: str, headers=()) -> None:
        if 100 <= status < 200 or status in {204, 205, 304}:
            body = b""
        self._send_headers(status, content_type, len(body), headers)
        try:
            self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def _send_headers(self, status: int, content_type: str, length: int, headers=()) -> None:
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        if not (100 <= status < 200 or status in {204, 304}):
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()


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
    RelayHandler.max_body_bytes = settings["max_body_bytes"]
    RelayHandler.generation_slots = threading.BoundedSemaphore(settings["max_generations"])
    hosts = listen_hosts(ip4, ip6)
    for host in hosts:
        assert_this_machine(host, own, "listen")
    servers = []
    connection_slots = threading.BoundedSemaphore(settings["max_connections"])
    try:
        for host in hosts:
            servers.append(
                make_server(
                    host,
                    port,
                    settings["max_connections"],
                    connection_slots,
                )
            )
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
