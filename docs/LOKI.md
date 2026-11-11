# Lokiの保存・検索・保持期間

<!-- doc-purpose:start -->
> **役割:** Lokiの専用保存・7日保持・Grafana検索・reject・復元の境界を確認する。
>
> **対象読者:** 基盤構築担当 / 運用担当 / 開発・設計担当
>
> **読むタイミング:** ログ収集の設定・合成試験・機密境界を確認するとき。
>
> **前提となる資料:** [段階的な監視基盤の配置と受入](<OBSERVABILITY.md>)
>
> **次に読む資料:** [症状から確認する](<TROUBLESHOOTING.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

Loki 3.7.9を単一binary・filesystem・専用named volumeで使用します。通常の`monitoring`へ追加しますが、新規host権限と実機受入は [段階導入](OBSERVABILITY.md) で承認してから配置します。Vectorは [秘匿化と配送境界](VECTOR.md) に従います。実環境への配置やログ取込は、この実装作業では行っていません。

## 構成

Compose内部の`http://loki:3100`だけを使用し、host 3100は公開しません。Grafanaがqueryし、Vectorがwriteします。private Compose networkは「Prometheusだけに接続を許すACL」ではありません。host/public endpoint、Basic認証、追加MinIO、memberlist、分散replicaは追加しません。runtime HTTP(S) proxyは渡しません。

`loki` volumeは既存projectまたは明示DATA_VOLUME_PREFIXの名前を使います。TSDB/v13 schema・24時間index・filesystem chunks/index/cache/compactorを`/loki`へ保存します。rootfs readonly、非root10001、capabilityなしで、初期budgetは512MiB/0.5CPUです。1GiB等への変更は実測と承認に基づいて判断します。budgetは使用量の実証ではありません。

公式distroless imageにshell/curlを仮定しません。選択版が実装する`/usr/bin/loki -health -health.url=http://127.0.0.1:3100/ready`をhealthcheckとします。config検査は`-config.file=... -verify-config=true`です。公式sourceでflagを確認しましたが、実imageでの実行は未検証です。digestは公式publisherの固定Compose sourceから採用し、直接registry応答との再照合は未了です。[pin出典](https://github.com/grafana/loki/blob/4c66e9a54851299caa1d7469e02aa398b3296807/production/docker/docker-compose.yaml)

## 保持期間・reject

既定は168時間＝7日です。schema v13/TSDBとcompactor retentionを組み合わせ、delete delayは2時間です。14日/30日へ変える場合はretention、old-sample上限、query期間、容量・backlogを揃えてレビューします。設定期間ちょうどで全disk使用が消える保証ではなく、index/chunks/WAL/compactionとdelete delayの余力が必要です。

7日より古い時刻、10分を超える未来時刻、64KiB超lineはrejectする設定です。size/time制限を一括無効にしてエラーを隠しません。Vector側の最終messageも有界です。`loki_discarded_samples_total`のreason、HTTP error、transport側のdiscard/retryを照合します。長期buffer再送は元時刻が古くなり受理されない場合があります。

`discover_log_levels:false`としてLokiの本文keyword推測を停止します。Vectorの`level` labelとJSON `level/level_normalized`を一致させます。Grafana 13.3の実際の色・field extraction・unknown表示は後日受入で確認し、labelがあるだけで表示の正しさを保証しません。

## Grafanaの操作

provisionされたdatasourceは`foundation-loki`、type=loki、proxy access、URL=`http://loki:3100`、default=false、editable=falseです。既存`foundation-prometheus`のUID/defaultは変更しません。

`grafana-init`は`lakehouse-logs-overview`がなければ一度だけ作成します。既存operations/host/container/log dashboardはGUI正本として保持します。新規host/containerテンプレートには同じ時刻範囲と一致するproject/service選択を渡すLogs linkを付けます。既存GUI dashboardのlink追加は手動のopt-inです。Exploreも利用でき、追加pluginやLogs Drilldownの外部downloadを要求しません。

1. Grafanaで **Lakehouse Logs Overview** を開きます。
2. project→service→container→levelを選び、keywordを指定します。
3. 5分件数、service別件数、warn/error/fatal数・rate、最新errorと全選択logを比べます。
4. Exploreで同じ`foundation-loki`を使い、時刻・projectを維持して原event fieldを確認します。

全error/warn queryも選択project/service/containerに限定します。別projectのerrorを混ぜてアラート根拠にしません。warning/errorの重大度と、本文のOOM/Traceback/S3 502等の症状分類は別です。例えば既にerror/fatalへ正規化されたworker logからTracebackを検索します。本文だけでlevelをerrorへ変更しません。通知先やrecipientは追加しません。

## no logs / no data の切り分け

| 症状 | 読取りで確認すること |
| --- | --- |
| 全くlogがない | 明示captureを実行したか、正確なproject/owner/service、専用JSONL、Vector UIDのread権限 |
| Vector起動しない | state owner/group/0700、spool bindの存在、公式config validator、profile選択 |
| Lokiがreject | 元timestamp・future/old上限・JSON byte数、reject metricのreason |
| 他projectが混ざる/件数過大 | selected project、実ラベル、再capture/retryの重複、source_event_id衝突の定義 |
| INFOが赤く見える | source level/parser/status、JSON fieldとlabelの一致、Grafanaの実field/color |
| errorが増える | application障害とtransport障害を分け、S3/proxy/CDC等の固定serviceへscopeする |

rawの会社log、Proxy URL、credential、host識別子をissueへ貼りません。private原本を社内に保持し、公開報告はaggregateと合成再現だけにします。

## backup / rollback / 受入

volume backupは停止/整合性の取れる時点、index/chunks/WAL/compactor metadataを一組として保存し、schema/版と復元手順を記録します。単にコピーしたことを復元成功としません。既存Prometheus/Grafana volumeとGUI内容も別に保持します。

rollbackは既存managerから承認されたcode/configを戻し、追加collectorだけを所有者承認のもとで停止します。named volumeを残し、旧Lokiが新schema/dataを読めると確認せずimageだけを戻しません。`down -v`、prune、volume削除、再帰chown/chmod、daemon logging変更は行いません。

公式config validator、VRL実行、合成tagのVector→Loki→scoped query→Grafana、restart重複/欠落、volume persistence、停止時の通常application logging、容量・24時間比較は未実行です。source/static testをこれらの代用として成功扱いにしません。

一次資料: [Loki 3.7.9 source](https://github.com/grafana/loki/tree/v3.7.9)・[filesystem](https://grafana.com/docs/loki/latest/operations/storage/filesystem/)・[retention](https://grafana.com/docs/loki/latest/operations/storage/retention/)・[configuration](https://grafana.com/docs/loki/latest/configure/)。
