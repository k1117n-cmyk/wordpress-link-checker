# wordpress-link-checker

[![Python 3.11以上](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](#ダウンロード)
![対応OS: macOS / Linux](https://img.shields.io/badge/OS-macOS%20%7C%20Linux-555555)
![依存: Python標準ライブラリのみ](https://img.shields.io/badge/Dependencies-Standard%20library-2ea44f)
[![ライセンス: 独自利用許諾](https://img.shields.io/badge/License-Custom-orange)](LICENSE.txt)

WordPressサイトのリンク切れ候補を検出し、掲載元と検査結果をレポートにまとめる、無料のPython製CLIツールです。公開ページから内部・外部リンクを抽出し、GET応答を検査します。GUI画面はありません。Python 3.11以上、macOS/Linuxで動作する構成です。追加パッケージは不要です。Windowsはロック処理に使用するfcntlに対応しないため対象外です。

## ダウンロード

```sh
git clone https://github.com/k1117n-cmyk/wordpress-link-checker.git
cd wordpress-link-checker
python3 --version
```

Python 3.11以上が必要です。追加パッケージのインストールは不要です。

## 実行

`conf/linkcheck_example.json` を対象サイト用にコピーし、最低限 `site_id`、`site_url`、`sitemap_urls` を変更します。例のexample.comは実サイト用設定ではありません。

作業ディレクトリを `wordpress-link-checker/` にして実行します。設定例はそのまま使わず、手元の設定ファイルにコピーしてください。

```sh
# 設定例をコピーし、対象サイトの値へ編集する
cp conf/linkcheck_example.json conf/linkcheck_mysite.json

# 設定確認のみ。通信・ファイル作成なし
python3 bin/check_wordpress_links.py --config conf/linkcheck_mysite.json --validate-config

# 設定したサイトへのHTTP検査を実行
python3 bin/check_wordpress_links.py --config conf/linkcheck_mysite.json

# 自動テスト。外部サイトへの通信なし
python3 -m unittest discover -s tests -v
```

初回は少数ページ用の `conf/linkcheck_urls_example.json` をコピーし、`site_id`・`site_url`・`additional_urls` を実際の値へ変更して、小さい範囲から確認してください。トップページは常に収集候補に加わります。`max_pages` 等で打ち切った場合は検査未完了になります。

実行中は待機・通信中も含め、1秒ごとに `#` が増えて `###…` と表示されます。60文字ごとに改行し、終了・中断時に表示を停止します。これは稼働表示であり、処理済み件数や進捗率を示すものではありません。表示先は標準エラー出力です。`--validate-config` では表示しません。

## 設定

| 項目 | 意味 |
|---|---|
| site_id | 保存先に使う識別子。英数字・`_`・`-` |
| site_url | 公開サイトURL。WordPressをサブディレクトリに置く場合はそのURL |
| collection | `sitemap` / `rest` / `urls` のどれか |
| sitemap_urls | サイトマップ入口の配列。空ならオリジン直下の `/wp-sitemap.xml` |
| additional_urls | 追加の公開ページURL。サイトURLを基準とする相対URLも可 |
| allowed_origins | 追加の掲載元オリジン。wwwの有無、http/httpsの違いは明示指定。site_urlのオリジンは自動追加 |
| allowed_path_prefixes | 掲載元パスの先頭一致。既定は `/`。`/blog/` のように末尾の区切りを含める |
| exclude_patterns | 要求しないURLの正規表現配列。URLをパーセントデコードした文字列に適用。転送先にも適用 |
| check_images | trueならimgのsrc/srcsetも検査 |
| crawl_internal | trueなら許可範囲内のリンクから掲載元を追加収集 |
| soft_404_patterns | 本文の正規表現配列。2xxの受信範囲に合致したらsoft 404疑い。既定は空 |
| output_dir | 保存先。相対パスはこのREADMEがあるディレクトリを基準に解決 |

数値上限の既定値は [検出仕様](docs/DETECTION_SPEC.md) とスクリプト冒頭の `DEFAULTS` を参照してください。未知の設定キーはエラーになります。要求間隔は最低1秒、404/410の再確認は最低60秒後、一時障害の再試行は10秒・60秒以上待ちます。429ではRetry-Afterを優先し、実行期限を超える待機は未完了として終了します。

1回の実行内では逐次実行し、同時要求は最大1件です。複数プロセスを起動した場合の要求間隔は共有しません。再確認の待機時間も実行時間上限に含みます。大規模サイトでは複数時間に収まらない場合があるため、上限到達は結果に明示します。

収集方式は自動で切り替えません。サイトマップを利用できない場合は `collection: "rest"` または `"urls"` を指定してください。RESTは公開投稿・固定ページのみで、全ページ分のid/linkを取得してから公開HTMLを検査します。収集中の総件数変化・ID重複・途中失敗は未完了になります。

## 結果

既定では `reports/linkcheck/<site_id>/<実行日時_一意ID>/` に保存します。

- `summary.md`：冒頭にリンク切れ・その他の要確認・Amazon除外の件数を表示し、確認が必要なリンクの掲載元・リンク文字列・URL・HTTP判定・理由を一覧化します（最大100掲載箇所）。完了状態、件数表、適用設定、収集記録も含みます。全掲載箇所は `links.tsv` で確認できます。
- `links.tsv`：掲載元URL・タイトル・要素位置・リンク文字列・リンク先・判定。全掲載箇所を残します。
- `requests.jsonl`：GETごとの日時、URL、応答ヘッダー、エラー、打切り有無。転送・再試行・robots.txt・収集要求も含みます。本文は保存しません。
- `sources.tsv`：掲載元ごとの収集元、取得状態、抽出リンク数、除外・失敗理由。

| 判定 | 意味 |
|---|---|
| broken | 再確認でも404/410。HTTP応答に基づくリンク切れ候補。実際の削除確定ではない |
| candidate | 初回404/410。時間上限などで再確認できず未完了 |
| unreachable | DNS・接続・タイムアウト・408・5xx等の到達失敗 |
| pending | 401/403/429。認証・拒否・頻度制限 |
| tls_error / redirect_error | 証明書等のTLS異常／転送異常 |
| invalid_url / review | 不正・非公開接続先／その他の要確認応答 |
| soft_404_suspected | 設定した本文規則に合致。自動確定ではない |
| redirected / recovered | 転送後到達可能／再試行で回復 |
| unprocessed / excluded | 未処理／robots・除外規則・未対応形式等で対象外 |
| ok | HTTP上で到達可能 |

TSVはUTF-8、タブ区切り、CSVの引用規則を使います。フィールド内のバックスラッシュ・タブ・改行は `\\`・`\t`・`\r`・`\n` にエスケープします。集計は掲載箇所数と重複排除した確認URL数を区別します。URLのクエリは保持されるため、結果には機密情報が含まれる可能性があります。保存ディレクトリは所有者のみアクセス可能な権限で新規作成します。

終了コード：`0` は予定範囲完了・問題なし、`1` は完了・問題あり、`2` は未完了、`3` は設定不正・起動不能。転送のみ・回復のみでは1になりません。Ctrl+Cでは可能な範囲の結果を保存して2で終了します。同じ保存先・site_idでの二重起動は拒否します。

## ブラウザでの確認

Amazonの直接リンクと既知の短縮リンク（`link.amazon`、`amzn.to`、`amzn.asia`、`amzn.eu`、`amzn.in`、`amzn.com`、`a.co`）は、クリック集計への影響を避けるため、設定にかかわらず検査から除外します。対象ドメインとそのサブドメインにはrobots.txtも含めて通信せず、転送先が対象ドメインの場合もアクセス前に止めます。結果は理由付きの `excluded` となり、リンク切れかどうかは判定しません。対象ドメイン一覧はスクリプトの `AMAZON_DOMAINS` にあります。

独自の中継URLは、Amazonへの転送を検知する前に中継先へのアクセスが発生します。中継先自体がクリックを記録する場合もあるため、使用している中継URLは `exclude_patterns` に追加してください。

YouTubeなどでは、自動検査がエラーでもブラウザから表示できることがあります。`broken` を含め、HTTP応答だけでページの削除を断定せず、掲載元からブラウザで確認してからリンクを修正してください。この無料版にはAmazonの別枠集計や自動メール通知はありません。

## 制限

ログイン、JavaScript実行、ページ内アンカーの存在確認、本文の自動修正は行いません。公開ページのGETでもアクセスログ記録等は発生します。管理・認証・カート等の操作系URLは既定で除外し、サイト固有の操作URLはexclude_patternsに追加してください。

robots.txtを各オリジンで確認し、ワイルドカード・末尾一致・最長一致・同じUser-agentのグループ統合に対応します。Crawl-delayとRequest-rateも考慮します。robots.txtが404なら規則なし、取得失敗・受信打切り等なら当該オリジンを保留します。WAFやCAPTCHAを回避しません。

接続は80/443の公開IPに限り、各要求でDNSを検査し、検査済みIPに直接接続します。HTTPSは元のホスト名で証明書検証とSNIを行います。プロキシ環境変数は使用しません。総要求時間はDNS解決・TLS・本文受信を含めて制限します。追加ライブラリはなく、標準ライブラリのhttp.client、ssl、html.parser等を使用しています。

検査対象が多い場合やリンク先の再確認・再試行が多い場合、上限の2時間以内に完了しないことがあります。途中再開・差分検査はなく、毎回最初から検査します。投稿用サイトマップだけを指定した場合、固定ページ全件の検査は保証しません。

結果や要求履歴にはURL・クエリ・応答ヘッダー・ローカルパスが残ります。自分用の設定と結果はGitの管理対象から除外しています。定期実行設定・メール通知・サーバーへの設置・既存監視への統合は、この配布物には含めていません。

検出仕様は [docs/DETECTION_SPEC.md](docs/DETECTION_SPEC.md) を参照してください。

## ライセンス

本ソフトウェアは、[LICENSE.txt](LICENSE.txt) に記載した独自の利用許諾に基づいて無料で提供します。自身で所有または管理権限を有するWordPressサイトでの利用、運用に合わせたスクリプトの変更、必要なバックアップを認めています。再配布・再販売・公開・共有などの条件と保証に関する定めは、利用前にLICENSE.txtを確認してください。
