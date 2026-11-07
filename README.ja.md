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

起動ログの `tailscale` 行がエージェントに渡すベース URL です。画像 URL にも同じホスト名（MagicDNS の `*.ts.net`）を使います。MagicDNS が取れないときだけ Tailscale IPv4 を使います。

Forge への転送先は常に `http://127.0.0.1:<forge_port>` です。`forge_port` は `config.json` で設定します。転送先をTailscaleアドレスに変更する設定はありません。

## リレー仕様

リレーのプロトコルは HTTP/1.1 です。`Upgrade` ヘッダーは Forge へ転送しないので、WebSocket への切り替えは Forge に届きません。

### 生成

生成は `POST /sdapi/v1/txt2img` と `POST /sdapi/v1/img2img` だけです。ボディは JSON オブジェクトです。

`prompt` には `positive` のルールを、`negative_prompt` には `negative` のルールを適用します。空でない `hr_prompt` と `hr_negative_prompt` にも同じ側のルールを適用します。この2つの欄が空文字のときは空のままにし、ボディに無いときも欄は足しません。Hires. fix が有効なとき、Forge は空の Hires 用プロンプトへ本体のプロンプトをコピーします。ルールの書き方は「プロンプトルール」にあります。ルールが1つでもある側で、`prompt` または `negative_prompt` が無いときは、空文字にルールを適用してその欄を足します。`prompt` と `negative_prompt` の `null` は空文字として扱います。

プロンプト欄は文字列です。文字列の配列なら、各文字列にルールを適用します。それ以外の型は受け付けません。

プロンプト欄以外は受け取ったまま Forge へ渡します。転送の前に、リレーは `send_images` を `true` にします。

生成が成功したときのボディは `images`、`seeds`、`subseeds` だけです。`info` も `parameters` もプロンプト文も入れません。

```json
{
  "images": ["http://<ホスト>:<listen_port>/images/<小文字16進32桁>.png"],
  "seeds": [1234567890],
  "subseeds": [0]
}
```

`images`、`seeds`、`subseeds` の同じ添字が1枚の枠です。Forge がその枠に空文字、または文字列以外を返したとき、その `images` は `null` です。`seeds` と `subseeds` には Forge の `info` にある `all_seeds` と `all_subseeds` の JSON の整数だけを、`index_of_first_image` の位置から置きます。その位置より前の枠は `null` です。Forge が先頭のグリッド画像のためにこの値を置くと、その枠のシードは `null` になります。`return_mask` がシードの間に画像を入れる並びでは、枠は揃いません。

画像のバイト列はリレーが保存し、URL だけを返します。URL のホストは起動ログの `tailscale` 行と同じです。エージェントにはこの URL を復号せず、取得せず、ファイルへ保存せず、文字列のまま渡すよう伝えてください。保存したファイルは Forge が返したバイト列そのものです。PNG などの parameters メタデータには、書き換えたプロンプトが残ることがあります。

ファイル名は小文字の16進32桁に `.png`、`.jpg`、`.webp`、`.bin` のいずれかを付けたものです。`GET /images/<名前>` が返す Content-Type は `image/png`、`image/jpeg`、`image/webp`、`application/octet-stream` です。名前がこの形でないときと、ファイルが無いときは、404 と `not found` を返します。`image_ttl_hours` を過ぎたファイルは次の生成のときに削除します。値が `0` のときは削除しません。

### 生成以外

リレーが受け付けるメソッドは `GET`、`POST`、`PUT`、`DELETE`、`OPTIONS` です。`GET /images/...` と上の2つの生成を除き、Forge のステータスとボディをそのまま返します。クエリ文字列は転送し、生成かどうかはクエリ文字列を除いたパスで判定します。拡張機能の生成ルートもこの扱いで、応答に base64 の画像が残ることがあります。

`current_image`、`image`、`images`、`init_images`、`mask` の中身は消しません。進行状況の `current_image` には、途中の画像が base64 で残ることがあります。書き換えたプロンプトがエージェントの送った文と違うとき、リレーはその文字列を32件まで覚え、あとの応答の文字列から除きます。画像の欄と、保存したファイルからは除きません。別の文字列にその文が含まれていれば、その部分も除きます。

### エラーと制限

- 生成のボディが JSON として読めない: 400 `{"error":"generation body must be JSON"}`
- JSON だがオブジェクトでない: 400 `{"error":"generation body must be a JSON object"}`
- プロンプト欄が文字列でも文字列の配列でもない: 400 `{"error":"prompt fields must be strings"}`
- `config.json` が読めない、またはルールとして無効: 400 `{"error":"prompt rules file is invalid"}`。詳しい理由はサーバーのログに出します。
- `Content-Length` が整数でない: 400 `{"error":"bad content-length"}`
- `Content-Length` が無い: ボディは空として扱います
- `Content-Length` が 0 未満、または 512 MiB を超える: 413 `{"error":"body too large"}`
- Forge へ接続できない、または生成が 6時間で終わらない: 502 `{"error":"upstream unreachable"}`
- 生成の 200 応答を URL とシードにできない: 502。画像の並びを読めないときの `error` は `upstream generation response was not JSON`、`upstream images field is not a list`、`upstream image was not decodable` です。`index_of_first_image` を整数として読めないときも 502 で、変換に失敗したメッセージが `error` になります。
- 生成のステータスが 200 以外: Forge のステータスとボディを返します

応答には `Cache-Control: private, no-store` と `X-Content-Type-Options: nosniff` を付けます。

この HTTP ポートに TLS の開始バイトが来たときは、接続を切り、ログに `ignored TLS handshake on the HTTP port` と出します。それ以外の HTTP でない要求は、ログに `ignored non-HTTP request` と出します。受け取ったバイト列はログに書きません。

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
