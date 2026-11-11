# 図キャプションと根拠の台帳

<!-- doc-purpose:start -->
> **役割:** 各図の説明文、表す範囲、引用する実装と根拠を図番号から引く。
>
> **対象読者:** 評価・報告担当
>
> **読むタイミング:** 図を報告書へ貼り、意味と限界が伝わる説明を添えるとき。
>
> **前提となる資料:** [PoC報告書・発表に使う図版と雛形](<README.md>)
>
> **次に読む資料:** [報告する主張と根拠の対応表](<EVIDENCE-MATRIX.md>) / [報告書用の実装構成一覧](<IMPLEMENTATION-INVENTORY.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

[図版集の使い方](README.md) / [HTML一覧](gallery.html)

照合日: 2026-11-11。生成元はfigures.jsonです。図の作成は、同日付で全機能を再試験したことを意味しません。

## 初めて図を使うとき

1. 説明したい問いを決め、下の用途から図を選びます。
2. SVG/PNGを貼り、同じ図のキャプションを添えます。
3. 合成条件・未評価・表単位commit等の注記も残します。
4. 会社での結果は別の証跡IDで示し、図の照合日と分けます。

captionは図の説明で、会社での実施結果の代筆ではありません。全体を示す図01は拡大可能な付録にし、説明の中心には用途別図を選ぶと読みやすくなります。

## 根拠を追って変更するとき

専門のレビューでは、各図の根拠からCompose・SQL・API・検証記録へ遡り、対象commitの差分を確認します。sourcesは公開ファイルの参照で、今回の会社受入の証跡を自動収集する機能ではありません。

内容を直す場合はfigures.jsonのcaption・notes・sources・Mermaidを揃え、SVG/HTML/台帳を再生成します。[編集ガイド](EDITING-GUIDE.md)と[根拠対応表](EVIDENCE-MATRIX.md)を使います。

## 図01 全41サービスの配置と起動条件

**用途:** 基盤設計・導入レビュー

**キャプション:** core6サービスに13種類のprofileを重ねる41定義の単一ホストPoC構成。監視拡張はsource-only検査であり、host権限と実機受入は別途必要。

[SVG](figures/01-platform-detail.svg) / [Mermaid](mermaid/01-platform-detail.mmd)

- Dokploy・Samba・daemon proxyはホスト側の別設定。SQL Serverは外部DB。
- 初期化4定義と通常0の明示検証2定義を区別する。新規監視・比較Labの実機受入は別途必要。

**根拠:** [compose.yaml](../../infra/lakehouse/compose.yaml)、[compose.yaml](../../infra/compute/compose.yaml)、[compose.yaml](../../infra/orchestration/compose.yaml)、[compose.yaml](../../infra/demo/compose.yaml)、[compose.yaml](../../infra/cdc/compose.yaml)、[compose.yaml](../../infra/poc/compose.yaml)、[compose.yaml](../../infra/mcp/compose.yaml)、[compose.yaml](../../infra/openobserve/compose.yaml)、[compose.yaml](../../infra/sail/compose.yaml)

## 図02 ファイル・SQLのバッチ取り込み経路

**用途:** データ連携・業務担当

**キャプション:** SQLのread-only抽出とSamba/GUI素材は同じlanding契約へ合流する。workerが原本とBronzeを保存し、明示したmedallion操作でSilver・隔離・Goldへ進む。

[SVG](figures/02-batch-flow.svg) / [Mermaid](mermaid/02-batch-flow.mmd)

- ファイルを消すだけでは取り込み済みBronzeを削除しない。会社データは合成試験と別環境で受入。
- 1原本16MiB以下、契約CSV/JSONは100000行以下。SQL抽出の6表は同一時点transactionではない。

**根拠:** [extract-sqlserver.py](../../scripts/extract-sqlserver.py)、[generate-demo.py](../../scripts/generate-demo.py)、[admin.py](../../scripts/admin.py)、[sources.json](../../medallion/sources.json)

## 図03 メダリオンの採用・隔離・集計ルール

**用途:** データ品質・業務集計レビュー

**キャプション:** Bronzeの版からupdated_at等の固定順序で最新候補を選び、型・数量・親参照を検査する。Silverと隔離を分け、親子明細を事前集約してGoldの多重加算を防ぐ。

[SVG](figures/03-medallion.svg) / [Mermaid](mermaid/03-medallion.mmd)

- seed42 / orders20 / days7 / edge専用fixture。GUIの複数runの累積値と比較しない。
- 置換は1表単位。複数表の読取りでは世代が混在し得る。更新時刻が不正なら版順を保証できない。

**根拠:** [01_silver.sql](../../medallion/01_silver.sql)、[02_quarantine.sql](../../medallion/02_quarantine.sql)、[03_gold.sql](../../medallion/03_gold.sql)、[expected-demo.json](../../medallion/expected-demo.json)

## 図04 CDCの確定順序と再開位置

**用途:** DBA・ストリーミング設計

**キャプション:** SQL Server CDCをDebeziumで読み、Kafkaを経由してrawイベントとSQLite現在値を保存する。consumerはSilver/Goldまで確認してcheckpointを保存し、その後Kafka offsetをcommitする。

[SVG](figures/04-cdc-sequence.svg) / [Mermaid](mermaid/04-cdc-sequence.mmd)

- 過去の合成試験：初期144＋INSERT6＋UPDATE6＋DELETE6＋tombstone6＝raw168。restart後distinct168。
- GUIの固定I/U/Dは専用DBの所有権tokenを検証する素材操作。会社業務DBのDMLは実行しない。

**根拠:** [consumer.py](../../infra/cdc/consumer.py)、[connector.py](../../infra/cdc/connector.py)、[CDC.md](../../docs/CDC.md)、[verification.json](../../docs/verification.json)

## 図05 GUI・API・workerの操作責任

**用途:** 利用者導線・API連携

**キャプション:** Streamlit・Swagger・MCP連携は固定FastAPIを使い、source jobとpipeline jobを分ける。同梱n8n/AirflowはWORKER_TOKENでworkerへ直接依頼する。設定保存からDockerやsudoを呼ばない。

[SVG](figures/05-control-api.svg) / [Mermaid](mermaid/05-control-api.mmd)

- APIは1worker前提。直近20件のjob履歴はメモリ内で、API再起動後は失われる。
- タイムアウトは処理停止を意味しない。workerのbusyと実体を見て、二重実行を避ける。

**根拠:** [api.py](../../infra/demo/api.py)、[job_manager.py](../../infra/demo/job_manager.py)、[control_settings.py](../../infra/demo/control_settings.py)、[admin.py](../../scripts/admin.py)

## 図06 常駐SparkとNotebookの実行経路

**用途:** 分析基盤・分散計算レビュー

**キャプション:** compute有効時、Jupyterのdriverがstandalone Masterへ接続し別Workerでexecutorを動かす。event logを共有し、Historyで終了後も参照する。computeなしのNotebookは明示local[2]である。

[SVG](figures/06-spark-compute.svg) / [Mermaid](mermaid/06-spark-compute.mmd)

- 7077/7078・Notebook callbackは内部用。Sparkの3UIはhost loopbackに限定。
- spark-checkは別のlocal[2]互換smoke。常駐クラスタへのremote実行成功とは区別する。

**根拠:** [compose.yaml](../../infra/compute/compose.yaml)、[lakehouse_session.py](../../infra/poc/lakehouse_session.py)、[Dockerfile.notebooks](../../infra/poc/Dockerfile.notebooks)、[COMPATIBILITY.md](../../docs/COMPATIBILITY.md)

## 図07 AIへ渡す事実と根拠の境界

**用途:** AI・業務利用の説明

**キャプション:** Trinoが各表のmain snapshotを固定し、上限付きcontext JSONへ表名とsnapshot IDを付ける。LangChain Document化までを同梱し、モデル推論やRAGの完成とは区別する。

[SVG](figures/07-ai-context.svg) / [Mermaid](mermaid/07-ai-context.mmd)

- export時SQLのhashは、過去snapshotを生成したSQL版の証明ではない。実行監査台帳は追加課題。
- Qdrantの合成vector3点とMarquezの手動系統seedは疎通試験。意味検索精度・実系統自動収集を保証しない。

**根拠:** [ai_context.py](../../scripts/ai_context.py)、[context_chain.py](../../examples/ai/context_chain.py)、[server.py](../../infra/mcp/server.py)、[verification.json](../../docs/verification.json)

## 図08 企業プロキシとCAを設定する場所

**用途:** ネットワーク・導入担当

**キャプション:** daemon、BuildKit、稼働中client、Java/SDKの設定を分ける。private canonicalへ必須内部名と会社追加先をまとめ、企業CAをホスト・image・JVMへ必要な範囲で適用する。

[SVG](figures/08-proxy-ca.svg) / [Mermaid](mermaid/08-proxy-ca.mmd)

- NO_PROXYは経路選択。n8nのSSRF許可は別の保護。内部bypassがあってもSSRF許可にはならない。
- GUI診断は固定HTTPS4先だけ。成功してもdaemon・Java・実会社proxy全経路の受入完了ではない。

**根拠:** [configure-proxy.sh](../../scripts/configure-proxy.sh)、[settings.py](../../scripts/settings.py)、[proxy](../../infra/proxy)、[COMPANY-PROXY.md](../../docs/COMPANY-PROXY.md)

## 図09 認証・権限・接続先の境界

**用途:** セキュリティ・公開レビュー

**キャプション:** 利用者は限定入口から操作し、API/MCPは専用tokenを使う。管理者だけがDokploy/Docker/ホスト設定を変更する。内部Trino/カタログは閉域前提で、利用者別認可の完成は別課題である。

[SVG](figures/09-security-boundaries.svg) / [Mermaid](mermaid/09-security-boundaries.mmd)

- SQLのDB資格情報、worker token、S3鍵をLLMへ渡さない。Notebook/Superset等nativeツールの権限は別管理。
- 公開素材は実IP・proxy・ユーザー名・実データ・証明書を含めず、既知情報と全履歴を照合する。

**根拠:** [api.py](../../infra/demo/api.py)、[server.py](../../infra/mcp/server.py)、[compose.yaml](../../infra/lakehouse/compose.yaml)、[SSRF.md](../../docs/SSRF.md)

## 図10 監視・状態確認・業務品質を分ける

**用途:** 運用・品質レビュー

**キャプション:** Prometheusはworker/self/node/cadvisor/Loki/Vectorの6job定義。Vectorのcurated有限capture・VRL秘匿化からLokiへ配送するsource実装。host権限・公式validator・実配送・GUI・容量は別受入。

[SVG](figures/10-observability.svg) / [Mermaid](mermaid/10-observability.mmd)

- host/cgroupはread承認後に受入。DB/SQL/Kafka固有指標は追加評価。任意OpenObserveはlog-only、metricsは後続。
- Grafana JSON exportはdashboard単体。利用者・権限・全設定の復元はDBと設定のバックアップで確認する。

**根拠:** [prometheus.yml](../../infra/monitoring/prometheus.yml)、[admin.py](../../scripts/admin.py)、[grafana_setup.py](../../scripts/grafana_setup.py)、[poc_quality.py](../../infra/demo/poc_quality.py)、[compose.yaml](../../infra/openobserve/compose.yaml)、[OPENOBSERVE.md](../../docs/OPENOBSERVE.md)

## 図11 公開Gitから配置・反映する手順

**用途:** 導入・変更管理

**キャプション:** ホスト準備とprivate設定を行い、生成後appNameへCompose名を合わせる。Composeサービス自身のEnvironmentを保存し、Git Providerと9ファイルCommandからDeployする。GUI保存だけでは反映しない。

[SVG](figures/11-deployment-lifecycle.svg) / [Mermaid](mermaid/11-deployment-lifecycle.mmd)

- 9Composeの標準Commandは初回から使用可能。起動対象はCOMPOSE_PROFILESで段階追加する。
- CLIかDokployの管理入口を一つに決める。private envとDokployサービスenvは管理者が同期して再配置する。

**根拠:** [install.sh](../../infra/dokploy/install.sh)、[DOKPLOY.md](../../docs/DOKPLOY.md)、[START-HERE.md](../../docs/START-HERE.md)、[COMPANY-UPDATE.md](../../docs/COMPANY-UPDATE.md)

## 図12 永続データと復元を一組で管理する

**用途:** バックアップ・引継ぎ・復旧

**キャプション:** 受領原本とIceberg metadata/data、Lakekeeper DBと暗号鍵を整合した停止点で保管する。各GUIのDB/暗号鍵、SparkとCDCの再開状態、外部landing/workspaceも復元範囲へ含める。

[SVG](figures/12-persistence-recovery.svg) / [Mermaid](mermaid/12-persistence-recovery.mmd)

- 20は全profileのvolume定義数。core用Composeは9定義。任意Labの2種を含む。
- appName・DATA_VOLUME_PREFIX・秘密値を単独変更しない。DB passwordのenv変更だけで既存DBは更新されない。

**根拠:** [OPERATIONS.md](../../docs/OPERATIONS.md)、[compose.yaml](../../infra/lakehouse/compose.yaml)、[compose.yaml](../../infra/compute/compose.yaml)、[compose.yaml](../../infra/cdc/compose.yaml)、[compose.yaml](../../infra/poc/compose.yaml)、[compose.yaml](../../infra/openobserve/compose.yaml)、[compose.yaml](../../infra/sail/compose.yaml)

## 図13 合成PoCで確認したこと・未評価のこと

**用途:** PoC結果の報告・意思決定

**キャプション:** 小規模合成データでファイルからIceberg、更新・削除、再実行、各GUI/APIの経路を確認した記録がある。一方、実会社proxy/DB・モデル推論・復元・容量/性能の受入は別に必要である。

[SVG](figures/13-evidence-summary.svg) / [Mermaid](mermaid/13-evidence-summary.mmd)

- 数値はdocs/verification.jsonと独立fixtureの範囲。全試験が単一commitで再実行されたとは主張しない。
- 図の作成日は再試験日ではない。報告先では環境・期間・母数・受入判定・未実施を一緒に記載する。

**根拠:** [verification.json](../../docs/verification.json)、[expected-demo.json](../../medallion/expected-demo.json)、[VALIDATION.md](../../docs/VALIDATION.md)

## 図14 PoCから会社導入へ進む評価の順番

**用途:** PoC計画・次フェーズの合意

**キャプション:** 機能往復の確認を起点に、会社ネットワーク/DB受入、業務整合、負荷と復元、AI評価へ段階を分ける。未評価を完了扱いせず、次へ進む条件と証跡を合意する。

[SVG](figures/14-poc-roadmap.svg) / [Mermaid](mermaid/14-poc-roadmap.mmd)

- Cloudflare・ClickHouse・LLM基盤・全ログ監視等は採用判断が必要な候補。自動導入済みとはしない。
- 具体的な評価項目・結果記入欄はEVALUATION-PLANとPOC-REPORT-TEMPLATEを使用する。

**根拠:** [START-HERE.md](../../docs/START-HERE.md)、[COMPANY-ROLLOUT.md](../../docs/COMPANY-ROLLOUT.md)、[OPERATIONS.md](../../docs/OPERATIONS.md)、[AI-READINESS.md](../../docs/AI-READINESS.md)
