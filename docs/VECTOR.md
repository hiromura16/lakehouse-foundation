# Vectorによる対象ログの整形と秘匿化

<!-- doc-purpose:start -->
> **役割:** 対象projectの有限capture・VRL重大度・秘匿化・有界配送と未検証項目を確認する。
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

Vector 0.61.0の完成済み公式バイナリを使用します。Rustサービスや独自コネクタは作りません。設定・VRL・fixtureとPythonによる明示captureをレビューします。Alloy、Promtail、Docker logging driver pluginは使用しません。[構成・権限](OBSERVABILITY.md)・[Loki](LOKI.md)

## 1. 収集範囲と開始条件

標準経路は `明示capture → private専用JSONL → Vector file source → envelope → severity → multiline → redaction → Loki` です。Docker socketやhostログディレクトリをVectorへ渡しません。daemon logging driverを変更しないため、既存の`docker logs`と10MiB×3世代の回転は維持します。

`MONITORING_LOG_DIR` の既定は公開例 `/srv/lakehouse/log-spool` です。会社の実パスではありません。Git外・Notebook/landing/private設定/DockerRootDirから独立した専用ディレクトリをoperatorが選び、作成・所有権・private権限を承認して準備します。この作業で実hostの作成/chown/chmodは実行していません。bindはreadonlyで`create_host_path:false`です。適切なdirectoryがなければ配置は失敗します。

captureは**自動常駐collectorではありません**。明示フラグ付きの有限captureだけを提供し、継続収集のscheduleや実際のDocker metadata/rotation受入は未実施です。hostのDocker APIに接続するcapture実行は、対象hostで別途許可が必要です。collectorへsocketを常設する代替にしません。

`MONITORING_HOST_ALIAS=local`は生成・upgradeされる短い識別名です。実host名を自動取得しません。複数hostを比較する場合は`lab-a`などの架空aliasをprivate設定または設定GUIで選びます。小文字英字で始まる32文字以内の英数字・`_`・`-`だけを受け付け、ログのhost labelへ使用します。

[capture CLI](../scripts/log_capture.py) の`--help`で実行前にscope/上限を確認します。対象は選択private envの**正確なproject**、`app.owner=lakehouse-foundation`、固定service allowlistです。任意host、他project、任意SQL、実データのfixture化、Dokploy/Swarmの全host収集は提供しません。64ファイル×16MiBまでで停止し、自動削除やrotation設定変更をしません。再captureの重複と元ログの回転による欠落は実機受入が必要です。

## 2. 永続化・権限

`vector-data` はprefix付き専用named volumeで、checkpointとdisk bufferを保持します。公式Debian imageからの派生Dockerfileはconfig COPYと**新規volume用のimage-layer metadata**だけを準備します。追加download/apt/Rust buildはありません。runtimeは`WORKER_UID:WORKER_GID`の非root、rootfs readonly、capabilityなし、no-new-privilegesです。

起動wrapperはstate rootのsymlink・owner・group・0700を確認し、不一致なら停止します。既存volumeやhostを修復しません。新規copy-upと既存volume再配置の実動作は未検証です。stateを削除して「直す」運用はしません。内部9598はPrometheus metrics専用で、Vector APIは無効、host portはありません。healthcheckはDebian BashのTCP builtinを使う同梱scriptです。BashはDebian essential packageに基づく想定であり、実image実行の確認は未了です。

## 3. 重大度を本文から推測しない

正規化値は `trace/debug/info/warn/error/fatal/unknown` の7種類です。`level_raw`、`level_normalized`、`level_source`、`level_parser`、`level_parse_status`を保持し、最終`level`はnormalizedと一致させます。

優先順は明示structured field → application別の既知位置 → 文書化された固定prefix → unknownです。stderr、`uvicorn.error`、HTTP 404/500、logger/module/class名、本文中のerror/failed、SQL/topic名、exception fieldの存在だけで重大度を上げません。未知やparse failureはerrorになりません。

[parser matrix](../infra/monitoring/vector/log_parsers.yaml) は配備lockの版、公式参照、合成format例、mapping、multilineとfallbackを記録します。会社の実ログを取得していないためformat対応は**provisional**です。Trino、Python/Uvicorn/portal/CDC、Superset/Gunicorn、Prometheus/Grafana、PostgreSQL、SeaweedFS、Lakekeeper、Redis、Nginx/Traefik/Dokploy、Kafka/Debezium/UI、exporter/Loki/Vector/labを区別します。未知formatを既知として統計の分母に混ぜません。

