# GrafanaのGUI編集と保存

照合日: 2026-11-10。Grafana **13.3.0** とPrometheus **3.16.0**、`scripts/grafana_setup.py` が対象です。ダッシュボードの正本はGrafanaでGUIから保存した内容です。ComposeはGrafanaの起動、Prometheus datasource、永続volumeを管理し、ダッシュボードJSONを継続的にprovisionしません。

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

以下の `up` は**CLI管理の場合だけ**実行します。Dokploy管理ではComposeサービス自身のEnvironmentを保存し、7ファイルを使ってDeployします。起動方法を重ねません。[配置手順](DOKPLOY.md)

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
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

### Dokployとブラウザーだけで初回Importする場合

CLIを使わない場合は次の手順で同じテンプレートをGUIへ取り込めます。既に同じUIDがある環境ではImportせず、そのDashboardを開きます。

1. Dokployの **Containers** で `grafana` / `prometheus` / `worker` の状態を確認します。画面URLはprivate envの `GRAFANA_PORT` で、既定は `http://localhost:3000` です。
2. 管理者のローカルエディターでprivate envを開き、`admin` と `GRAFANA_ADMIN_PASSWORD` でGrafanaへログインします。ownerを別GUIで作るn8nとは異なります。
3. **Connections → Data sources** でPrometheusを確認します。内部URLは `http://prometheus:9090`、UIDは `foundation-prometheus` です。provisionされた接続なのでGUIで変更できません。
4. **Dashboards → New → Import dashboard** を開き、checkoutの `infra/monitoring/dashboards/foundation.json` をUploadします。
5. UID `lakehouse-operations` とDatasourceを確認して **Import**。既存UIDとの上書き確認が出たら、初回ではないので取り消し、既存Dashboardを開きます。
6. Dashboardを再読み込みして表示を確認します。既存UIDができた後のCLI `grafana-init` は、そのGUI保存内容を保持します。

Grafana.comのIDや外部URLを入力しなくても、同梱JSONだけで初期画面を作れます。企業proxy経由の外部Dashboard取得も不要です。[Grafana公式のImport手順](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/import-dashboards/)

テンプレートは `editable: true` のclassic dashboard JSONです。インポートにはGrafana 13の新APIを使い、`metadata.name` にUID、`spec` にダッシュボードのモデルを渡します。旧 `/api/dashboards/db` へのfallbackは実装しません。[Dashboard API](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/dashboard/)

## GUIで編集する

Grafanaで `Lakehouse Operations` を開き、パネル、配置、PromQL、表示期間等を編集してSaveします。保存後はGrafana DBの内容が正本です。コンテナを再起動・再作成しても、同じ名前付きvolumeを使う限り保存先を保持します。

### 1つのパネルを編集して保存する

1. Dashboardの **Edit** を押し、対象パネルのメニューから **Edit** を開きます。
2. DatasourceはPrometheusのままにし、QueriesをCode表示にして式を確認します。初回は `lakehouse_successful_runs` のStat等、小さな変更から試します。
3. パネルのTitleや表示形式を変えてパネル編集を終了し、Dashboardの **Save** を押します。確認画面が出た場合は説明を入力してもう一度Saveします。
4. **Exit edit** から閲覧へ戻り、再読み込みして変更が残るか確認します。
5. 時刻やrefreshを変える場合は **Edit → Dashboard options → View all settings** で設定し、同じように保存します。[公式のDashboard settings](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/modify-dashboard-settings/)

最初のジョブ前は件数0、`lakehouse_last_success` が0のことがあります。表示例の件数を固定期待値にせず、自分で実行したジョブの前後差を確認します。

| 確認する式 | 意味・用途 |
| --- | --- |
| `up{job="lakehouse"}` | workerのmetrics収集が成功しているか |
| `lakehouse_ready` | worker初期化が完了したか |
| `lakehouse_blocked` | Trino中止未確認のため新しい処理を止めているか |
| `lakehouse_successful_runs` / `lakehouse_failed_runs` | 現workerプロセスの成功・失敗件数 |

`No data` の場合は、Prometheusの `/targets` でjob `lakehouse` がUPかを先に確認します。収集は15秒周期です。これらはジョブ運用の指標で、Goldの歩留まりやOEEを表すものではありません。生産KPIは [Superset](POC-TOOLS.md) を使います。

