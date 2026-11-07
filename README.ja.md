# sd-webui-agent-relay

[English](README.md) | **日本語**

Forge Neo API の前段に置くリレーです。Forge Neo にはリレー実行ホストのループバックアドレス `127.0.0.1` の 7860 番ポートで接続します。クラウド上のエージェントから Tailscale 経由でリレーへ接続する構成を想定しています。

リレーは次のアドレスだけで待ち受けます。

- `127.0.0.1`（リレー実行ホスト内からの接続）
- リレー実行ホストに割り当てられた Tailscale IPv4（`100.64.0.0/10`）
- リレー実行ホストに割り当てられた Tailscale IPv6（`fd7a:115c:a1e0::/48`）

`0.0.0.0` では待ち受けず、LAN やインターネットのインターフェースにも公開しません。`tailscale funnel` は使わないでください。

リレーには Tailnet 内の端末から接続できます。同じ Tailnet に参加している端末や利用者がすべて信頼できるとは限りません。必要に応じて Tailscale ACL または grants で接続元を制限してください。Tailscale によるネットワークアクセス制御はアプリケーション独自の認証とは別のもので、このリレーには独自認証機能はありません。

## 起動

```powershell
uv run python relay.py
```

起動ログの `tailscale` 行がエージェントに渡すベース URL です。画像 URL にも同じホスト名（MagicDNS の `*.ts.net`）を使います。MagicDNS が取れないときだけ Tailscale IPv4 を使います。

Forge への転送先は常に `http://127.0.0.1:<forge_port>` です。`forge_port` は `config.json` で設定します。転送先をTailscaleアドレスに変更する設定はありません。

