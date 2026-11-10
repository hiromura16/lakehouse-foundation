# 用語を役割と実際の操作へ結び付ける

<!-- doc-purpose:start -->
> **役割:** 保存・処理・配置・操作の用語を、同梱実装の役割へ結び付ける。
>
> **対象読者:** 初めて使う人 / データ利用担当
>
> **読むタイミング:** Iceberg、snapshot、profile、jobなどの違いを確認するとき。
>
> **前提となる資料:** なし。必要な用語は本文で確認します。
>
> **次に読む資料:** [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>) / [初めての理解から運用・設計へ進む道順](<LEARNING-PATH.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

初めて読む場合は「保存」「処理」「配置」「操作」の順に確認します。詳しい採用版は [REFERENCES](REFERENCES.md)、実装の全体像は [ARCHITECTURE](ARCHITECTURE.md) にあります。

## データを保存する用語

| 用語 | この基盤での意味 | 間違えやすい点 |
| --- | --- | --- |
| データレイク | ファイルやオブジェクトを蓄積する保存層 | CSVを置いただけで型・品質・業務指標は揃わない |
| データウェアハウス | 整理した表を業務分析に使う考え方 | 特定製品の名前ではない |
| レイクハウス | オブジェクト保存に表のmetadata/commitを加え、複数engineから扱う構成 | S3、Iceberg、catalog、SQL engineは別の役割 |
| S3互換 | S3形式のAPIでオブジェクトを扱うこと。この構成ではSeaweedFS | 全S3実装で全機能・STS・認可が同じとは限らない |
| bucket / object key | 保存領域と、その中のオブジェクト名 | hostディレクトリやSQL表名とは別 |
| raw原本 | 受領したバイト列。CSV/JSON/画像の元の内容 | Bronzeの現在表、CDCイベントとは別の保存表現 |
| Parquet | 列指向のデータファイル形式 | それだけで表のsnapshotやtransactionを管理しない |
| Iceberg | schema、snapshot、manifest等を扱う表形式 | 常駐DBサーバーの製品名ではない |
| REST catalog | 表を探し、metadataの更新を調整するAPI。ここではLakekeeper | 表データそのものはS3にあり、catalog DBだけでは復元できない |
| namespace / schema | catalog内で表をまとめる区分 | raw bucketのディレクトリとは別 |
| snapshot ID | 1つのIceberg表のある版 | ファイルhash、job ID、全表同時の世代IDとは別 |
| main ref | その表で現在使うsnapshotへの参照 | 作成時刻が最も新しいsnapshotと同じとは限らない |

## 素材を整える用語

| 用語 | この基盤での意味 | 実装・操作 |
| --- | --- | --- |
| landing | 完成した素材を受け取るhostディレクトリ | `LANDING_DIR`、Samba/GUI/SQL抽出が出力 |
| ready | コピーが終わった内容のSHA-256を書いた完了marker | `mark-ready.py`。原本が変われば作り直す |
| hash / SHA-256 | バイト列を識別する値 | 同じ内容の確認に使う。データの業務的正しさは証明しない |
| ingest | ready付き素材を原本とBronzeへ取り込むこと | `lakehouse.sh ingest`。派生表生成とは別 |
| Bronze | 元の文字列と由来を保持する層 | 過去の版・重複・不正文字列を最初から消さない |
| Silver | 最新候補の型・値・親参照を検査して採用した業務明細 | 不正な最新版から古い正常版へ戻さない |
| Quarantine | 不正な最新候補と理由を保持する層 | 元ソースを直す。隔離表の手修正は次の変換で失われる |
| Gold | 工場UTC日別の生産、受注別の出荷などの集計 | 粒度・式・分母・時刻を確認して読む |
| grain / 粒度 | 1行が何を表すか | 指図、製造イベント、工場日、受注を混ぜて足さない |
| primary key / 主キー | 同じ業務対象の版をまとめる識別子 | ファイル名、受領順、処理時刻とは別 |
| updated_at | その業務キーの版を比較する更新時刻 | 実際の事象時刻・取り込み時刻と区別 |
| atomic replace | 1表の置換が一括で確定すること | 14SQL全体や6ファイル全体のtransactionではない |
| idempotency / 冪等性 | 同じ処理を再実行しても二重計上しないための性質 | 同じpath/hashのskip、主キーの版採用等を個別に確認 |
| CDC | DBの変更を継続して読むこと | Debezium/Kafkaと専用consumer。有限の素材streamとは別 |
| LSN / offset / checkpoint | DBの変更位置、Kafkaの読取位置、本consumerの処理確定位置 | 保存先・確定順が異なり、同じ数値ではない |
| tombstone | Kafkaで削除を表すために使われるnull valueのrecord | 削除イベント本体と別recordとしてrawへ残り得る |

## 配置・運用の用語

| 用語 | この基盤での意味 | 間違えやすい点 |
| --- | --- | --- |
| image / container | 起動材料と、その実行単位 | image更新と実行中containerの再作成は別 |
| tag / digest | imageの版名と内容を固定する参照 | tagだけの同一名は内容固定の保証にならない |
| Compose project | 一組のサービスを識別する実行名 | 別名でupすると別DB/volumeを作る原因になる |
| profile | 必要な追加機能を選ぶ設定 | 7Composeファイルを結合しても全profileが自動起動するわけではない |
| appName | Dokployの生成後の実行名 | 表示名や作成時の入力slugとは別。suffixを含む実名を使う |
| one-shot | 初期化等を終えて終了コード0で止まるサービス | Exited(0)を異常と判断して再起動し続けない |
| named volume / bind | Dockerが管理する保存領域と、指定host pathのmount | clone先を変えることと保存データの移動は別 |
| private env | Git外にある環境専用の設定と秘密値 | shellスクリプトではない。sourceやGit登録をしない |
| saved / applied | ファイルへ保存済みと、稼働環境へ反映済み | GUI保存だけではmountやcontainerの環境変数は変わらない |
| proxy / NO_PROXY | 外向きのHTTP経路と、直接接続する除外先 | SQL TCPや全Java/SDKの解釈まで一律にはならない |
| CA / TLS | 証明書の信頼元と暗号化・接続先検証 | 暗号化だけと、CA/host名の検証は別 |
| SSRF | サーバー側HTTP処理が意図しない宛先へ接続する問題への保護 | NO_PROXYは経路で、SSRFの接続許可にはならない |
| readiness / healthy | 起動準備・固定healthの確認 | 業務件数・権限・復元・性能の合格ではない |
| RPO / RTO | 復旧時に許せるデータ損失時間と復旧時間 | 設定値を決めただけで達成したことにはならない |

## GUI・AI・評価の用語

| 用語 | この基盤での意味 | 実際の使い方 |
| --- | --- | --- |
| API / OpenAPI / Swagger UI | 操作の窓口、機械可読仕様、その試行画面 | `/docs`で型を見て、Bearer認証後に既知APIを実行 |
| job ID / run ID | 非同期処理の追跡IDと、生成素材の実行識別子 | job成功、DB commit、Iceberg到着を混同しない |
| Spark driver / executor | 処理を組み立てる側と分散処理を行う側 | Jupyter内driverと別Workerのexecutorを区別 |
| MCP | AIクライアントへ固定tool/resourceを渡す接続方式 | LLM本体や任意shellを自動提供するものではない |
| context / Document | 根拠付きJSONとLangChainの受渡しデータ | データ準備であり、モデル推論は別 |
| embedding / vector / RAG | 索引化の表現、近傍検索、検索結果を使う生成の構成 | Qdrant起動や合成3点だけで実RAG品質は評価できない |
| lineage | データ・処理の関係 | Marquez手動seedと、実ジョブからの自動収集を区別 |
| fixture / evidence | 固定入力の期待値と、観測条件・結果の根拠 | 合成期待値を会社の実測値として転記しない |

分からない用語が操作の判断に関わる場合は、関連文書の成功条件と副作用を先に確認します。例えば「verify」は通常業務表を空にする操作ではなく、隔離namespaceで固定合成データを確かめる機能です。
