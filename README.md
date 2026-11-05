# Torelis eBay運用ツール（Phase 1：manual アダプタ）

要件定義書 v2.0 の **Phase 1（台帳と出品前工程・APIなしで完結）** の実装です。
カード1枚を「入荷 → 撮影 → 価格承認 → 出品（CSV）→ 落札・支払 → 発送」まで1本の台帳で追い、Discord に通知します。
eBay との接点は `ListingAdapter` / `OrderAdapter` に切り出してあり、Phase 1 は **manual アダプタのみ**（CSV出力・結果CSV取り込み・ボタンでの記録）です。

## 構成

```
caddy  … HTTPS（証明書自動更新）、/img/ の画像を認証なしで静的配信、それ以外を app へ
app    … FastAPI + Jinja2 + HTMX（画面とAPI）。起動時に DB マイグレーションを自動適用
worker … APScheduler（期限チェック・エスカレーション・為替・バックアップ・ディスク監視）と画像整形ジョブ（Pillow + rembg）
bot    … discord.py。outbox テーブルを拾って投稿、ボタン処理、Lv1/Lv2 ロール同期
```

DB は SQLite（WAL）＋ SQLAlchemy ＋ Alembic。DB・画像・バックアップはすべて名前付きボリューム `data`（コンテナ内 `/data`）に置き、全コンテナで共有します。
コンテナ間の直接通信はせず、app/worker → bot の投稿依頼は DB の `outbox` テーブル経由です。

```
app/
  models.py            データモデル（要件 8章）
  services/            業務ロジック（states / items / photos / pricing / listing / orders / notify / duty / users …）
  adapters/            eBay アダプタ（base.py に抽象クラス、manual.py が Phase 1 の実装）
  web/                 画面（routes/、templates/、static/）
  worker.py / bot.py / cli.py
config/ebay_csv_columns.json      Seller Hub 一括出品CSVの列定義（設定ファイルで変更可）
config/market_import.example.json 既存の価格CSVを取り込むための列マッピング例
migrations/                        Alembic
```

## 初回セットアップ（VPS）

前提：Docker / Docker Compose、サブドメインの DNS が VPS を向いていること。

```bash
git clone git@github.com:hegelgelda/ebayopssystem.git && cd ebayopssystem
cp .env.example .env    # 値を埋める（下記）
docker compose up -d --build          # またはビルド済みイメージを使う：docker compose pull && docker compose up -d
docker compose exec app python -m app.cli create-admin --discord-id <あなたのDiscordユーザーID> --name <表示名>
```

`https://<DOMAIN>/` を開き「Discordでログイン」。以降のユーザーは画面の「ユーザー」から Discord ユーザーID で招待します（許可リスト方式）。

### .env の主な項目

| キー | 内容 |
|---|---|
| `DOMAIN` / `BASE_URL` | 例：`ops.example.com` / `https://ops.example.com`。画像の公開URLとOAuthのリダイレクト先に使う |
| `SESSION_SECRET` | ランダムな長い文字列 |
| `HTTP_PORT` / `HTTPS_PORT` | 既存ジョブ（板記録など）とポートが衝突する場合に変更。80/443 を別のリバースプロキシが使っている場合は、そちらから `caddy` か `app:8000` に転送する |
| `DISCORD_*` | 下記「Discord の準備」 |
| `S3_*` | バックアップ先（板記録と同じバケット、`S3_PREFIX` で別プレフィックス） |
| `INSTALL_REMBG` / `ENABLE_REMBG` / `REMBG_MODEL` | 背景除去。`u2net`（標準）は worker が約1.3GB使う。メモリが厳しければ `u2netp` |
| `WORKER_MEM_LIMIT` | worker のメモリ上限（初期 2g） |
| `PSA_API_TOKEN` | 任意。PSA Public API があれば cert 検証ページのスクレイピングより優先 |

### Discord の準備

1. Developer Portal でアプリを作成し、Bot を追加してトークンを `DISCORD_BOT_TOKEN` に。
2. OAuth2 → Redirects に `https://<DOMAIN>/auth/callback` を登録。Client ID / Secret を `.env` に。
3. Bot をサーバーに招待（権限：View Channels, Send Messages, Create Public Threads, Send Messages in Threads, Manage Threads, Manage Roles）。Bot のロールを Lv1 / Lv2 ロールより上に置く（ロール同期のため）。
4. チャンネル `#orders`（**フォーラム**）、`#listing`、`#daily`、`#weekly`、`#system` とロール `Lv1`、`Lv2` を作り、それぞれの ID を `.env` に入れる。

Bot トークンが未設定の間も他のコンテナは動きます（通知は `outbox` に溜まり、設定後に順次投稿されます）。

### 既存データの取り込み

