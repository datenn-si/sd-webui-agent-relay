# sd-webui-agent-relay

**English** | [日本語](README.ja.md)

A relay that sits in front of the Forge Neo API. It connects to Forge Neo on port 7860 of the loopback address `127.0.0.1` on the host running the relay. It is intended for setups where a cloud-hosted agent connects to the relay over Tailscale.

The relay listens only on the following addresses:

- `127.0.0.1` (connections from within the relay host)
- The Tailscale IPv4 address assigned to the relay host (`100.64.0.0/10`)
- The Tailscale IPv6 address assigned to the relay host (`fd7a:115c:a1e0::/48`)

It does not listen on `0.0.0.0`, and it is not exposed on LAN or internet interfaces. Do not use `tailscale funnel`.

The relay is reachable by devices on the Tailnet. Do not assume every device or user on the same Tailnet is trusted. Use Tailscale ACLs or grants to restrict which peers can connect when needed. Tailscale network access control is separate from application authentication; this relay does not implement its own authentication.

## Getting Started

```powershell
uv run python relay.py
```

The `tailscale` line in the startup log is the base URL to give to the agent. Image URLs use the same hostname (the MagicDNS `*.ts.net` name). Use the Tailscale IPv4 address only if MagicDNS is unavailable.

The forwarding destination to Forge is always `http://127.0.0.1:<forge_port>`. Set `forge_port` in `config.json`. There is no setting to change the forwarding destination to a Tailscale address.

The default limits are 16 simultaneous connections across all listener addresses, 2 simultaneous image generations, and a 512 MiB request body. Over-capacity connections and generation requests receive 503; rejected connections are closed. The request size limit applies before and after prompt rewriting.

## Relay specification

The relay speaks HTTP/1.1. It drops the `Upgrade` header, so a switch to WebSocket does not reach Forge.

### Generation

Generation is only `POST /sdapi/v1/txt2img` and `POST /sdapi/v1/img2img`. The body is a JSON object.

The relay applies the `positive` rules to `prompt` and the `negative` rules to `negative_prompt`. It applies the same side to `hr_prompt` and `hr_negative_prompt` when those values are not empty. An empty string in either Hires field stays empty, and a missing Hires field is not added. Forge copies the main prompt into an empty Hires prompt when Hires. fix is on. Prompt Rules describes how a rule is written. If a side has at least one rule and `prompt` or `negative_prompt` is missing, the relay applies the rules to an empty string and adds the field. `null` in `prompt` or `negative_prompt` is treated as an empty string.

A prompt field is a string. Each string in a list of strings is rewritten. Any other type is rejected.

Every other field is forwarded as received. Before forwarding, the relay sets `send_images` to `true`.

A successful generation body contains only `images`, `seeds`, and `subseeds`. It does not contain `info`, `parameters`, or prompt text.

```json
{
  "images": ["http://<host>:<listen_port>/images/20261107_000001_123456.png"],
  "seeds": [1234567890],
  "subseeds": [0]
}
```

The same index in `images`, `seeds`, and `subseeds` is one slot. When Forge returns an empty string or a non-string for a slot, that `images` entry is `null`. `seeds` and `subseeds` receive only the JSON integers from `all_seeds` and `all_subseeds` in Forge's `info`, starting at `index_of_first_image`. Slots before that index are `null`. When Forge sets the index for a leading grid image, that slot's seed is `null`. Slots do not line up when `return_mask` inserts images between seeds.

The relay stores the image bytes and returns a URL. The URL host is the `tailscale` line in the startup log. Tell the agent to pass that URL string on as it is. The agent must not decode it, request it, or save it to a file. The stored file is the byte string Forge returned, so parameters metadata in a PNG can still contain the rewritten prompt.

New file names use `YYYYMMDD_<sequence>_<seed>.<extension>`, for example `20261107_000001_123456.png`. The sequence is at least six digits and increments per saved image each day; `none` is used when Forge provides no seed for that image slot. Existing UUID-named files remain accessible. The `Content-Type` from `GET /images/<name>` is `image/png`, `image/jpeg`, `image/webp`, or `application/octet-stream`. A name in any other shape, and a missing file, return 404 and `not found`. Files older than `image_ttl_hours` are deleted on the next generation. `0` keeps the files.

### Other routes

Only API paths under `/sdapi/`, `/api/`, and `/internal/` are forwarded to Forge. `GET /favicon.ico` is also forwarded. `GET /images/<name>` is served from the relay's image directory. Other paths, including the WebUI root and static assets, return 404 without reaching Forge. The relay accepts `GET`, `POST`, `PUT`, `DELETE`, and `OPTIONS` on the API paths. It preserves Forge's status and body, and forwards end-to-end response headers while dropping hop-by-hop headers (including headers named by `Connection`), recalculating `Content-Length`, and enforcing its own `Cache-Control` and `X-Content-Type-Options`. If the body is rewritten or redacted, stale content encoding and entity validators are also dropped. The query string is forwarded, and whether a request is generation is decided from the path with the query string removed. Extension API generation routes can still return base64 image data.

The relay does not remove the contents of `current_image`, `image`, `images`, `init_images`, or `mask`. A progress response can still carry a preview in `current_image` as base64. When a rewritten prompt differs from the text the agent sent, the relay remembers up to 32 of those strings and removes them from the text of later responses. It does not remove them from image fields or from stored files. A string that contains one of them loses that part as well.

### Errors and limits

