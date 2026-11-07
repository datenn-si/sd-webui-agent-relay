# sd-webui-agent-relay

[English](README.md) | **日本語**

Forge Neo API の前段に置くリレーです。Forge Neo にはリレー実行ホストのループバックアドレス `127.0.0.1` の 7860 番ポートで接続します。クラウド上のエージェントから Tailscale 経由でリレーへ接続する構成を想定しています。

リレーは次のアドレスだけで待ち受けます。

- `127.0.0.1`（リレー実行ホスト内からの接続）
- リレー実行ホストに割り当てられた Tailscale IPv4（`100.64.0.0/10`）
- リレー実行ホストに割り当てられた Tailscale IPv6（`fd7a:115c:a1e0::/48`）

`0.0.0.0` では待ち受けず、LAN やインターネットのインターフェースにも公開しません。`tailscale funnel` は使わないでください。

## 起動

```powershell
uv run python relay.py
```

起動ログの `tailscale` 行が、エージェントに渡すベース URL です。画像 URL にも同じホスト名（MagicDNS の `*.ts.net`）を使います。MagicDNS が取れないときだけ Tailscale IPv4 を使います。

Forge への転送先は常に `http://127.0.0.1:<forge_port>` です。`forge_port` は `config.json` で設定します。転送先をTailscaleアドレスに変更する設定はありません。

## 設定ファイル

サンプルからローカル設定を作成します。

```powershell
Copy-Item config.example.json config.json
```

`config.example.json` は Git で管理する設定例です。`config.json` はローカル設定として `.gitignore` の対象です。実際の転送先やプロンプトルールは `config.json` に設定してください。

## プロンプトルール

プロンプトルールは `config.json` の `positive` と `negative` に設定します。ファイルを保存すると、次の生成から反映されます。再起動は不要です。各ルールは次の順に適用します。

1. `replace` は Python の `re` による正規表現のリストです。上から順に `pattern` を `text` に置換します。`text` は空文字でもかまいません。
2. `delete` は削除する部分文字列のリストです。すべて削除したあと、余分なカンマを整理します。
3. `prefix` は項目を `", "` でつなぎ、プロンプトの先頭に追加します。既存のプロンプトが空でなければ、その後ろに `", "` を挟みます。
4. `suffix` は項目を `", "` でつなぎ、プロンプトの末尾に追加します。既存のプロンプトが空でなければ、その前に `", "` を挟みます。

たとえば、`prefix` が `["masterpiece", "best quality"]` で既存のプロンプトが `portrait` の場合、結果は `masterpiece, best quality, portrait` です。`suffix` が `["highly detailed"]` の場合は `portrait, highly detailed` になります。既存のプロンプトが空なら、追加した項目だけになり、余分なカンマは付きません。

`pattern` と `text` は Python の `re.compile` と `re.sub` に従います。大文字小文字を無視するフラグは `(?i)`、単語境界は `\b` です。グループ参照には `\1` を使います（`$1` ではありません）。JSON ではバックスラッシュを `\\` と書きます。

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

これは設定例の一部です。`config.json` 全体ではありません。

空の `hr_prompt` と `hr_negative_prompt` は空のままにします。Forge は Hires. fix 有効時に本体プロンプトをコピーします。

画像生成の JSON 応答には URL とシードだけを含め、プロンプト文は含めません。配信するファイルは Forge が返したバイト列そのものです。そのため、PNG などに含まれる parameters メタデータは残ります。

配信ファイルは `images/` に保存します。`image_ttl_hours`（既定値は168時間）を過ぎたファイルを削除します。値を `0` にすると削除しません。

## ファイアウォール

リレーは実行ホストに割り当てられた Tailscale IPv4 と IPv6 にだけ bind します。既存のファイアウォールルールが Tailscale 経由の受信を許可していれば、個別ルールの追加は不要です。

パブリック側のネットワークインターフェースにポートを開けないでください。`RemoteAddress` を `Any` にした許可ルールも追加しないでください。

受信ルールを明示する場合は、次のコマンドを使います。送信元の範囲は広げないでください。

```powershell
New-NetFirewallRule -DisplayName "SD Relay (Tailscale only)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7861 -RemoteAddress 100.64.0.0/10 -Profile Any
```

## テスト

テスト用の固定値を使い、HTTPテストはループバック上の偽Forgeにだけ接続します。Tailscaleや実際のForgeには接続しません。

```powershell
uv run python -m unittest
```

## ライセンス

このプロジェクトは [MIT License](LICENSE) のもとで公開されています。