```bash
# CSV を data ボリュームに置く
docker compose cp ./import app:/data/import
# カード一覧（型番・日本語名・英語名・セット名・画像）。tcg-catalog から取り込む（毎日 4:10 に自動更新。設定画面の「今すぐ更新」でも可）
docker compose exec app python -m app.cli import-catalog
# モデルID → カード名（買取表HTMLの CARDS 定数。JSON / CSV も可）
docker compose exec app python -m app.cli import-cards --file /data/import/buyback.html
# Courtyard カタログ（cert番号 → モデルID）。列：cert_number, model_id, card_name, set_name, card_number, grader, grade
docker compose exec app python -m app.cli import-cert-catalog --file /data/import/courtyard_catalog.csv
# 価格参考値（冪等：model_id + source + metric + as_of で UPSERT）
docker compose exec app python -m app.cli import-market --file /data/import/prices_long.csv           # 縦持ち形式
docker compose exec app python -m app.cli import-market-mapped --config /data/import/market_import.json  # 横持ちCSVを列マッピングで
```

縦持ち形式の列は `model_id,source,metric,value,currency,count,as_of`。`source` は `courtyard_buyback / courtyard_p2p / pokeca / snkrdunk / pricecharting`、
`metric` は価格承認画面の参考値に対応します（`courtyard_*: median`、`pokeca: psa10_price / psa10_population / change_30d`、`snkrdunk: last_price`、`pricecharting: psa10`）。
既存の分析CSV（`card_price_liquidity_v2_jp.csv` 等）は列名に合わせて `config/market_import.example.json` をコピーして書き換えてください。

## 区分と出品レーン

| 区分 | 在庫のキー | 入荷時の特定方法 | 必須の写真 | 数量 | 既定のレーン |
|---|---|---|---|---|---|
| PSA鑑定品 | cert 番号 | ラベルのバーコード／QR → Courtyardカタログ・PSA | 表・裏 | 1 | オークション |
| シングル | SKU `S-00001` | **型番で検索**（「201」「201/165」、セットで絞り込み可）→ 候補から選ぶ | 表・裏 | 1〜 | 通常出品 |
| パック | SKU `P-00001` | セット名・IDで検索、または手入力 | 表・裏 | 1〜 | 通常出品 |
| BOX | SKU `X-00001` | セット名・IDで検索、または手入力 | 表・裏 | 1〜 | 通常出品 |
| バルク | SKU `L-00001` | 名称と内容を入力 | 全体1枚 | 1 | オークション |

- **SKUラベル**：PSA 以外は読み取れる番号が無いので、SKU を QR にしたラベル（A4シール／62×29mm のラベルプリンター）を「在庫」画面や入荷画面から印刷して貼る。撮影・在庫一覧の「読み取る」ボタンで QR を読むとその在庫が開く。印刷済みかどうかも記録する
- **出品レーン**（通常出品／オークション）は入荷時に決め、撮影 → 価格承認 → 出品準備 → CSV出力 → 出品中まで、画面がレーンごとのタブに分かれて混ざらずに進みます。CSV はレーンごとに出力し、混在させようとすると拒否されます。
- 通常出品は「販売価格」だけを承認し、GTC・数量付きで出品します。2個以上の在庫は通常出品のみで、販売を記録するたびに残り数が減り、0 になると売却済みになります。
- レーンは出品前ならいつでも在庫詳細・価格承認画面から切り替えられます（価格は新しいレーンで承認し直し）。
- 区分ごとの既定レーン、eBay カテゴリID・コンディションID、シングルの状態の選択肢、区分ごとの出品テンプレートは設定画面で変更できます。**eBay のカテゴリID・コンディションID（特にパック・BOX・未鑑定カードの状態ID）は初期値なので、Seller Hub のテンプレートで確認してください。**

## 日々の運用（manual アダプタ）

1. **入荷**（スマホ）：「入荷」→ cert番号を読み取り → 自動取得したカード情報を確認 → 仕入値・保管場所 → 「登録して次へ」
2. **撮影**（スマホ）：「撮影」→ 対象を開く（またはcertを読み取る）→ 表・裏（＋追加）を撮影。表裏が揃うと自動で「撮影済み」。整形済み画像は `https://<DOMAIN>/img/<cert>/<n>-<token>.jpg` で公開
3. **価格承認**（PC）：参考値・損益分岐価格・ツール案を見て、Lv1 は修正案を提案、Lv2 が承認 →「価格確定」
4. **出品準備**（PC）：「出品」→ 出品データを作成 → プレビューを Lv2 が確認 →「出品準備完了」→ CSV出力
5. Seller Hub（Reports → Uploads）に CSV をアップロード → 結果CSVをダウンロードして「結果CSVを取り込む」→「出品中」
6. **落札**：在庫詳細の「落札を記録」→ #orders にスレッドが作られ、その日の担当にメンション。未対応ならエスカレーション時間後に @Lv2
7. **支払**：注文詳細「支払を記録」→ 発送期限（日本の2営業日後 23:59 JST）が決まる
8. **発送**：追跡番号を入力 → 確認画面 → 登録 → 画面の手順で Seller Hub に登録して「eBayに登録済み」
9. 配送状態はキャリアの追跡ページを見て手で選ぶ（キャリアAPIは Phase 2）。配達完了から48時間で「完了」になり粗利を確定

