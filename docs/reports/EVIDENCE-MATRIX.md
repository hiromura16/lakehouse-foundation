# 報告する主張と根拠の対応表

<!-- doc-purpose:start -->
> **役割:** 報告する主張を既存検証・合成fixture・導入先受入・未評価に分けて根拠へ結ぶ。
>
> **対象読者:** 評価・報告担当 / 開発・設計担当
>
> **読むタイミング:** 数値や成功を報告へ引用するときと、導入先の証跡台帳を作るとき。
>
> **前提となる資料:** [検証記録](<../VALIDATION.md>)
>
> **次に読む資料:** [データレイクハウスPoC 報告書テンプレート](<POC-REPORT-TEMPLATE.md>) / [PoC報告書に載せる画面の撮り方](<SCREENSHOT-GUIDE.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

基準日: 2026-11-10（JST）。**下表は既存の公開検証記録の読み方で、今回の会社評価の結果欄ではありません。** 同梱する図は構成や処理の説明資料です。図の存在だけで動作・性能・導入済みを証明しません。[素材一覧](README.md)・[評価計画](EVALUATION-PLAN.md)

## 結果を4種類に分ける

| 表示 | 意味 | 報告での使い方 |
| --- | --- | --- |
| 既存検証記録 | 開発用の実コンテナ、合成データ等で観測された結果 | 検証条件・記録日・限界を添えて参考として引用 |
| 合成fixture | 同梱する固定入力の期待値と独立チェック | 会社データの期待値・品質率と混同しない |
| 導入先の受入 | 会社で実際に実施して得た結果 | 下の空欄へ根拠ID付きで記入。公開版には実値を入れない |
| 未評価・計画 | 実装候補、未測定、未接続、未受入 | 完了と読める表現や達成率へ含めない |

## 検証版と引用の単位

[verification.json](../verification.json) の `checked_on` は `2026-11-10`、timezoneは `Asia/Tokyo` です。`results.public_git_deployment.validated_application_commit` は `b5acdb6b642a1bb31ae95cfd2a114912526c3d15` です。このcommitは公開配置欄の記録で、すべての後続項目に共通する実行commitを保証しません。

報告へ引用するときは、読み取ったJSONのファイルhashまたはGit commit、項目path、評価条件、観測値、限界を一緒に残します。報告書の作成日を、未実施の試験の実施日にしません。

## 既存の主張・観測値・限界

| ID | 報告へ使える主張と観測 | 根拠 | 種類・解釈の限界 |
| --- | --- | --- | --- |
| EV-01 | 原本6ソースと画像1枚で、原本bytes/hash・品質・集計・再投入の11検査を照合。初回7取り込み、再投入0取り込み/7skip | [検証記録](../VALIDATION.md)、`results.core` / `results.public_git_clone` | 既存検証記録。小さい合成入力の機能試験。任意入力の完全性・性能試験ではない |
| EV-02 | 固定edge fixtureはBronze155、Silver145、Quarantine8、旧版/重複2。生産の計画2347、良品2154、不良62、生産2216、停止185分 | [expected-demo.json](../../medallion/expected-demo.json)、[メダリオン](../MEDALLION.md) | 合成fixture。seed42/orders20/days7/edge専用。145+8が155に一致しないことは欠落ではない |
| EV-03 | 固定edge fixtureの受注1435、出荷1363、残72、納期後出荷132/4件を照合 | [expected-demo.json](../../medallion/expected-demo.json) の `sales totals` と独立チェックSQL | 合成fixture。受注単位の集計。会社の販売実績ではない |
| EV-04 | SQL専用試験DBの6表へ新規schema投入し、SELECT抽出の結果をCSV/JSONと照合 | [素材仕様](../DEMO-DATA.md)、[検証記録](../VALIDATION.md)、`results.sql-source` | 既存検証記録。会社本物の認証・権限・schema・TLSでの成功を含めない |
| EV-05 | CDC初回144行、I/U/D各6行、tombstone6。raw168/distinct168でconsumer再起動後も増殖せず、現在値は144行へ戻る | [CDC](../CDC.md)、`results.cdc` | 既存検証記録。専用合成DB。会社での鮮度・長期保持・DDL・冗長性は未評価 |
| EV-06 | Spark4.1.4/Iceberg1.12.0で3行を書きTrinoから同じ値を参照。常駐クラスタでは別worker実行・Arrow6行・保存Historyを確認 | [互換性](../COMPATIBILITY.md)、`results.spark` / `results.persistent_spark` | 既存検証記録。小規模互換・remote実行試験。最大負荷・自動拡張・無停止性ではない |
| EV-07 | GUI/APIで素材batch、有限stream2回、保存/未反映、明示変換を確認。素材生成直後のGoldは未変更 | [API](../API.md)、`results.gui_api` | 既存検証記録。APIの履歴は最大20件のメモリ内記録。監査・job再開の永続保証なし |
| EV-08 | 異常catalogは26件・preset7種。実APIから選択した不良急増を素材として生成し、直後のGold非変更を確認 | [異常一覧](../ANOMALY-SCENARIOS.md)、`results.operator_portal` | catalog/生成試験と一部API確認。全26件の下流受入を会社で完了した意味ではない |
| EV-09 | n8nの手動workflow、Airflowの手動DAGがworkerへの依頼に成功。n8nでは未許可内部先をSSRF拒否し、許可workerは成功 | [GUI手順](../GUI-WALKTHROUGHS.md)、[SSRF](../SSRF.md)、`results.n8n` / `results.airflow` / `results.n8n_ssrf` | 既存検証記録。現行のHTTP Request経路等の対象範囲。独自node/会社proxyまで保証しない |
| EV-10 | worker/Prometheus targetがup。Grafana GUI編集が再起動・初期化後も残る | [Grafana](../GRAFANA.md)、`results.monitoring` / `results.grafana` | 既存検証記録。ホスト全体や会社SQLの監視・通知を完成したものではない |
| EV-11 | Goldのmain snapshotを固定して34行をLangChain Documentへ変換 | [AI連携](../AI-READINESS.md)、`results.ai_ready` | 既存検証記録。モデル推論は未実行。表ごとの上限付き参照で、全件集計ではない |
| EV-12 | MCP SDK2.3.0で認証・9read tools/2resourcesを確認。一時プロセスで3write toolsのopt-inと同意を確認 | [MCP](../MCP.md)、`results.mcp` | 既存検証記録。主MCPはread-only。任意SQL/shellや自律的な業務変更を公開しない |
| EV-13 | SupersetのGold2datasets、Jupyter実kernel、Qdrant合成vector3点、Marquez手動demo lineageを確認 | [PoCツール](../POC-TOOLS.md)、`results.poc_tools` | 既存検証記録。vectorは数値疎通、lineageは手動seed。意味検索精度や実jobの自動採取ではない |
| EV-14 | Superset dashboard、Notebook workspace、Qdrant3点、Marquez jobが対象serviceの再起動後も保持 | [検証記録](../VALIDATION.md)、`results.poc_tools.persistence` | 既存検証記録。再起動保持。backupからの別環境復元試験ではない |
| EV-15 | Basic認証CONNECT/TLS模擬環境で成功/407/CA欠落/内部bypass/redirect拒否を確認 | [会社proxy](../COMPANY-PROXY.md)、`results.corporate_proxy` | 模擬試験。`actual_company_proxy_tested=false`。実会社の認証方式・経路は未評価 |
| EV-16 | 公開CI相当で260テスト、Python/SQL書式と全profileのCompose展開を確認 | [検証記録](../VALIDATION.md)、`results.source_quality` / `results.company_compatibility` | 記録日時点の静的・自動試験。260は過去記録値で、今後の最新件数は当該CI結果を参照。実会社での起動試験ではない |
| EV-17 | 公開配置で34service定義、33container、29稼働と4one-shotを記録。spark-checkは通常起動しない | `results.public_git_deployment`、[配置](../DEPLOYMENT.md) | その時点の全profile配置記録。全会社VMで同数・同資源・全機能が同時稼働する保証ではない |

## 図と根拠の対応

| 図 | 報告で説明すること | 主に参照する根拠 |
| --- | --- | --- |
| [01 詳細構成](figures/01-platform-detail.svg) | コンポーネント、標準/任意、管理とデータ | [構成](../ARCHITECTURE.md)、[Compose](../../infra/lakehouse/compose.yaml)、EV-17 |
| [02 バッチ経路](figures/02-batch-flow.svg) | 完成素材、原本、台帳、明示実行 | EV-01/04、[worker](../../scripts/admin.py) |
| [03 メダリオン](figures/03-medallion.svg) | 最新候補、品質、隔離、集計粒度 | EV-02/03、[業務SQL](../../medallion/) |
| [04 CDC](figures/04-cdc-sequence.svg) | raw/現在値/再開とI/U/D | EV-05、[CDC実装](../../infra/cdc/) |
| [05 GUI/API](figures/05-control-api.svg) | 保存と適用、非同期job、明示処理 | EV-07/08、[API実装](../../infra/demo/api.py) |
| [06 Spark](figures/06-spark-compute.svg) | remote/local、常駐と履歴 | EV-06、[compute](../../infra/compute/) |
| [07 AI context](figures/07-ai-context.svg) | snapshot根拠、上限、read/writeの境界 | EV-11/12/13、[context実装](../../scripts/ai_context.py) |
| [08 proxy/CA](figures/08-proxy-ca.svg) | daemon/build/runtime/Javaと内部bypass | EV-15、[proxy](../PROXY.md) |
| [09 セキュリティ境界](figures/09-security-boundaries.svg) | 共有token、SSRF、ホスト管理とアプリ | EV-09/12、[設定](../CONFIGURATION.md) |
| [10 監視](figures/10-observability.svg) | health、処理、業務値、保持範囲 | EV-10、[監視設定](../../infra/monitoring/) |
| [11 配置](figures/11-deployment-lifecycle.svg) | private設定、Dokploy、検証、再配置 | [初回手順](../START-HERE.md)、[Dokploy](../DOKPLOY.md) |
| [12 保存・復旧](figures/12-persistence-recovery.svg) | backup対象と復元時の照合 | EV-14、[運用](../OPERATIONS.md)。復元成功の証跡ではない |
| [13 検証概要](figures/13-evidence-summary.svg) | 何を確認し何が残るか | EV-01〜17、[機械可読記録](../verification.json) |
| [14 次段階](figures/14-poc-roadmap.svg) | 会社受入・負荷/復旧・AI評価の順序 | [評価計画](EVALUATION-PLAN.md)。日付・予算の確約ではない |

## 導入先の証跡台帳テンプレート

この表を社内の管理先へコピーして記入します。既存公開のEV番号と、導入先で採番する証跡IDを分けます。

| 証跡ID | 評価ID | 実施日時/TZ | 配置commit | 入力範囲/版 | 観測した結果 | 社内原本の保管先 | 判定/限界 | 共有用コピー |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ［記入］ | ［記入］ | ［記入］ | ［記入］ | ［記入］ | ［記入］ | ［記入］ | 未実施 | ［記入］ |

| 素材の種類 | 原本に保持するもの | 共有用に確認するもの |
| --- | --- | --- |
| 取込/変換 | manifest、原本hash、snapshot、独立照合 | 対象範囲と数値を一般化。実ファイル名・実業務値を除去 |
| CDC | 操作順、時刻、LSN/offset、各層の差分 | 実DB/schema/table名、接続先、資格情報を除去 |
| GUI/ログ | 操作前後、保存、再起動、該当job | アカウント、token、URL、Environment、通知先を除去 |
| Notebook/JSON | 実行code、入力版、出力、上限 | 出力cell・metadata・接続URI・実payloadも確認 |
| PDF/画像/SVG | 編集前原本と共有版 | 検索可能text、metadata、リンク、埋込/添付、ぼかし下の原本残存を確認 |

共有版は原本を上書きせず別ファイルへ作り、レビュー後に配布します。公開前検査で検出0であっても、未知の機密が存在しないことを無条件に保証するものではありません。図の一般名と匿名化した結果を優先します。

## 初めて台帳を使う場合

評価計画のIDと証跡IDを一対一に限定する必要はありません。一つの評価へ複数の画面・集計・hashがある場合は関連付け、同じ画面がどの問いを支えるかを明記します。原本は社内保存し、公開用には一般化した説明だけを残します。

証跡を読む順序は、評価条件→実施操作→観測結果→期待との差→判定です。スクリーンショットの見た目だけで成功とせず、対象commit・期間・snapshot・jobの対応を追えることを確認します。

| 証拠の種類 | 直接示すもの | それだけでは示さないもの |
| --- | --- | --- |
| Compose config成功 | 結合した設定が構文上成立 | image取得、health、実データの正しさ |
| job成功 | 指定jobが実装上の完了へ到達 | 独立した業務正解との一致 |
| Goldの集計 | 指定版・範囲の計算結果 | 未抽出データの完全性、全期間の品質 |
| AI context | 根拠snapshot付きの入力を取得 | LLM回答精度、意味検索品質 |
| backup作成 | 保存ファイルを取得 | 別環境で整合して復元可能 |

## 証跡の版と差替え

再実施した証跡は旧原本を消して置き換えず、新しいID/版を付けて関連を記録します。図や本文がどちらを引用するかを更新し、失敗が解消した理由を残します。過去の検証で使った版と現在のmainが異なる場合は、影響する変更と再試験の要否を判断します。

出力物のhashは同じファイルを特定する手段です。hash一致だけで、その内容が業務的に正しい、最新である、公開可能であるとは判断しません。測定や社内原本の公開可否は、別の確認として扱います。
