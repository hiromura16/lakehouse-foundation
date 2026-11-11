# SailとSparkを同じ合成データで比較する

<!-- doc-purpose:start -->
> **役割:** 任意のSail LabでSparkと合成データの型・結果・再実行を比較し、未検証事項と測定条件を記録する。
>
> **対象読者:** 開発・設計担当 / 基盤構築担当
>
> **読むタイミング:** Sparkの既存検証後、独立したnamespaceと有限fixtureで別エンジンの比較を準備するとき。
>
> **前提となる資料:** [バージョンと連携互換性](<COMPATIBILITY.md>) / [導入・検証・停止](<DEPLOYMENT.md>)
>
> **次に読む資料:** [検証記録](<VALIDATION.md>) / [平時・更新・復旧の運用手順](<OPERATIONS.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

Sail Labは`COMPOSE_PROFILES`の`compute,sail-lab`を明示的に選ぶ比較用機能です。
既存Spark、Python worker、SQL Server/CDC、通常のGold表はそのまま使います。
この実装はソース・合成fixture・模擬HTTP/エンジンの検証を提供します。
コンテナbuild、Spark Connect接続、Lakekeeper/SeaweedFSへの実書込み、Trino照合、
再起動、冷温各試行、資源・性能測定、会社受入は未実施です。
`plan`の成功、mockテストの成功を連携や性能の成功として報告しません。

## 実装したものと境界

| 要素 | 実装・現在の状態 |
| --- | --- |
| Sail server | 公式PySail **0.7.3** Linux wheelをhash固定したローカルimage。Rustをコンパイルしない |
| クライアント | 既存Spark **4.1.4** imageから派生し、Connect依存だけをhash固定で追加。PySparkを再インストールしない |
| RPC | Compose内の`sail-server:50051`。host port公開なし |
| healthcheck | Pythonのloopback TCP接続。livenessであり、Connect protocol/catalogの証明ではない |
| Catalog | Sail公式`iceberg-rest`設定。`http://catalog:8181/catalog`と既存warehouseを利用 |
| S3 | Sail公式`AWS_ENDPOINT`等へ既存`S3_ACCESS_KEY`/`S3_SECRET_KEY`をprivateに変換 |
| 入力 | seed 42、20指図、7日、6種類のERP/MES、**145 raw行**の固定合成fixture |
| 出力 | `spark_lab`/`sail_lab`各13表。Bronze 6、Silver 6、工場日別Gold相当1 |
| 実行 | `sail-benchmark`はreplicas 0。通常Deployでは素材や表を作らない |
| 資源 | server初期budget 2 CPU/2 GiB。client/JVM分も別途計上する。実測値ではない |
| 自動削除 | なし。lab表、snapshot、証跡volumeを保持する |

上流0.7.3のテスト行列にはSpark 4.1.3と4.2.0等があります。
当基盤の4.1.4とのprotocol互換性は未確認です。
公式deploymentガイドのPoC向けwheel方式を使い、未確認の公式container digestを作りません。
serverのamd64/arm64 wheelと、client追加依存のCPython 3.10 Linux wheelを検査済みhashに限定します。
client元imageのPython ABIが異なる場合は安全にbuildが失敗します。実際のABI/build確認は受入項目です。
既存Python/uv/Sparkのbase image digestは[lock](../infra/lakehouse/images.lock.json)を継承します。

## 入力と業務意味

[fixture.json](../infra/sail/fixture.json)は既存Demo generatorのcleanデータに、
日本語、NULLと空文字、正確な大きいdecimal、UTC以外のoffset、100 ns、同一イベントの重複を加えた合成素材です。
[SHA-256](../infra/sail/fixture.sha256)を両エンジンの実行前に照合します。
fixtureはコードレビュー対象の固定入力です。APIで任意の会社データや任意SQLを指定する機能はありません。
145行は機能確認用の小さい業務形状fixtureです。大規模負荷や性能代表性の証明ではありません。

共通の[SQL recipeとPython oracle](../infra/sail/contract.py)は次を実施します。

1. 六つの素材をBronze表へ保存。Bronzeの重複数を保持します。
2. 固定business keyと更新時刻でSilverの最新revisionを採用。同一revisionの矛盾を入力時に拒否します。
3. 計画・生産・停止をそれぞれ工場/UTC日で集計してから結合。明細同士のJOINによる掛け算を避けます。
4. 歩留まりは良品/全生産、計画達成率は良品/計画数。分母0はNULLです。
5. 停止はUTC開始日に全量を割り当てます。区間重複の統合や日跨ぎ分割を行いません。

型の扱いには意図的な差があります。通常GoldはDOUBLEの表示値ですが、labの率は`decimal(18,2)`、
停止分は`decimal(28,9)`で明示的に丸めます。正確な停止長は`downtime_ticks_100ns`にも保持します。
これは同じ工場日別の業務意味を検査する比較recipeで、既存medallion SQLを無変更で実行する主張ではありません。
受注・出荷等のSilverも全業務列を照合しますが、今回のGold比較対象は工場日別1表です。

### datetime2(7)を成功したことにしない

Sail 0.7.3公式資料では、Icebergの`timestamp_ns`変換とSpark Connectのresult-schema対応は別です。
後者は未対応と明記されています。通常Spark/Iceberg v2のmicrosecond型へ切り捨てて一致させません。
当labは日時を**UTCの7桁小数文字列と100 ns整数ticks**として比較し、offsetも正確に正規化します。
原素材の元offset表記は固定fixtureに保持します。Bronze列の日時表現は、両エンジンに渡す前に同じ規則で正規化します。
Python datetimeやfloatに7桁の小数を通さず、整数秒と小数を分けます。

次は意図した失敗(exit 2)になる、ネット接続なしの契約probeです。
実エンジンに投げたSQLの失敗を再現したものではありません。

**Bash / Zsh共通**

```bash
python3 scripts/sail_benchmark.py plan
python3 scripts/sail_benchmark.py timestamp7-probe
```

native datetime2(7)/Iceberg timestampの連携受入を満たしたとは記録しません。
この制約が比較目的に適さない場合は、labを有効化せず型対応の更新を待ちます。

## 実書込み前の許可と準備

この節は後日、所有者が許可した合成テスト環境だけで実施するoperator手順です。
会社DBへ接続せず、Sail/Spark共に既存ソースDB資格情報を必要としません。
既存のlifecycle managerを維持し、準備用CLIから`up`/`down`を実行しません。
監視/OpenObserveとSailを同時に増設せず、MemAvailable 3 GiB以上、継続Swap増加なしを確認してください。

1. 承認済commit、既存project名、既存private設定を確認。必要時だけ衝突防止付き`configure --upgrade`を使います。
2. 通常Sparkの`spark-check`を同じprojectで明示実行し、その結果を保存します。3行の互換fixtureと本labは別物です。
3. `compute,sail-lab`追加のメモリ/CPU、named volume、private S3設定、loopback/internal通信、CA trustをレビューします。
4. Spark image ownerを先にbuildし、派生clientをbuildします。cold cacheではこの順序が必須です。
5. 所有者が許可した後、既存managerの通常Deployでserverを追加します。fixture投入は自動起動しません。
6. HTTP/TCP healthだけでなく、次の明示runでConnect・catalog・S3・Trinoを検査します。

Sailの追加定義は[専用Compose](../infra/sail/compose.yaml)です。CLIとDokployでは、この定義を含めた標準9ファイルを同じprojectへ結合します。
次はproject名とprivate設定を**ローカルで選んでから**作る共通引数です。
既存volume prefixと一致しないprojectを作らないでください。

**Bash / Zsh共通**

```bash
ENV_FILE='<private-env-file>'
PROJECT='<project-name>'
compose_args=(
  --project-name "$PROJECT" --env-file "$ENV_FILE"
  -f infra/lakehouse/compose.yaml -f infra/compute/compose.yaml
  -f infra/orchestration/compose.yaml -f infra/demo/compose.yaml
  -f infra/cdc/compose.yaml -f infra/poc/compose.yaml
  -f infra/mcp/compose.yaml -f infra/openobserve/compose.yaml -f infra/sail/compose.yaml --profile compute --profile sail-lab
)
docker compose "${compose_args[@]}" config --quiet
```

以下のbuildはホストのimage/cacheを変更し、ネットワーク取得を伴います。適用許可後だけ実施します。
共通app tagをremote registryからpullせず、Dockerfile FROM/依存の取得は従来のproxy/CAを利用します。

**Bash / Zsh共通**

```bash
docker compose "${compose_args[@]}" build spark-volumes-init
docker compose "${compose_args[@]}" build sail-server sail-benchmark
```

serverはroot/socket/device/host rootを使いません。clientのstate volume rootはimageで非root用に準備します。
既存volumeのowner/modeが違う場合、runnerは修復せず拒否します。実hostへのchown/chmodは手順に含めません。
S3/RESTのnative TLSで社内CAが認識されることは実際に検証してください。TLS検証を外して解決しません。
当基盤内の既定endpointはHTTPです。外部catalog auth/credential vending/remotesigningを確認なしで流用しません。

## 明示実行、比較、証跡

実行コマンドは**書込み操作**です。`--approve-lab-write`は対象合成namespaceの書込みへの明示同意です。
任意schema/table/SQL/endpointは受け付けません。
RESTでnamespaceの`lab.owner`と各表の同じmarkerを検証し、存在しないnamespaceだけを新規作成します。
既存のmarkerなしnamespaceや表は自動採用・修復しません。各overwriteの直前にも所有権を読みます。
公式v0.7.3の[DDL対応表](https://github.com/lakehq/sail/blob/v0.7.3/docs/guide/catalog/index.md#iceberg)は
Iceberg RESTのCTASを対応とし、[機能一覧](https://github.com/lakehq/sail/blob/v0.7.3/docs/guide/sources/iceberg/features.md)は
catalog経由commitとfull overwriteを対応としています。これは上流の対応表であり、当基盤での実書込み成功ではありません。
RESTで未対応の`CREATE OR REPLACE`を使わず、初回CREATE→所有済表のINSERT OVERWRITEを使います。
13表のcommitは表単位で、全表同時のtransactionではありません。部分失敗の表は保持して状態を確認します。

専用volumeの0600 lockとdurable実行markerで並列実行を防ぎます。
クラッシュ・query失敗・stop失敗後はmarkerを残して拒否します。client停止だけでremote処理終了を保証しません。
operatorが対象containerとserverの処理終了を独立確認してから、markerの扱いをレビューしてください。
自動解除や強制的な再試行、広域table削除はありません。

以下は承認後の例です。証跡directoryはGit外の既存0700 private directoryを選びます。
`umask 077`と上書き拒否を使い、既存証跡を潰しません。JVMログを公開せず、必要なtag付きJSONだけレビューします。
client処理はserverに依存するため、実行を監督し、事前に同意した時間上限を超えたらoperatorが停止/残存jobを確認します。
現段階でSpark Connect全APIに共通の総deadlineを保証するものではありません。

**Bash / Zsh共通**

```bash
EVIDENCE_DIR='<private-evidence-directory>'
umask 077
set -C
docker compose "${compose_args[@]}" run --rm --no-deps sail-benchmark \
  run --engine spark --approve-lab-write --trial 1 --cache-state unknown \
  > "$EVIDENCE_DIR/spark-trial-01.json"
docker compose "${compose_args[@]}" run --rm --no-deps sail-benchmark \
  run --engine sail --approve-lab-write --trial 1 --cache-state unknown \
  > "$EVIDENCE_DIR/sail-trial-01.json"
python3 scripts/sail_benchmark.py compare \
  --spark-report "$EVIDENCE_DIR/spark-trial-01.json" \
  --sail-report "$EVIDENCE_DIR/sail-trial-01.json"
```

結果は`LAKEHOUSE_SAIL_RESULT=`で始まるversion 1 JSONです。
実行ID、UTC開始/終了、経過秒、設定版と観測版、fixture checksum、stage、
13表のcount/全行multiset hash、Iceberg format/schema/nullability/snapshot、Trino照合、replay、cleanupを記録します。
不足するimage identity/commit/dirty status/CPU/RAM/spillは`unknown`/NULLで保持します。
任意のS3/JDBC URL、SQL例外、proxy、CA内容、接続secret、実ログ本文をJSONへ入れません。
`compare`は各結果をPython oracleと独立照合し、両方が同じ間違いを出しただけの一致を防ぎます。
snapshot IDは別表なので一致を求めず、format/schema/nullability/業務列と重複数を一致させます。
hashに加えて全canonical行をmultiset比較し、単体関数では最大5件の**合成行**差分を返せます。
通常CLIへ行内容は出しません。結果ファイルの署名/実行証明は提供せず、独立レビューも必要です。

## 性能・資源受入は別の実測

Sail serverとclient合計、Spark client/JVMのCPU/RAM/storageを同じ総割当で測ります。
serverだけの2 GiBをSpark全体と比べる測定は公平ではありません。
同じimmutable入力、同じcache条件、cold/warmを分離し、各条件を最低3回反復します。
image cacheや業務volumeを削除してcoldにする手順は使いません。隔離環境の状態を記録します。

| 指標 | 今回の状態 | 後日の記録 |
| --- | --- | --- |
| 起動・job秒 | runtime未実施 | 生の試行値、平均、ばらつき、cache条件、client/server別 |
| CPU/RAM average/max | 未測定 | 同じ観測区間、測定元、資源上限、sample数 |
| spill・snapshot・error | runtime未実施 | engineが実際に提供する値と未提供を区別 |
| exact equality/Trino/replay | mock契約を検査、実連携未実施 | 全13表、Unicode/NULL/decimal/UTC7/重複数 |
| native timestamp7 | 文書上の制約をnegative probe | 未対応を成功扱いしない |
| 改善判定 | 未判定 | 起動/メモリ/時間の少なくとも一つで実際に改善したか |

遅い・互換性がない結果も有効な結論です。Sparkを削除したり、Sail側の書込みをSparkで代行したりしません。
受入が揃うまで本番Gold置換、性能向上、zero-code-changeを主張しません。

## 停止とロールバック

所有者承認後、既存managerで追加したSailサービスだけを停止し、`sail-lab`を外します。
Spark、notebooks、通常Gold、SQL/CDC、private設定、lab表・volume・証跡を保持します。
volume削除、prune、table purge、広域namespace DROP、再帰chown/chmodは不要です。
不確実なjob/markerはserver/client双方の終了を確認して別途レビューします。
base imageやSail版を戻す場合、保存データのformat互換性と証跡を確認します。

## 固定した公式資料と検証範囲

[上流証拠一覧](../infra/sail/upstream-evidence.json)に、2026-11-11に照合したrelease、wheel hash、
publishing commitと未実施runtimeを記録します。公式Sail container digestは未確認なのでNULLです。

- [Sail v0.7.3 release](https://github.com/lakehq/sail/releases/tag/v0.7.3)
- [公式wheel方式のPoC Docker image](https://github.com/lakehq/sail/blob/v0.7.3/docs/guide/deployment/docker-images/quick-start.md)
- [PySail 0.7.3の配布物・hash・attestation](https://pypi.org/project/pysail/0.7.3/)
- [v0.7.3 Sparkクライアントの試験行列](https://github.com/lakehq/sail/blob/v0.7.3/pyproject.toml)
- [v0.7.3 REST catalogとconfigの優先順](https://github.com/lakehq/sail/blob/v0.7.3/docs/guide/catalog/iceberg-rest.md)
- [catalog別Iceberg DDL対応表](https://github.com/lakehq/sail/blob/v0.7.3/docs/guide/catalog/index.md#iceberg)
- [Iceberg read/write・timestamp_ns制限](https://github.com/lakehq/sail/blob/v0.7.3/docs/guide/sources/iceberg/features.md)
- [S3互換endpointと環境変数](https://github.com/lakehq/sail/blob/v0.7.3/docs/guide/storage/s3.md)
- [Spark 4.1.4 Connect依存metadata](https://pypi.org/pypi/pyspark/4.1.4/json)
- [Spark v4.1.4が定義するConnect依存の下限](https://github.com/apache/spark/blob/v4.1.4/python/packaging/classic/setup.py)

ソース検証は`python -m unittest tests.test_sail_lab`で実行します。
fixture/比較、入力拒否、mock REST/Trino、所有権、replay失敗、stop失敗、private証跡、
owned一時directoryのLinux lock/symlink/hardlinkを検査します。
wheel download/install、image platform実行、protocol、SQL feature、S3、Trino、再起動、資源測定は別の未実施受入です。
