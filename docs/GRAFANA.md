# GrafanaのGUI編集と保存

更新日: 2026-11-09。ダッシュボードの正本はGrafanaでGUIから保存した内容です。ComposeはGrafanaの起動、Prometheus datasource、永続volumeを管理し、ダッシュボードJSONを継続的にprovisionしません。

## 何をどこへ保存するか

| 対象 | 保存・管理方法 |
| --- | --- |
| Grafana本体、ポート、ログ | Composeで管理 |
| Prometheus datasource | `infra/monitoring/grafana-datasource.yml` をprovision |
| GUIで編集したダッシュボード | Grafana DB。名前付き `grafana` volumeに永続化 |
| 初回インポートのテンプレート | `infra/monitoring/dashboards/foundation.json` |
| GUI編集のJSON控え | Git外のprivateファイルへexportし、必要な内容だけレビューして版管理 |

datasourceは従来どおりprovisionされますが、ダッシュボードのfile providerは配置しません。リポジトリのJSONを書き換えて再配置しても、既存のGUI保存内容を自動的に差し替えません。

## 初回の起動とインポート

private設定の `COMPOSE_PROFILES` に `monitoring` を含めて起動します。以下は `LAKEHOUSE_ENV_FILE` を設定済みの場合です。明示的な `--env-file` も引き続き使えます。

```bash
./scripts/lakehouse.sh up
```

起動後、CLIはGrafanaのreadinessを最大60秒待ち、UID `lakehouse-operations` のダッシュボードを確認します。

- UIDが存在する場合は、その内容を読み替えずに終了します。
- UIDが存在しない場合だけ、テンプレートから `Lakehouse Operations` を作成します。
- 権限エラーやサーバー障害は、既存ダッシュボードの削除・上書きで回避しません。
- 作成が競合した場合も、既存UIDを再確認して保持します。更新用のPUTやDELETEは実行しません。

DokployのGit配置など、CLIの `up` を経由せずにGrafanaを起動した場合は、起動後に明示的な初期化を実行します。

```bash
./scripts/lakehouse.sh grafana-init
```

`grafana-init` はデプロイや再起動を行わず、同じ作成専用の確認を行います。監視を起動しているサイトのprivate envを選び、実際のGrafanaポートへ接続してください。

テンプレートは `editable: true` のclassic dashboard JSONです。インポートにはGrafana 13の新APIを使い、`metadata.name` にUID、`spec` にダッシュボードのモデルを渡します。旧 `/api/dashboards/db` へのfallbackは実装しません。[Dashboard API](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/dashboard/)

## GUIで編集する

Grafanaで `Lakehouse Operations` を開き、パネル、配置、PromQL、表示期間等を編集してSaveします。保存後はGrafana DBの内容が正本です。コンテナを再起動・再作成しても、同じ名前付きvolumeを使う限り保存先を保持します。

確認するときは、GUIでタイトルやパネルを変更して保存し、Grafanaを再起動した後に同じダッシュボードを開きます。その後 `grafana-init` を再実行し、保存した変更が残っていることを確認します。サイトでの実行結果は検証記録へ残してください。

既存の別UIDのダッシュボードは、初期化スクリプトで削除・変換しません。以前のfile provisioningで作ったものが残っている場合は、新しい編集可能なダッシュボードと区別して扱います。対象UIDを手動で削除した後に初期化を実行すると、テンプレートから新規作成されます。

## GUIの内容をJSONファイルへ残す

初期ダッシュボードの現在の内容を、Git外のprivateファイルへ書き出せます。

```bash
python3 scripts/grafana_setup.py export \
  --output /absolute/private/grafana/dashboard-review.json
```

`LAKEHOUSE_ENV_FILE` を使わない場合:

```bash
python3 scripts/grafana_setup.py \
  --env-file /absolute/private/site.env export \
  --output /absolute/private/grafana/dashboard-review.json
```

exportは読み取り専用です。出力先はリポジトリ外の絶対パスに限定し、親ディレクトリは0700、出力ファイルは0600で保持します。既存ファイルを上書きせず、公開ソースのテンプレートへ直接書き込みません。ダッシュボードのパスワードやAPI応答本文もコンソールへ出しません。

出力するのは現在のダッシュボードモデルで、GUIで変更したtitle、panels、queries等を含みます。実際の接続先、業務上の値、個人情報が含まれ得るため、公開用に残す内容をレビューします。テンプレートへ反映する場合も、初回導入用の見本の更新として扱い、既存環境へ自動同期しません。

GUIで新たに作った別UIDのダッシュボードは、GrafanaのJSON export機能で控えを保存できます。このCLI exportの対象は初期UID `lakehouse-operations` です。

## 認証とバックアップ

初期化とCLI exportは、private envの `GRAFANA_ADMIN_PASSWORD` と管理者 `admin` によるBasic認証を使います。認証情報をURLへ埋め込まず、対象のローカル／private Grafanaへ直接接続します。proxyとHTTP redirectを経由して資格情報を転送しません。[Grafana OSSのAPI認証](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/authentication/)

GUIで管理者パスワードを変更した場合は、private設定も現在の値へ合わせます。永続DBがある状態でComposeの初期パスワード環境変数だけを変更しても、既存の管理者パスワードがその値へ変更されるとは限りません。

GUI内容の永続化にはGrafanaの名前付きvolumeが必要です。JSON exportはダッシュボードの控えであり、利用者、権限、datasourceの資格情報等を含むGrafana全体のバックアップとは別です。データ移行時はvolume、現在の認証情報、必要なバックアップと復元手順を保持します。