既定値は、全待ち受けアドレスを合わせて同時接続16、画像生成の同時実行2、リクエストボディ512 MiBです。上限に達した接続または生成要求には503を返して接続を閉じます。ボディサイズは受信時とプロンプト書き換え後の両方で確認します。

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
  "images": ["http://<ホスト>:<listen_port>/images/20261107_000001_123456.png"],
  "seeds": [1234567890],
  "subseeds": [0]
}
```

`images`、`seeds`、`subseeds` の同じ添字が1枚の枠です。Forge がその枠に空文字、または文字列以外を返したとき、その `images` は `null` です。`seeds` と `subseeds` には Forge の `info` にある `all_seeds` と `all_subseeds` の JSON の整数だけを、`index_of_first_image` の位置から置きます。その位置より前の枠は `null` です。Forge が先頭のグリッド画像のためにこの値を置くと、その枠のシードは `null` になります。`return_mask` がシードの間に画像を入れる並びでは、枠は揃いません。

画像のバイト列はリレーが保存し、URL だけを返します。URL のホストは起動ログの `tailscale` 行と同じです。エージェントにはこの URL を復号せず、取得せず、ファイルへ保存せず、文字列のまま渡すよう伝えてください。保存したファイルは Forge が返したバイト列そのものです。PNG などの parameters メタデータには、書き換えたプロンプトが残ることがあります。

新しいファイル名は `YYYYMMDD_<連番>_<シード>.<拡張子>` 形式です（例: `20261107_000001_123456.png`）。連番は画像ごとに日付単位で増加し、最低6桁です。Forgeからその画像スロットのシードが得られない場合は `none` を使います。既存のUUID形式のファイル名も引き続き配信できます。`GET /images/<名前>` が返す Content-Type は `image/png`、`image/jpeg`、`image/webp`、`application/octet-stream` です。名前がこの形でないときと、ファイルが無いときは、404 と `not found` を返します。`image_ttl_hours` を過ぎたファイルは次の生成のときに削除します。値が `0` のときは削除しません。

### 生成以外

Forge へ転送するのは `/sdapi/`、`/api/`、`/internal/` 配下のAPIパスと `GET /favicon.ico` だけです。`GET /images/<名前>` はリレー内の画像ディレクトリから配信します。WebUIのルートや静的ファイルなど、それ以外のパスはForgeへ転送せず404を返します。APIパスでは `GET`、`POST`、`PUT`、`DELETE`、`OPTIONS` を受け付けます。Forgeのステータスとボディを維持し、エンドツーエンドのレスポンスヘッダーを転送しますが、ホップバイホップヘッダー（`Connection` が指定するヘッダーを含む）は除きます。`Content-Length` は再計算し、`Cache-Control` と `X-Content-Type-Options` はリレー側で設定します。ボディを書き換えたり秘匿処理したりした場合、古くなる圧縮情報やエンティティ検証ヘッダーも除きます。クエリ文字列は転送し、生成かどうかはクエリ文字列を除いたパスで判定します。拡張機能のAPI生成ルートでも、応答に base64 の画像が残ることがあります。

`current_image`、`image`、`images`、`init_images`、`mask` の中身は消しません。進行状況の `current_image` には、途中の画像が base64 で残ることがあります。書き換えたプロンプトがエージェントの送った文と違うとき、リレーはその文字列を32件まで覚え、あとの応答の文字列から除きます。画像の欄と、保存したファイルからは除きません。別の文字列にその文が含まれていれば、その部分も除きます。

### エラーと制限

- 生成のボディが JSON として読めない: 400 `{"error":"generation body must be JSON"}`
- JSON だがオブジェクトでない: 400 `{"error":"generation body must be a JSON object"}`
- プロンプト欄が文字列でも文字列の配列でもない: 400 `{"error":"prompt fields must be strings"}`
- `config.json` が読めない、またはルールとして無効: 400 `{"error":"prompt rules file is invalid"}`。詳しい理由はサーバーのログに出します。
- `Content-Length` が整数でない、または複数ある: 400 `{"error":"bad content-length"}`。`Transfer-Encoding` は受け付けません。
- `Content-Length` が無い: ボディは空として扱います
- `Content-Length` が設定上限（既定512 MiB）を超える: 413 `{"error":"body too large"}`。書き換え後の生成ボディも確認します。
- 不正なリクエストターゲットまたはHostヘッダー: 400 `{"error":"invalid request"}`
- 許可されたAPI、favicon、画像以外のパス: 404 `not found`
- Forgeへ接続できない: 502 `{"error":"upstream unreachable"}`。上流タイムアウト: 504 `{"error":"upstream timeout"}`。不正な上流HTTP応答: 502 `{"error":"invalid upstream response"}`。生成要求のタイムアウトは既定6時間です。
- 生成の 200 応答を URL とシードにできない: 502。画像の並びを読めないときの `error` は `upstream generation response was not JSON`、`upstream images field is not a list`、`upstream image was not decodable` です。`index_of_first_image` を整数として読めないときも 502 で、変換に失敗したメッセージが `error` になります。
- 生成のステータスが 200 以外: Forge のステータスとボディを返します

ForgeのHTTPエラー応答は、可能な範囲でステータス、ボディ、エンドツーエンドのヘッダーを維持します。応答には `Cache-Control: private, no-store` と `X-Content-Type-Options: nosniff` を付けます。HTTPログには接続元IP、メソッド、クエリ文字列を除いたパス、応答ステータスを記録します。クエリ文字列とリクエストボディは記録しません。

この HTTP ポートに TLS の開始バイトが来たときは、接続を切り、ログに `ignored TLS handshake on the HTTP port` と出します。それ以外の HTTP でない要求は、ログに `ignored non-HTTP request` と出します。受け取ったバイト列とリクエストボディはログに書きません。

## 設定ファイル

サンプルからローカル設定を作成します。

```powershell
Copy-Item config.example.json config.json
```

`config.example.json` は Git で管理する設定例です。`config.json` はローカル設定として `.gitignore` の対象です。実際の転送先やプロンプトルールは `config.json` に設定してください。

リソース制限は `config.json` で変更できます。

- `max_connections`（既定値 `16`）: loopback、Tailscale IPv4、Tailscale IPv6の全リスナーで共有する同時接続数
- `max_generations`（既定値 `2`）: `txt2img` と `img2img` の同時実行数
- `max_body_bytes`（既定値 `536870912`、512 MiB）: 受信ボディと書き換え後の生成ボディの最大サイズ

いずれも正の整数です。変更はリレーの再起動後に反映されます。

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

リレーは loopback と実行ホストに割り当てられた Tailscale IPv4/IPv6 に bind します。既存のファイアウォールルールが Tailscale 経由の受信を許可していれば、個別ルールの追加は不要です。接続元を制限するには、必要に応じてTailscale ACLまたはgrantsを設定してください。Tailscale経由を許可するファイアウォールルールは、アプリケーション層の認証ではありません。

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