複数行はcontainer/source/stream/processごとに集約し、最初のrecordのlevel・logger・時刻・identityを継承します。Python最終exception行やJava continuationも対象です。次の新record、1秒timeout、最大32eventで区切り、truncated flagを付けます。最終messageは4096文字へ制限します。UnicodeやJSON escapeによる膨張もLoki64KiB以内へ収める設計ですが、公式VRLでの実行確認は別です。並行traceの混在、rotation、shutdownは未受入です。

## 4. 秘匿化とラベル

Bearer/Basic、password/secret/token/API key、SQL接続文字列、AWS/S3 key（camelCase含む）、Cookie/Set-Cookie、userinfo付きURLを検知したmessageは、取りこぼしを抑えるため全体を秘匿表示へ置換します。logger/raw-level等もallowlistと秘密patternを通します。正規表現だけで任意の機密内容の除去を保証するものではなく、application側の秘密値非出力が第一です。

Loki index labelはjob、sanitized host alias、Compose project/service、安定container alias、stream、normalized levelだけです。実container ID、request/trace/run/user ID、path、SQL、全文error、`source_event_id`はlabelへ入れません。任意metadataは引き継がず、allowlistのeventを作り直して**両sinkへ分岐する前に**秘匿化します。

`source_event_id` はsource identity・元timestamp・stream・元position/contentのhashから一度だけ作り、両sinkへ同じ値を渡します。retryでsink別IDを作り直しません。同じtimestamp/stream/contentが同じposition hashになるcaptureでは同一行が衝突し得ます。比較・重複分析の補助で、exactly-once IDではありません。

## 5. 配送保証と停止時の扱い

各sinkは独立512MiB disk buffer、`drop_newest`、1request同時実行、10秒request timeout、10回retry、backoff初期1秒/待機上限30秒を使用します。30秒は全retryの合計締切ではありません。buffer満杯・retry尽き・元Dockerログの回転・timestamp reject・process停止では欠落し得ます。buffered recordの再送が重複する場合もあります。

source acknowledgementsは無効です。optional sinkの停止がcheckpointや正常sinkを無期限に止めないための**損失を許す有界比較構成**です。有限diskで無期限の無損失と正常sinkの無条件進行を同時保証しません。実際のsink飽和/個別停止/recovery/restartの試験は未実行です。長時間無損失が必要なら、独立spool/readerと容量・権限を別設計として判断します。

内部metricsに加え、bounded service/parser/statusの`lakehouse_log_parse_events_total`、truncated countをlog-to-metricで作ります。実際の受信/送信/破棄/error/buffer metric名とlabelは選択版のexportで照合します。counterの増加はquery visibilityや各recordの受理を保証しません。通常の`monitoring`はLoki-onlyです。OpenObserve側の設定と資格情報はlab選択時だけ読みます。

## 6. 検証の現状

85合成case定義（既知format期待45・秘匿化/metadata期待30・unknown/error/continuation期待10）とpipeline3件、合計88の**native Vector期待テスト**を同梱します。これはテスト定義数であり、成功したparser実測率ではありません。Python/static/有限capture試験と、loggerの秘密assignment・安全なmodule名の期待fixtureを別に検査します。公式`vector validate`/`vector test`はDocker APIがsandboxで拒否されたため未実行です。[公式validator plan](../scripts/validate_observability.py)を許可されたdisposable CIで実行し、通るまで運用受入にしません。

会社での20–50行/serviceのprivate原本→Vector→Loki/OpenObserve→Grafana色/field比較、false error/fatal=0、既知format parse成功99%以上の評価は後日の許可された受入です。分母・未知format除外・失敗数を明記し、少数の合成期待値を達成率として報告しません。

## 7. 一次資料

- [Vector 0.61.0 source](https://github.com/vectordotdev/vector/tree/v0.61.0)・[file source](https://vector.dev/docs/reference/configuration/sources/file/)
- [VRL](https://vector.dev/docs/reference/vrl/)・[reduce](https://vector.dev/docs/reference/configuration/transforms/reduce/)・[unit tests](https://vector.dev/docs/reference/configuration/unit-tests/)
- [Loki sink](https://vector.dev/docs/reference/configuration/sinks/loki/)・[HTTP sink](https://vector.dev/docs/reference/configuration/sinks/http/)・[buffering](https://vector.dev/docs/architecture/buffering-model/)
