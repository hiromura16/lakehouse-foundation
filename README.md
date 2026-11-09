# Lakehouse Foundation

会社のMES・ERPデータを扱う、Iceberg中心のデータレイクハウス基盤です。公開リポジトリからcloneし、導入先のアドレス・認証情報を**リポジトリ外の設定ファイル**へ入力して構築できます。

SQL Serverは会社にある既存DBへ接続します。この構成にSQL Serverコンテナは含みません。

## 用意しているもの

| 目的 | 実装 |
| --- | --- |
| 永続保存・カタログ・SQL | SeaweedFS S3、Lakekeeper、PostgreSQL、Trino、Iceberg v2 / Parquet |
| メダリオン | Bronze原本・履歴、Silver型整備・重複排除、Quarantine不正行、Gold工場・受注集計 |
| ダミー投入 | MES生産実績・停止履歴、ERP製品・製造指図・受注・出荷をファイルまたはテスト用SQLスキーマへ投入 |
| GUI操作 | n8nのインポート用ワークフロー。変換自体のGUI候補はApache Hop・NiFiの比較資料 |
| 運用 | Grafana・Prometheus、Samba共有設定、Dokploy導入、企業proxy/CA手順 |
| 任意の計算・定期実行 | 対応JARを固定したSpark、Airflowと対応Provider、相互接続の確認コード |

最新版だけを個別に並べず、[互換性の表](docs/COMPATIBILITY.md)に沿って安定版のタグ・digest・JARを固定します。実機で通った範囲と未検証の範囲は [検証記録](docs/VALIDATION.md) に分けて記録します。

## 最短の導入

Linux x86-64、Docker Engine、Docker Compose **2.23.1以上**、Python **3.12以上**を用意します。通常ユーザーからDockerを管理できる状態を前提とし、既存ホストの権限をスクリプトで変更しません。最初はcoreだけで起動し、Airflow等は資源を確認して追加します。

```bash
git clone https://github.com/hiromura16/lakehouse-foundation.git
cd lakehouse-foundation

# 秘密値は自動生成され、画面には表示されません。
python3 scripts/configure.py \
  --output "$HOME/.config/lakehouse-foundation/site.env"

# 作成するディレクトリの所有者は、運用する通常ユーザーにします。
sudo install -d -m 0770 -o "$(id -un)" -g "$(id -gn)" \
  /srv/lakehouse/landing

# site.envのLANDING_DIR、UID/GID、使用ポートを手元で確認します。
./scripts/lakehouse.sh \
  --env-file "$HOME/.config/lakehouse-foundation/site.env" plan
./scripts/lakehouse.sh \
  --env-file "$HOME/.config/lakehouse-foundation/site.env" up
```

設定ファイルは環境変数で選べます。clone先のパスは自動で解決され、ホスト側のランディング先だけを `LANDING_DIR` で指定します。

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
./scripts/lakehouse.sh plan
./scripts/lakehouse.sh status
```

会社のproxyが必要な場合は、イメージ取得前に [PROXY.md](docs/PROXY.md) のdaemon設定・社内CAを準備します。Composeの環境変数だけではDocker自身のimage pullには反映されません。

初期状態のWeb口はloopbackに限定します。共有する場合はprivate設定の `BIND_IP`、認証、HTTPS、接続元制限を導入先に合わせて設定します。内部カタログは単一チームの検証モードです。

## ダミーデータを投入する

このコマンドは**素材を投入するだけ**です。取り込み・SQL変換・ワークフローを起動しません。

```bash
python3 scripts/generate-demo.py \
  --destination landing \
  --output /srv/lakehouse/landing \
  --seed 42 --orders 20 --days 7 --scenario edge
```

`clean` は正常データ、`edge` は重複・改訂・遅延・型不正・参照不整合を含むデータです。同じseedと引数で同じ内容を生成します。SQL Serverへの投入は専用の `DEMO_SQLSERVER_*` 設定と書き込み指定を必要とし、毎回新しいスキーマを使います。[データ契約・SQLへの投入方法](docs/DEMO-DATA.md)

## メダリオンを実行する

```bash
# 素材の投入とは別の操作です。Bronze取り込み→Silver/Gold更新を実行します。
./scripts/lakehouse.sh \
  --env-file "$HOME/.config/lakehouse-foundation/site.env" medallion

# 固定の疑似データを、隔離した検証用namespaceで確かめます。
./scripts/lakehouse.sh \
  --env-file "$HOME/.config/lakehouse-foundation/site.env" verify
```

業務ルールは [medallion/](medallion/) のコメント付きSQL、素材の列契約は [sources.json](medallion/sources.json) です。取り込み用Pythonを業務ルールごとに書き換える構成にしません。原本のハッシュと由来を保存し、更新ファイルの再投入・ジョブの再実行で二重計上しない仕組みにしています。

Goldでは工場・日別の生産進捗、良品数、不良数、歩留まり、停止時間と、受注別の出荷・未出荷・遅延を集計します。複数の実績と出荷を直接多重結合しないよう、先にそれぞれ集約します。

## GUI・監視・Spark・Airflow

private設定の `COMPOSE_PROFILES` へ必要な機能を追加し、`up` を再実行します。

| profile | 用途 |
| --- | --- |
| `automation` | n8n。`infra/automation/workflow-medallion.json` 等をインポートし、GUIで実行 |
| `monitoring` | PrometheusとGrafana。初回だけ登録し、その後はGUIで編集・保存 |
| `orchestration` | Airflow。明示的な手動DAG実行から開始 |
| `compute` | Sparkの相互運用確認。通常起動では常駐しない |

例: `COMPOSE_PROFILES=automation,monitoring`。n8nにはGUIからHTTP Header Auth資格情報を登録します。APIトークンをワークフローJSONに書き込みません。

- [GrafanaのGUI編集・保存・バックアップ](docs/GRAFANA.md)
- [GUIでできること・各OSSの比較](docs/GUI-OPTIONS.md)
- [構成とメダリオン設計](docs/ARCHITECTURE.md)
- [導入・運用・停止・GUI資格情報の手順](docs/DEPLOYMENT.md)
- [DokployのProject・環境の整理とGitからの展開](docs/DOKPLOY.md)
- [既存SQL Serverからの読み取り専用抽出](docs/SOURCE-SQLSERVER.md)
- [Spark / Airflow / Icebergの互換性](docs/COMPATIBILITY.md)
- [検証記録](docs/VALIDATION.md)

停止は `lakehouse.sh --env-file <private-file> down` です。データボリュームは保持します。既存DBのCDC、企業IdP、バックアップからの復元、AI推論は、それぞれ後続の接続・運用検証が必要です。
