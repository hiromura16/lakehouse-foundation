# はじめに読むガイド

このリポジトリは、会社のMES・ERPを取り込むための単一ホストの導入基盤です。ファイルを集め、Icebergで複数エンジンから参照し、業務指標へ加工するところまでを、合成データで確認してから実データへ進めます。

## 最初に決める4つのこと

| 決めること | なぜ必要か | 保存先 |
| --- | --- | --- |
| 実行するサーバーと管理担当者 | Dockerの操作権限、容量、障害対応の責任を明確にする | 会社の運用記録 |
| ランディングの場所 | cloneや再配置で素材が消えないよう、コードから分離する | private envの `LANDING_DIR` |
| 接続を許可する範囲 | 初期のloopbackから、認証付きの共有へ段階的に進む | `BIND_IP`、ホスト側通信制御、IdP |
| 試験用と業務用の接続先 | ダミー投入先と参照先の取り違えを防ぐ | `DEMO_SQLSERVER_*` と `SOURCE_SQLSERVER_*` |

コードのclone先は任意です。`scripts/lakehouse.sh` が自身の場所から構成ファイルを探すため、他の人と同じ絶対パスへ置く必要はありません。

## 読む順番

### 初めて構築する人

1. [全体構成図](ARCHITECTURE.md) で、保存、処理、GUIの関係を把握します。
2. [会社への導入手順](COMPANY-ROLLOUT.md) に沿って、proxy、CA、ホスト、private設定を準備します。
3. [設定項目一覧](CONFIGURATION.md) を確認し、coreだけを起動します。
4. [ダミー投入GUI](DEMO-GUI.md) と [メダリオン](MEDALLION.md) で合成データの経路を試します。
5. [GUI操作ガイド](GUI-WALKTHROUGHS.md) を見てn8n、Grafana、Airflowを操作します。
6. [会社SQLの読み取り](SOURCE-SQLSERVER.md) を確認してから、実際の参照用接続を設定します。
7. [運用手順](OPERATIONS.md) と [障害対応](TROUBLESHOOTING.md) を担当者間で確認します。

### 業務指標や変換を変更する人

[データ契約](DEMO-DATA.md) → [メダリオンの処理順](MEDALLION.md) → `medallion/` のSQL → [検証記録](VALIDATION.md) の順に読みます。Pythonの取り込み制御と、SQLの業務ルールは別の責務です。

### デプロイを管理する人

[Dokploy](DOKPLOY.md) → [互換性](COMPATIBILITY.md) → [proxyとCA](PROXY.md) → [運用](OPERATIONS.md) の順に読みます。イメージの単独更新ではなく、Spark・Iceberg、Airflow・Provider・Pythonの組み合わせを確認します。

### AIや外部サービスを追加する人

[AI連携](AI-READINESS.md) でデータの取り出しと根拠を確認し、[Cloudflare](CLOUDFLARE.md) で社内に残すものと外部サービスを使う範囲を決めます。接続例があることと、モデル推論や外部サービスの運用が有効になっていることは別です。

## 最初の受け入れ確認

一度に全コンテナを有効にせず、次の順序で結果を残します。

| 段階 | 成功したと判断する条件 |
| --- | --- |
| 設定 | `plan` が成功し、選択したprofileとvolume名・landingが意図どおり |
| core | DB、S3、カタログ、Trino、workerが起動し、workerの初期化が成功 |
| 素材 | 合成ファイルと `.ready` ができ、SQL投入では専用の試験schemaだけが増える |
| 加工 | `verify` が画像原本、10組の期待値、二度目の取り込みskipを確認 |
| GUI | n8nの実行履歴が成功し、Grafanaで保存した編集が再起動後も残る |
| 任意エンジン | Sparkの3行をTrinoから参照し、Airflowの手動DAGが成功 |
| 実接続 | 会社のSQL認証・CA・権限・proxyを、その会社の環境で確認 |

合成データの検証成功だけで、本番の容量、性能、利用者認可、バックアップ復旧を判定しません。

## この構成での「保存」と「反映」

設定ファイルを保存しても、起動中コンテナの環境変数やmountは自動的には変わりません。GUIで設定を保存した後は、対象の変更を確認し、Dokployから再配置します。GUIが表示する保存状態と稼働状態を区別します。

同じように、素材投入は取り込みを起動しません。ダミー投入、`ingest`、`medallion`、検証は、意図して別の操作にしています。GUIやAPIから操作する場合もこの境界を維持します。

## 目的別の資料一覧

| 確認したい内容 | 資料 |
| --- | --- |
| ファイルと責任範囲 | [ディレクトリ案内](DIRECTORY-GUIDE.md) |
| APIとSwagger UI | [API](API.md) |
| CSV・JSON・画像・イベント投入 | [ダミーGUI](DEMO-GUI.md)、[素材仕様](DEMO-DATA.md) |
| SQL ServerのCDC | [CDC](CDC.md) |
| GrafanaのGUI保存 | [Grafana](GRAFANA.md) |
| 実際に成功した試験と制約 | [検証記録](VALIDATION.md) |