確認するときは、GUIでタイトルやパネルを変更して保存し、Grafanaを再起動した後に同じダッシュボードを開きます。その後 `grafana-init` を再実行し、保存した変更が残っていることを確認します。サイトでの実行結果は検証記録へ残してください。

既存の別UIDのダッシュボードは、初期化スクリプトで削除・変換しません。以前のfile provisioningで作ったものが残っている場合は、新しい編集可能なダッシュボードと区別して扱います。対象UIDを手動で削除した後に初期化を実行すると、テンプレートから新規作成されます。

## GUIの内容をJSONファイルへ残す

初期ダッシュボードの現在の内容を、Git外のprivateファイルへ書き出せます。

```bash
python3 scripts/grafana_setup.py export \
  --output "$HOME/.config/lakehouse-foundation/grafana/dashboard-review.json"
```

`LAKEHOUSE_ENV_FILE` を使わない場合:

```bash
python3 scripts/grafana_setup.py \
  --env-file "$HOME/.config/lakehouse-foundation/site.env" export \
  --output "$HOME/.config/lakehouse-foundation/grafana/dashboard-review.json"
```

exportは読み取り専用です。出力先はリポジトリ外の絶対パスに限定し、親ディレクトリは0700、出力ファイルは0600で保持します。既存ファイルを上書きせず、公開ソースのテンプレートへ直接書き込みません。ダッシュボードのパスワードやAPI応答本文もコンソールへ出しません。

出力するのは現在のダッシュボードモデルで、GUIで変更したtitle、panels、queries等を含みます。実際の接続先、業務上の値、個人情報が含まれ得るため、公開用に残す内容をレビューします。テンプレートへ反映する場合も、初回導入用の見本の更新として扱い、既存環境へ自動同期しません。

GUIで新たに作った別UIDのダッシュボードは、GrafanaのJSON export機能で控えを保存できます。このCLI exportの対象は初期UID `lakehouse-operations` です。

GUI exportはDashboardの **Export → Export as code → Advanced options** で **Model: Classic** を選び、**Download file** でprivateな保存先へダウンロードします。V2 Resourceを選ぶ場合はAPI resource全体になるため、復元先と形式を合わせます。同梱CLI exportは `spec` からclassicモデルを取り出してUIDを戻す実装で、API応答のresource全体をそのままテンプレートへ置く操作ではありません。[公式のexport操作](https://grafana.com/docs/grafana/latest/visualizations/dashboards/share-dashboards-panels/)

## 認証とバックアップ

初期化とCLI exportは、private envの `GRAFANA_ADMIN_PASSWORD` と管理者 `admin` によるBasic認証を使います。認証情報をURLへ埋め込まず、対象のローカル／private Grafanaへ直接接続します。proxyとHTTP redirectを経由して資格情報を転送しません。[Grafana OSSのAPI認証](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/authentication/)

GUIで管理者パスワードを変更した場合は、private設定も現在の値へ合わせます。永続DBがある状態でComposeの初期パスワード環境変数だけを変更しても、既存の管理者パスワードがその値へ変更されるとは限りません。

GUI内容の永続化にはGrafanaの名前付きvolumeが必要です。JSON exportはダッシュボードの控えであり、利用者、権限、datasourceの資格情報等を含むGrafana全体のバックアップとは別です。データ移行時はvolume、現在の認証情報、必要なバックアップと復元手順を保持します。

## 今回照合した一次資料と実装

- [Grafana 13.3.0 release](https://github.com/grafana/grafana/releases/tag/v13.3.0) と [同tagのDashboard APIコード](https://github.com/grafana/grafana/blob/v13.3.0/pkg/api/dashboard.go)。採用版を確認しました。
- [Dashboard API](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/dashboard/)。照合時の文書はv13.3を表示。キットの使用先は `/apis/dashboard.grafana.app/v1/namespaces/default/dashboards` で、legacy APIやv1beta1へのfallbackはありません。
- [Import](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/import-dashboards/)、[GUI設定](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/modify-dashboard-settings/)、[OSS API認証](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/authentication/)。一般のService Account Token対応と、同梱CLIのBasic認証を区別しました。
- [Prometheus 3.16のPromQL](https://prometheus.io/docs/prometheus/3.16/querying/basics/) と `prometheus.yml` / `foundation.json` / `grafana_setup.py`。収集先、表示式、create-only初期化とprivate exportを照合しました。