不落札は在庫詳細の「不落札を記録」→ 在庫は「価格確定」に戻り、前回価格の -5% 案が価格承認に出ます。

## 更新（デプロイ）

```bash
git pull
docker compose pull && docker compose up -d     # GHCR のイメージを使う場合（.github/workflows/docker.yml が main への push でビルド）
# もしくはサーバーでビルド：docker compose up -d --build
```

DB マイグレーションは app の起動時に自動で適用されます（`python -m app.cli migrate`）。

## バックアップと復旧

- worker が毎日 03:30 JST に DB（SQLite のオンラインバックアップ）と画像（tar.gz）を `/data/backups` に作成し、`S3_*` が設定されていればオブジェクトストレージの `<S3_PREFIX>backups/` にもアップロードします。
- 保持期間：オブジェクトストレージは30日（`BACKUP_RETENTION_DAYS`）、VPS内は直近3日（`LOCAL_BACKUP_RETENTION_DAYS`）。画像の全量を毎日固めるため、VPS内は短くしてディスクを圧迫しないようにしています。**`S3_*` が未設定の間は VPS 内が唯一の保存先になるため、VPS 内にも30日分を残します**（その間はディスク使用率に注意。80%を超えると #system に通知されます）。
- 手動：`docker compose exec worker python -m app.cli backup`

### 復旧手順

```bash
docker compose exec app python -m app.cli list-backups          # 例：20261104-033000
docker compose stop app worker bot
docker compose run --rm --no-deps app python -m app.cli restore --stamp 20261104-033000
docker compose up -d
```

ローカルに無いバックアップはオブジェクトストレージから取得します。VPS を作り直した場合も、`.env` を戻して `docker compose up -d` した後に同じ手順で復旧できます。

## 開発

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                   # 外部API不要。manual アダプタで全経路をテスト
APP_ENV=dev DEV_LOGIN=1 DATA_DIR=./data BASE_URL=http://localhost:8000 \
  sh -c "python -m app.cli migrate && uvicorn app.web.main:app --reload"
```

`APP_ENV=dev` と `DEV_LOGIN=1` のときだけ、ログイン画面に Discord ユーザーIDでの開発用ログインが出ます（本番では無効）。
`scripts/smoke_e2e.py` は起動中の Compose 環境に対する通し確認です（入荷 → 撮影 → 承認 → CSV → 取り込み → 落札 → 発送）。

## Phase 1 の範囲と未実装

実装済み：Discord OAuth ログイン（許可リスト）、Lv1/Lv2 権限（API直叩きも拒否）、ユーザー管理の安全策、入荷登録（バーコード/QR読み取り、Courtyardカタログ → PSA → 手入力）、撮影・画像整形・公開配信・差し替え履歴・回線断時の再送、参考値インポートと価格承認（スナップショット保存・自己承認の記録・再出品の値下げ案）、出品データ生成（テンプレート編集可）、CSV出力・結果取り込み、落札・支払・キャンセル・不落札の記録、発送期限・支払期限、追跡番号（形式チェック・確認画面・Seller Hub手順）、手動の配送状態記録、作業チェックリスト、コメント、ケースの記録、Discord 通知（注文スレッド・担当メンション・ボタン・重複防止・エスカレーション・ロール同期）、操作ログ（追記のみ・DBトリガーで更新/削除を拒否）、ダッシュボード、楽観的ロック、30秒自動更新、PWA、為替の日次取得、バックアップ・復旧、ディスク使用率と外部API連続失敗の #system 通知、ログの14日ローテーション。

Phase 2 以降：FedEx/DHL 追跡API、まとめ通知（#daily）、出品日前日の催促、eBay API アダプタ、通知メールの取り込み、週次レポート、SOLD 6ヶ月後の元画像退避、取引完了1年後の匿名化。

### 実装時に確認が必要な点

- `config/ebay_csv_columns.json` の列名と Condition Descriptor の ID（Professional Grader / Grade）は、Seller Hub から最新のテンプレート（トレーディングカードカテゴリ）をダウンロードして照合してください。結果CSVの列名候補も同ファイルで変更できます。
- 手数料率・関税率・送料（設定 →「手数料率・費用」）は初期値です。実際の値に合わせてください。
- PSA cert 検証ページのスクレイピングはページ構造の変更や Bot 対策で失敗することがあります（その場合は手入力）。