- The generation body is not valid JSON: 400 `{"error":"generation body must be JSON"}`.
- The JSON value is not an object: 400 `{"error":"generation body must be a JSON object"}`.
- A prompt field is neither a string nor a list of strings: 400 `{"error":"prompt fields must be strings"}`.
- `config.json` cannot be read, or the rules are invalid: 400 `{"error":"prompt rules file is invalid"}`. The server log has the detail.
- `Content-Length` is not an integer, or is duplicated: 400 `{"error":"bad content-length"}`. `Transfer-Encoding` is rejected.
- `Content-Length` is absent: the body is empty.
- `Content-Length` exceeds the configured limit (default 512 MiB): 413 `{"error":"body too large"}`. The rewritten generation body is checked too.
- An invalid request target or Host header: 400 `{"error":"invalid request"}`.
- A path outside the allowed API, favicon, and image routes: 404 `not found`.
- Forge cannot be reached: 502 `{"error":"upstream unreachable"}`. An upstream timeout: 504 `{"error":"upstream timeout"}`. An invalid upstream HTTP response: 502 `{"error":"invalid upstream response"}`. Generation requests time out after 6 hours by default.
- A generation response of 200 cannot be turned into URLs and seeds: 502. When the image list cannot be read, `error` is `upstream generation response was not JSON`, `upstream images field is not a list`, or `upstream image was not decodable`. When `index_of_first_image` cannot be read as an integer, the 502 `error` is that conversion message.
- Generation status is not 200: Forge's status and body are returned.

Forge error responses preserve their status, body, and applicable end-to-end headers. Responses include `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`. HTTP logs include the client IP, method, path without its query string, and response status. Query strings and request bodies are not logged.

A TLS client hello on this HTTP port closes the connection. The log line is `ignored TLS handshake on the HTTP port`. Any other non-HTTP request logs `ignored non-HTTP request`. Received non-HTTP bytes and request bodies are not written to the log.

## Configuration File

Create your local configuration from the sample.

```powershell
Copy-Item config.example.json config.json
```

`config.example.json` is the example configuration tracked in Git. `config.json` is your local configuration and is excluded via `.gitignore`. Set your actual forwarding destination and prompt rules in `config.json`.

Resource limits can be changed in `config.json`:

- `max_connections` (default `16`): simultaneous connections shared by the loopback, Tailscale IPv4, and Tailscale IPv6 listeners.
- `max_generations` (default `2`): simultaneous `txt2img` and `img2img` requests.
- `max_body_bytes` (default `536870912`, or 512 MiB): maximum accepted request body, including a rewritten generation body.

Each value must be a positive integer. Changes take effect after restarting the relay.

## Prompt Rules

Prompt rules are configured under `positive` and `negative` in `config.json`. Once you save the file, the rules take effect from the next generation; no restart is required. Each rule set is applied in the following order:

1. `replace` is a list of regular expressions using Python's `re`. Each `pattern` is replaced with `text` in the prompt, from top to bottom. `text` may be an empty string.
2. `delete` is a list of substrings to remove. After all of them are removed, redundant commas are cleaned up.
3. `prefix` joins its items with `", "` and adds them to the beginning of the prompt. If the existing prompt is not empty, `", "` is placed between the prefix and the existing prompt.
4. `suffix` joins its items with `", "` and adds them to the end of the prompt. If the existing prompt is not empty, `", "` is placed between the existing prompt and the suffix.

For example, if `prefix` is `["masterpiece", "best quality"]` and the existing prompt is `portrait`, the result is `masterpiece, best quality, portrait`. If `suffix` is `["highly detailed"]`, the result is `portrait, highly detailed`. If the existing prompt is empty, only the added items remain, with no stray commas.

`pattern` and `text` follow Python's `re.compile` and `re.sub`. Use `(?i)` for the case-insensitive flag and `\b` for word boundaries. Use `\1` for group references (not `$1`). In JSON, write backslashes as `\\`.

```json
"positive": {
  "replace": [
    { "pattern": "(?i)\\b1girls?\\b", "text": "1woman" },
    { "pattern": "(\\w+) hair", "text": "\\1 very long hair" }
  ],
  "delete": ["lowres"],
  "prefix": ["masterpiece", "best quality"],
  "suffix": ["highly detailed"]
}
```

This is an excerpt from an example configuration, not the entire `config.json`.

Leave an empty `hr_prompt` and `hr_negative_prompt` empty. When Hires. fix is enabled, Forge copies the main prompt.

The JSON response for image generation contains only URLs and seeds, not the prompt text. The served files are exactly the bytes returned by Forge, so the parameters metadata embedded in PNGs and similar files is preserved.

Served files are saved in `images/`. Files older than `image_ttl_hours` (default: 168 hours) are deleted. Setting the value to `0` disables deletion.

## Firewall

The relay binds to loopback and the Tailscale IPv4 and IPv6 addresses assigned to the host. If your existing firewall rules already allow inbound traffic over Tailscale, no additional rule is needed. Apply Tailscale ACLs or grants as appropriate to limit the source peers; a firewall rule allowing Tailscale traffic does not authenticate peers at the application layer.

Do not open ports on public-facing network interfaces. Do not add an allow rule with `RemoteAddress` set to `Any`.

To define an explicit inbound rule, use the command below. Do not widen the source range.

```powershell
New-NetFirewallRule -DisplayName "SD Relay (Tailscale only)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7861 -RemoteAddress 100.64.0.0/10 -Profile Any
```

## Testing

Tests use fixed test values, and HTTP tests connect only to a fake Forge on loopback. They do not connect to Tailscale or to a real Forge.

```powershell
uv run python -m unittest
```

## License

This project is licensed under the [MIT License](LICENSE).
