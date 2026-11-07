# sd-webui-agent-relay

**English** | [日本語](README.ja.md)

A relay that sits in front of the Forge Neo API. It connects to Forge Neo on port 7860 of the loopback address `127.0.0.1` on the host running the relay. It is intended for setups where a cloud-hosted agent connects to the relay over Tailscale.

The relay listens only on the following addresses:

- `127.0.0.1` (connections from within the relay host)
- The Tailscale IPv4 address assigned to the relay host (`100.64.0.0/10`)
- The Tailscale IPv6 address assigned to the relay host (`fd7a:115c:a1e0::/48`)

It does not listen on `0.0.0.0`, and it is not exposed on LAN or internet interfaces. Do not use `tailscale funnel`.

## Getting Started

```powershell
uv run python relay.py
```

The `tailscale` line in the startup log is the base URL to give to the agent. Image URLs use the same hostname (the MagicDNS `*.ts.net` name). Use the Tailscale IPv4 address only if MagicDNS is unavailable.

The forwarding destination to Forge is always `http://127.0.0.1:<forge_port>`. Set `forge_port` in `config.json`. There is no setting to change the forwarding destination to a Tailscale address.

## Configuration File

Create your local configuration from the sample.

```powershell
Copy-Item config.example.json config.json
```

`config.example.json` is the example configuration tracked in Git. `config.json` is your local configuration and is excluded via `.gitignore`. Set your actual forwarding destination and prompt rules in `config.json`.

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

The relay binds only to the Tailscale IPv4 and IPv6 addresses assigned to the host. If your existing firewall rules already allow inbound traffic over Tailscale, no additional rule is needed.

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
