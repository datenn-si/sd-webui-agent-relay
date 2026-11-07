# Agent Instructions

## Security and local data

- Keep the upstream on loopback. The relay listener may use loopback or the Tailscale addresses assigned to the relay host. Do not bind to `0.0.0.0` or recommend broad firewall rules.
- Do not add real machine addresses, MagicDNS names, local paths, prompt rules, or credentials to tracked source or documentation.
- Treat `config.json`, `.env` files, `config.local.json`, and `images/` as local data. Do not stage or publish them.
- Keep `config.example.json` as a tracked, anonymized template and update it when the configuration schema changes.
- Keep user-facing setup and network guidance in `README.md`. Put instructions for coding agents in this file.

## Development

- Python 3.11 or newer is required. Runtime code uses the standard library.
- Run tests with `uv run python -m unittest`.
- Run lint with `uv run ruff check`.
- Tests use fixed placeholder network identity values and only send HTTP requests over loopback.
