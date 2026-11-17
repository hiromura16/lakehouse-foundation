# 中央アプリポータル

Foundation・Visionと共通管理を、一つの一覧から選ぶための静的Web UIです。配備先の入口は `https://apps.chaoslab.fyi/` です。Cloudflare Accessで所有者を認証してから開きます。旧ポータルのドメインは廃止済みです。

## ソースと非公開データ

- `worker.mjs`：固定5モジュールを配信するWorker。GET/HEADだけを受け付け、専用ホスト名とCSPを固定します。
- `site/index.html`・`style.css`・`app.js`：検索、環境・用途の絞り込み、接続経路切替、接続先のコピー、CSV出力。
- `site/catalog.example.json`：説明用の予約アドレスだけを使うサンプル。
- `site/catalog.json`：配備時に管理者が非公開で用意する実台帳。Gitから除外しています。パスワード・トークン・URL中の資格情報を登録しません。

公開ソースだけでは実際の接続先は復元されません。実台帳・Access設定・Tunnel設定・LANゲートウェイ設定・資格情報は、導入先の非公開管理で保持します。サンプルを実際の稼働状況として配備しません。

## カタログの構造

サンプルのトップレベルとアプリ1件を確認してから追加します。`id`は一意で、`environment`は`foundation`・`vision`・`truenas`・`shared`、`category`は画面の用途区分です。`endpoints`には`scope`（public/lan/tailscale/internal/local）、`kind`（web/api/metrics/database/protocol）、`label`、`url`を持たせます。

Web画面を持つアプリには、CloudflareのHTTPS URLとLAN直通URLを両方登録します。DBなどブラウザー画面のないものは内部接続情報として登録し、管理画面がある場合だけ`related`にそのアプリIDを指定します。存在しない管理画面や未構築のDBに仮のURLを付けません。

`state`と`checked_at`は確認時点の記録です。ポータルは常時監視装置ではありません。実行中コンテナとの照合とURL検査の後に台帳を更新します。

## 接続経路と認証

外出先ではCloudflareを選択します。LAN内でCloudflareを通さず利用する場合はLAN版のポータルを開き、`LAN直通`を選びます。各サービス自身のログインは経路にかかわらず必要です。LAN側もSecure Cookieを必要とするアプリにはHTTPSを使い、配備先のLAN CAを端末へ登録します。

選択した経路のURLがない場合、現在のUIはCloudflare URLへフォールバックし、カードに実際の経路を明示します。LANしか使わない運用では、対象の全Web入口にLAN URLがあることを検査してください。内部DBへWebリンクを作らないため、登録総数とWeb入口数は一致しません。

Cloudflare用のサービストークンと各アプリのキーは別の認証です。既存クライアントの許可範囲を増やして検査する方法は使いません。検査用の一時認証を作る場合は短時間・対象限定にし、終了後に削除して所有者ポリシーを戻します。

## 配備・更新

1. 非公開の実台帳を準備し、ID重複・関連ID・URL・資格情報混入を検査します。
2. Workerのホスト名を配備先に合わせ、Access保護を確認します。
3. 既存Workerの`/content` APIへ、`worker.mjs`と4つの`site/*`をmultipartで更新します。メインはJavaScript module、他4件はtext moduleです。既存compatibility設定・Accessポリシーを保持します。
4. LAN版へ同じサイトファイルと実台帳を同期します。LAN版ではアセットと`/catalog.json`も同じホストで配信します。
5. 公開URL、LAN URL、ログイン後の保護された読取り、狭い画面と詳細展開を検査します。

実台帳をデプロイログや公開CI artifactへ出力しません。`workers.dev`やプレビューを使わない配備では、それらの入口を無効にして保護対象のドメインへ統一します。

## UIの維持方針と検査

スローガンや宣伝文句を追加しません。見出しはアプリ名・機能・状態を簡潔に表します。320/390/768/1024/1440/1920px、長い名前・URL、接続先の詳細展開、絞り込みと空状態、アイコン・カードの境界を確認します。外部の表示検査サービスには内部接続先を含む実台帳を送らず、同じ構造の説明用データを使用します。

JavaScriptの構文確認：

```bash
node --check infra/app_portal/site/app.js
node --check infra/app_portal/worker.mjs
```

既存の詳細な構築・操作文書は`docs/`にあります。現在稼働する配備のURL・個別ログイン・障害切り分けは、管理者の非公開台帳とNotionの現行VM運用マニュアルを参照します。
