# 段階的な監視基盤の配置と受入

<!-- doc-purpose:start -->
> **役割:** 監視のhost権限、create-only GUI、閾値、検証と実機受入を分けて段階導入する。
>
> **対象読者:** 基盤構築担当 / 運用担当 / 評価・報告担当
>
> **読むタイミング:** monitoringを追加・更新し、host mountと未検証項目を確認するとき。
>
> **前提となる資料:** [サイト設定リファレンス](<CONFIGURATION.md>) / [Dokploy 0.31.0でGitから配置する](<DOKPLOY.md>)
>
> **次に読む資料:** [GrafanaのGUI編集と保存](<GRAFANA.md>) / [平時・更新・復旧の運用手順](<OPERATIONS.md>) / [OpenObserveを任意の比較Labとして追加する](<OPENOBSERVE.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

既存のPrometheus/Grafanaへ監視対象を追加するための手順です。この変更は**構成コードの追加**です。ホストの読取り権限、Docker 29/cgroup v2での指標、実ダッシュボード、容量・性能は配置前後の別受入が必要です。現在の設定を反映するために、この資料の確認だけでDeployしないでください。

## 1. 既存の監視と追加する権限

`monitoring` はPrometheus・Grafana・Loki・Vectorの4serviceです。host root/PIDを読むNode ExporterとcAdvisorは別opt-inの `monitoring-host` へ分離しています。`monitoring-host` は `monitoring` が必須で、旧monitoringの再配置だけでは有効になりません。core-onlyは6サービスのままです。exporterにhost公開portはなく、PrometheusはCompose内部DNSへ15秒間隔、10秒timeoutで接続します。既存のworker/self job、15日/2GB保持、Grafana/Prometheus volumeは維持します。通知先は設定しません。

| 追加対象 | 初期メモリ上限 | 読取り・制約 | 未確認の受入項目 |
| --- | --- | --- | --- |
| Node Exporter 1.12.1 | 128MiB | host `/` を `/host` へreadonly、`pid: host`、非root、capabilityなし | CPU・memory・disk・process値をhost観測と照合 |
| cAdvisor 0.60.6 | 256MiB | rootfs/sys/DockerRootDir readonly、非root、socket・device・privilegedなし | Docker 29/cgroup v2でのcontainer指標・Composeラベル |

root読取りは機密ファイルを含む範囲へのアクセスです。readonlyは情報漏えいの防止を保証しません。**operatorがmountと用途を承認してから**既存managerで配置します。host network、D-Bus、`/dev/kmsg`、追加capability、Docker socketを追加してpanelを埋める手順は提供しません。socketの`:ro`はDocker APIの権限を制限しません。

Node Exporterはcollectorのallowlistを使用します。root/PIDを渡してもnetlinkや`/proc/net`がhost network namespaceを示すとは限らないため、network/TCP/systemdは無効です。対応するpanelは未提供を明示します。cAdvisorにComposeラベルが出ない場合、selectorは空になります。架空のラベルやゼロ値で補いません。厳密なcontainer別監視が必要なら、socket-free受入結果を確認し、別途権限設計を判断します。

## 2. 配置前の確認

通常監視でも専用 `MONITORING_LOG_DIR` が事前に必要です。spoolの存在、path/全祖先がsymlinkでないこと、`WORKER_UID:GID` と一致する所有者、mode 0700または0750、親directoryの安全性とVectorの通過権限を確認します。未作成なら配置を止め、host管理者による別承認の準備を待ちます。更新手順からmkdir・自動chown/chmodはしません。[既存環境のpreflightとregistry確認](COMPANY-UPDATE.md#監視を更新する前の追加preflight)

承認済みcommitと既存project/volume prefixを確認します。Git外private設定は既存値を保持し、`configure.py --upgrade` が必要な場合だけ使用します。同名backupがあると停止する既存保護を維持します。[更新手順](COMPANY-UPDATE.md)

`monitoring-host` を使う場合だけ、Dockerの保存先を確認します。保存先は仮定しません。operatorが対象hostで確認し、`CADVISOR_DOCKER_DATA_ROOT` へ絶対パスを登録します。

**Bash / Zsh共通**

```bash
# 読取りのみ。結果はprivate設定へ保存し、公開ログやGitへ貼らない。
docker info --format '{{.DockerRootDir}}'
```

未指定時のCompose fallbackは空の公開placeholderであり、Docker保存先の代替ではありません。実際の保存先・アクセス・metadataの受入が済むまでcontainer監視を成功扱いにしません。既存のNotebook/landing/private設定をmount先に流用しません。

通常の`plan`は設定検査だけでhostの存在・modeまでは調べません。CLIの `up` / `predeploy` はmonitoring有効時、Composeを呼ぶ前にspoolのmetadataを検査し、不一致なら停止します。directory作成・権限変更・書込みprobe・ログ内容/全ファイル一覧の検査は行いません。monitoring無効時はspoolのhost要件を追加しません。Notebook検査と、`monitoring-host` のhost root/collector権限確認も別に扱います。raw Dokploy Composeも同じ9ファイルを結合します。新しいlifecycle managerを追加しません。[Dokploy](DOKPLOY.md)

## 3. GUIのダッシュボード

`grafana-init` は固定UIDが**存在しない時だけPOST**します。既存のoperationsやGUI編集済みUIDをPUT/PATCHで上書きしません。複数UIDの一部だけ失敗した場合、成功/既存/失敗を個別に返し、CLIは非0で終了します。collector/loggingの停止をGrafana起動の依存条件にしません。

| UID | 内容 |
| --- | --- |
| `lakehouse-operations` | 従来のworker中心の操作監視。既存GUI内容を維持 |
| `lakehouse-host-overview` | CPU・load・memory/cache/buffers・swap・disk/IOPS・filesystem/inode・uptime・process |
| `lakehouse-container-overview` | 実際のCompose project/service/nameラベルから選択。CPU・working set・有限limit比・network・filesystem・OOM・start-time変化 |

start-time変化はrestart counterではありません。JVM working setの高さだけで障害としません。Trinoは85%警告から除外し、95%超と**実際に出た**OOMまたはstart-time変化が揃う場合だけ相関alertを出します。証拠metricがないことは、OOMがゼロという意味ではありません。

全テンプレートは同梱のbuilt-in panel/datasourceだけを使用します。出典はこのリポジトリ独自のquery/templateです。grafana.comからdashboard/pluginを取得しません。Grafana 13.3の公式設定に基づきplugin preinstall自動更新とGUIからのplugin管理を停止します。署名検証を無効にしません。新規templateの再生成は `python3 scripts/monitoring_assets.py`、照合は `--check` です。既存GUI dashboardには自動反映されません。

## 4. アラートと検証の境界

CPUは5分平均80%/10分平均90%と、その後の2分継続を分けています。MemAvailable 3GiB/1.5GiB未満、swap使用2GiB/3GiB超、filesystem/inode 80%/90%超、exporter停止2分/5分、有限container limit 85%/95%を初期ルールとします。これは実測容量の推奨値ではありません。zero/unlimited limitとroot/aggregate cgroupは比率判定へ混入させません。

Pythonのunit/static testはPromQLや実imageを実行しません。公式の固定Prometheus imageを使うvalidatorは次のように別実行します。既定はplanのみです。

**Bash / Zsh共通**

```bash
python3 scripts/validate_observability.py
# disposable Docker実行が許可された検証host/CIでのみ実行する。
python3 scripts/validate_observability.py --run
```

validatorはnetworkなし・bind mountなし・capabilityなし・非rootの一時containerへ**公開設定だけ**をコピーします。停止中のCOPYのため一時rootfsは書込み可能で、host mountはありません。失敗時は未検証のまま残し、無効なルールを運用へ適用しません。ローカル作業環境ではDocker APIへの接続がsandboxにより拒否され、ネイティブvalidatorは未実行でした。別のGitHub Actionsでは、公式Prometheus imageのconfig・rule・fixture検査とLoki imageのconfig検査が成功した記録があります。Prometheusの成功はhost profile分離前の静的nodes構成に対するもので、分離後のDNS/profile切替を含む構成は再検証待ちです。これはnetworkなしの公開設定検証であり、実hostのmount・exporter指標・ログ配送・GUI・24時間運転の受入ではありません。Vectorのネイティブ検査は別の結果として追跡します。

## 5. 受入・容量・ロールバック

配置許可後にのみ、対象hostの観測とCPU/RAM/filesystem/IO、実ラベル、各`up`、alertとGUIを照合します。既存operationsをGUI編集してから通常Redeployし、その内容が残ることを確認します。collector停止中もworker・既存metricsが利用できることを確認します。会社DBや業務tableへ書込みません。

通常 `monitoring` はPrometheus384MiB＋Grafana384MiB＋Loki512MiB＋Vector512MiB＝**上限計1792MiB**です。明示opt-inの `monitoring-host` はexporter128/256MiBの**追加384MiB**で、両方を合わせると**2176MiB**です。使用RSSの実測ではありません。host読取り・build・OS/cache・各sinkの512MiB disk bufferと専用capture最大1GiBは別途必要です。MemAvailableが3GiB未満またはswapが継続増加する場合、追加labの配置を止めて影響を報告します。自動的に他サービスの制限を変更しません。

rollbackは承認済みcode/configを既存managerから戻し、新規serviceだけを停止するoperator手順です。Prometheus/Grafana/notebook/event/work/data volume、private設定、GUI内容を削除しません。`down -v`、prune、daemon再起動、再帰chown/chmod、firewall/public bind変更は通常手順に含めません。

## 6. 版と一次資料

- [Node Exporter v1.12.1](https://github.com/prometheus/node_exporter/tree/v1.12.1)・[公式image index](https://github.com/prometheus/node_exporter/pkgs/container/node-exporter/1029141164)
- [cAdvisor v0.60.6 flags](https://github.com/google/cadvisor/blob/v0.60.6/docs/runtime_options.md)・[公式image index](https://github.com/google/cadvisor/pkgs/container/cadvisor/1267507127)
- [Grafana v13.3.0 defaults](https://github.com/grafana/grafana/blob/v13.3.0/conf/defaults.ini)
- [Prometheus rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)・[rule fixtures](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)

image lockのdigestは公式registry公開metadataとの照合です。対象imageのpull/実行・機能互換性の実測とは区別します。

ログ整形・収集権限は [Vector](VECTOR.md)、保存・検索・保持期間は [Loki](LOKI.md) を確認します。通常監視はLoki-onlyで、継続capture・実配送・公式VRL実行は未受入です。
