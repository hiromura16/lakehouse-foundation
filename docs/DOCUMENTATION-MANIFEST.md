# 目的・読者から探す文書マニフェスト

<!-- doc-purpose:start -->
> **役割:** 公開Markdown全件の固有の目的・読者・読む時点を、目的別と読者別に引く。
>
> **対象読者:** 初めて使う人 / 基盤構築担当 / データ利用担当 / 開発・設計担当 / 運用担当 / 評価・報告担当
>
> **読むタイミング:** 必要な文書を探すときや、文書の追加・改名時に登録を更新するとき。
>
> **前提となる資料:** なし。必要な用語は本文で確認します。
>
> **次に読む資料:** [初めての理解から運用・設計へ進む道順](<LEARNING-PATH.md>) / [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

この一覧は公開Markdown全件の道案内です。「前提」は先に確認する文書、「次」は読み終えた後の候補です。
実際の操作はリンク先で成功条件まで確認します。templateは社内へコピーして記入する雛形、referenceは照合資料です。

## 目的から探す

### 01 入口・学習

| 文書 | 何のために読むか | 読むタイミング | 区分 |
| --- | --- | --- | --- |
| [Lakehouse Foundation](<../README.md>) | MES・ERP向けPoC基盤の機能、構築入口、任意機能と報告素材を見渡す。 | clone直後に、何を構築できるかと最初の手順を選ぶとき。 | active |
| [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>) | 公開Markdown全件の固有の目的・読者・読む時点を、目的別と読者別に引く。 | 必要な文書を探すときや、文書の追加・改名時に登録を更新するとき。 | reference |
| [用語を役割と実際の操作へ結び付ける](<GLOSSARY.md>) | 保存・処理・配置・操作の用語を、同梱実装の役割へ結び付ける。 | Iceberg、snapshot、profile、jobなどの違いを確認するとき。 | reference |
| [初めての理解から運用・設計へ進む道順](<LEARNING-PATH.md>) | 理解・操作・運用・設計の段階ごとに、練習と到達条件を選ぶ。 | 読む順番が分からないときや、次に学ぶ操作を決めるとき。 | active |
| [BashとZshの両方で手順を実行する](<SHELLS.md>) | 会社VMのZshとBashでコマンドを使い分け、対話入力・コメント・shebang・引用の違いを確認する。 | Zsh端末で初回構築や運用のコマンドを貼る前、シェル例を追加・変更するとき。 | active |
| [初めて構築する人の一本道ガイド](<START-HERE.md>) | 新規Linux環境の準備からcore配置と合成データ検証までを順に進める。 | 初めてこのリポジトリを配置し、段階ごとの成功条件を確認するとき。 | active |

### 02 構築・通信

| 文書 | 何のために読むか | 読むタイミング | 区分 |
| --- | --- | --- | --- |
| [会社のプロキシ・CAを設定する](<COMPANY-PROXY.md>) | Git・uv・Docker・build・runtimeごとに社内proxyとCAを準備し、通信を診断する。 | 会社ネットワークへ初めて配置する前や、取得経路の失敗を切り分けるとき。 | active |
| [会社への導入・受入ガイド](<COMPANY-ROLLOUT.md>) | 会社側の担当分担、Samba、外部SQL、業務照合と導入受入条件を決める。 | 新規構築の手順に会社固有の準備と業務側の受入を組み合わせるとき。 | active |
| [会社VMの修正を取り込み、Dokployへ再配置する](<COMPANY-UPDATE.md>) | 既存の設定・鍵・volumeを保持し、会社VMの修正を取り込んでDokployへ再配置する。 | 既に動いている環境へ公開Gitの互換性修正を反映するとき。 | active |
| [サイト設定リファレンス](<CONFIGURATION.md>) | 生成器・Compose・CLIが使う設定項目と、保存先・認証・反映先を照合する。 | private設定を作成・変更し、何に使われる値かを調べるとき。 | reference |
| [導入・検証・停止](<DEPLOYMENT.md>) | private設定、host構成、CLIによる起動・検証・停止の操作を確認する。 | 管理方法を決めて配置するときや、CLI管理の構成を起動・停止するとき。 | active |
| [Dokploy 0.31.0でGitから配置する](<DOKPLOY.md>) | Dokploy 0.31.0の画面で、生成appName・個別Environment・7つのComposeを揃えて配置する。 | Dokployへログインできた後、同じComposeアプリを初回配置・再配置するとき。 | active |
| [Linuxサーバーを最初に準備する](<HOST-SETUP.md>) | 新規Ubuntuで操作端末・権限・Docker・Git・uvの前提を準備する。 | アプリ配置前にhostの状態と不足している前提を確認するとき。 | active |
| [プロキシ設定の場所と反映方法](<PROXY.md>) | 通信主体別のproxy設定ファイル、CA受渡しと反映方法を参照する。 | 会社向け手順を読んだ後に、個別の設定場所や再起動対象を調べるとき。 | reference |
| [SSRF対策と内部接続の設定](<SSRF.md>) | n8nの宛先制御、内部worker通信、proxyと固定APIの境界を設定する。 | 内部APIへの接続許可を決めるときや、SSRF制御による拒否を確認するとき。 | active |

### 03 データ・操作

| 文書 | 何のために読むか | 読むタイミング | 区分 |
| --- | --- | --- | --- |
| [異常シナリオを選んで素材を作る](<ANOMALY-SCENARIOS.md>) | 行・業務値とファイル転送の異常を選び、生成診断と下流の期待挙動を比較する。 | 正常素材との差分、隔離、重複、破損・未完成入力を試す計画を立てるとき。 | reference |
| [GUI操作APIとSwagger](<API.md>) | Swaggerの認証と固定APIを使い、設定保存・素材生成・非同期job・参照を行う。 | GUIと同じ操作をAPIクライアントや自動化から呼び出すとき。 | reference |
| [MES・ERPのダミーデータ投入](<DEMO-DATA.md>) | 6種のMES・ERP素材、画像、SQL投入、有限streamの生成仕様とCLIを確認する。 | ダミー生成の入力・出力・書込範囲を決め、素材だけを準備するとき。 | active |
| [GUIからダミーデータを投入する](<DEMO-GUI.md>) | Streamlitで素材生成条件を選び、投入jobと生成結果を確認する。 | 配置済みの合成検証環境へ、GUIからダミー素材を投入するとき。 | active |
| [GrafanaのGUI編集と保存](<GRAFANA.md>) | GrafanaのGUIでdashboardを編集・保存し、収集対象とPromQLの意味を確認する。 | monitoring起動後に初期dashboardを整え、監視値を解釈・引き継ぐとき。 | active |
| [n8n・Airflow・Grafanaを画面から使う](<GUI-WALKTHROUGHS.md>) | n8n・Airflow・Grafanaで接続、認証、手動実行、履歴、保存を順に確かめる。 | 対応profileを配置した後、同梱ジョブを画面から1回実行して確認するとき。 | active |
| [PoC操作ポータル](<POC-PORTAL.md>) | 操作ポータルの各ページから、サービス入口・データ・診断・履歴へ移動する。 | GUIを開いた後、目的の操作と結果確認の画面を探すとき。 | active |
| [BI・Notebook・ベクトル検索・データ系統を追加する](<POC-TOOLS.md>) | Superset・Jupyter・Qdrant・MarquezのGUIと合成デモを追加して用途別に検証する。 | coreの検証後にBI、Notebook、数値vector、手動lineageを試すとき。 | active |
| [固定SQL sourceで疑似ERP・MESを検証する](<SQLSERVER-FIXED-DEMO.md>) | 所有権付き固定SQL表への合成データ投入と、読取専用バッチ・CDCの段階的な受入を行う。 | 同じ開発DBの固定6表で投入・snapshot・増分・CDCを比較するとき。 | active |
| [合成入力を最初に作る](<../samples/README.md>) | 実データを使わずに最初の6契約素材を生成し、出力と後続処理の境界を確認する。 | 空の検証用landingに小さい固定条件の合成入力を初めて用意するとき。 | active |

### 04 設計・拡張

| 文書 | 何のために読むか | 読むタイミング | 区分 |
| --- | --- | --- | --- |
| [AIへ渡せるデータとLangChain連携](<AI-READINESS.md>) | Goldとsnapshotの根拠をJSON・LangChain Documentへ渡し、推論やRAGとの境界を確認する。 | AIへ渡す業務contextを取得するときや、モデル連携の追加要件を決めるとき。 | active |
| [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>) | 保存・変換・操作の構成とprofileの責務を、データと制御の経路から把握する。 | 構築前の全体理解や、構成変更時の影響範囲を調べるとき。 | reference |
| [SQL Server の変更を Iceberg へ反映する](<CDC.md>) | SQL ServerのsnapshotとI/U/DをDebezium・Kafka・consumer経由でIcebergへ反映する。 | DBAの準備後にcdc profileを配置し、現在値・削除・再開位置を照合するとき。 | active |
| [Cloudflareを使う場所と社内に残す場所](<CLOUDFLARE.md>) | Cloudflareの入口保護・外部保存・AI候補を、社内に残す処理と分けて評価する。 | 未接続の外部サービスを追加する前に、通信・認証・費用・データ範囲を検討するとき。 | reference |
| [バージョンと連携互換性](<COMPATIBILITY.md>) | 固定image・Python環境・Spark/Iceberg等の組合せと互換性の確認方法を調べる。 | 依存更新を計画するときや、build・起動・連携のどこで失敗したかを分けるとき。 | reference |
| [ソース・指標・API・計算基盤を拡張する](<CUSTOMIZATION.md>) | Gold指標・入力契約・API・計算環境の変更を、実装箇所と回帰条件へ落とし込む。 | 導入済みPoCを専用検証環境で拡張し、移行・再実行・受入を計画するとき。 | active |
| [GUIで設定・操作する選択肢](<GUI-OPTIONS.md>) | 操作・オーケストレーション・列変換のGUIを分け、同梱機能と追加候補を比較する。 | 画面で何を設定したいか整理し、追加OSSや実装の採否を決めるとき。 | reference |
| [AIクライアントからMCPで操作する](<MCP.md>) | 認証付きMCPから固定の業務参照・運用APIを呼び、AIクライアントへ接続する。 | APIの範囲を理解した後に、Streamable HTTPのMCP連携を用意するとき。 | active |
| [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>) | 6契約の取り込み、最新版採用、不正行隔離、Gold集計と再投入の規則を追う。 | 素材から業務値へ変換する意味を理解し、品質や更新結果を照合するとき。 | active |
| [既存SQL Serverからランディングへ抽出する](<SOURCE-SQLSERVER.md>) | 既存SQL Serverを読取専用SELECTで抽出し、6契約のlanding素材を作成する。 | 実ソースDBの列対応・権限・上限を決め、全件snapshotのbridgeを試すとき。 | active |

### 05 運用・根拠

| 文書 | 何のために読むか | 読むタイミング | 区分 |
| --- | --- | --- | --- |
| [BuildKit auth session timeout: 公開前の診断draft](<BUILD-KIT-AUTH-DIAGNOSTIC.md>) | PR11後も報告されたBuildKit timeoutの事実・仮説・次の診断を分離し、未解決として引き継ぐ。 | computeの配置前、明示検証、proxy build障害の切り分け時。 | active |
| [Notebook workspace準備と明示Spark検証](<COMPUTE-VALIDATION.md>) | 安全なworkspace plan/apply/probe、既存projectのSpark smoke、結果とpull policyを確認する。 | computeの配置前、明示検証、proxy build障害の切り分け時。 | active |
| [実装・配置・検証の追跡表](<IMPLEMENTATION-STATUS.md>) | 依頼された機能のソース実装、配置、既存検証、残る受入を別々に追跡する。 | できることと未実施の評価を確認し、PoCの範囲や進捗を説明するとき。 | reference |
| [平時・更新・復旧の運用手順](<OPERATIONS.md>) | 平時点検・更新・backup・復旧を、鍵・DB・object・volumeの状態を揃えて行う。 | 運用を引き継ぐとき、更新前、障害復旧前に実施順序を確認するとき。 | active |
| [採用版の公式リファレンスと文書監査](<REFERENCES.md>) | 採用版の公式仕様・tag・公開実装・検証記録の根拠を照合する。 | 画面名や設定の仕様を確認するときや、依存更新の調査を始めるとき。 | reference |
| [症状から確認する](<TROUBLESHOOTING.md>) | 観測した症状から設定・起動・通信・job・データの失敗箇所を絞り込む。 | 成功条件を満たさないときや、削除や再生成を行う前に原因を調べるとき。 | reference |
| [検証記録](<VALIDATION.md>) | 既存の合成環境・公開clone・CI・互換性修正で確認した条件と限界を読む。 | 報告に結果を引用するときや、変更後の回帰と会社受入を分けるとき。 | reference |

### 06 開発・レビュー

| 文書 | 何のために読むか | 読むタイミング | 区分 |
| --- | --- | --- | --- |
| [変更する人のための読みやすさと検証規約](<../CONTRIBUTING.md>) | 読みやすいコード・コメント・CLI説明と、専用uv環境での品質検査規約を揃える。 | コードや文書を変更する前と、差分をレビューに出す前。 | active |
| [ファイルの場所と変更のしかた](<DIRECTORY-GUIDE.md>) | 目的に対応するファイル、実行経路、公開コードとprivate状態の置き場を探す。 | 実装を読み始めるときや、変更対象と関連する確認箇所を絞るとき。 | reference |
| [外部レビュー指摘の対応と確認](<REVIEW-FOLLOWUP.md>) | PR #3の外部レビュー指摘と、再現・修正・回帰確認の対応を追跡する。 | 既存の指摘がどう解決されたか確認し、同種の回帰を避けるとき。 | reference |
| [レビュー指摘を改善へ取り込む](<REVIEW-WORKFLOW.md>) | issue・PR・依頼文書の指摘を対象commitで確認し、安全な回帰と差分へつなげる。 | レビュー指摘を再現して修正し、検証根拠を揃えて返すとき。 | active |

### 07 報告・図版

| 文書 | 何のために読むか | 読むタイミング | 区分 |
| --- | --- | --- | --- |
| [構成図とロゴの出典](<assets/README.md>) | 全体図・AI候補図・公式ロゴの出典と、詳細図素材の配置を確認する。 | 図やロゴを引用するとき、素材の出典と編集先を調べるとき。 | reference |
| [図キャプションと根拠の台帳](<reports/CAPTIONS.md>) | 各図の説明文、表す範囲、引用する実装と根拠を図番号から引く。 | 図を報告書へ貼り、意味と限界が伝わる説明を添えるとき。 | reference |
| [PoCの意思決定記録テンプレート](<reports/DECISION-RECORD-TEMPLATE.md>) | 選択肢・根拠・制約・承認・再評価条件を、後から追える意思決定記録に残す。 | PoCの採否や構成選択を決め、条件と責任を社内へ引き継ぐとき。 | template |
| [図と出力スクリプトを変更する人へ](<reports/EDITING-GUIDE.md>) | figures.json、描画・exportコード、生成物の責務と変更後の検証を理解する。 | 図の意味・レイアウト・出力形式を変更し、生成差分を確認するとき。 | active |
| [PoCの評価計画と受入条件](<reports/EVALUATION-PLAN.md>) | 配置・品質・CDC・AI・復旧の評価範囲と受入条件を、実施前に記入する。 | 会社PoCを始める前に、担当・条件・成功基準・残す証跡を決めるとき。 | template |
| [報告する主張と根拠の対応表](<reports/EVIDENCE-MATRIX.md>) | 報告する主張を既存検証・合成fixture・導入先受入・未評価に分けて根拠へ結ぶ。 | 数値や成功を報告へ引用するときと、導入先の証跡台帳を作るとき。 | reference |
| [報告書用の実装構成一覧](<reports/IMPLEMENTATION-INVENTORY.md>) | 34サービス・11profile・port・volume・接続主体を公開定義から技術付録として照合する。 | 詳細図の箱と線を確認するときや、構成定義の数量と制約を引用するとき。 | reference |
| [データレイクハウスPoC 報告書テンプレート](<reports/POC-REPORT-TEMPLATE.md>) | 業務目的、評価条件、観測結果、残る条件、次段階の判断を報告書へ整理する。 | 評価計画と証跡が揃い、社内保存用の報告書原本を作成するとき。 | template |
| [PoC説明用のスライド構成](<reports/PRESENTATION-OUTLINE.md>) | 読み手と持ち時間に合わせ、14図の選択・説明順・話す論点を組み立てる。 | 報告書の判断と根拠を、発表や引継ぎのスライドへ移すとき。 | template |
| [PoC報告書・発表に使う図版と雛形](<reports/README.md>) | 用途別14図・キャプション・雛形を選び、PNG・PDF・PPTXへ書き出す。 | PoC報告書や説明スライドを準備し、必要な図と根拠を選ぶとき。 | active |
| [PoC報告書に載せる画面の撮り方](<reports/SCREENSHOT-GUIDE.md>) | 各GUIで残す画面、先に確認する条件、写さない秘密や実データを決める。 | 実施結果を社内報告へ載せるため、撮影前後の確認をするとき。 | active |

## 読者から探す

### データ利用担当

| 文書 | 目的 |
| --- | --- |
| [AIへ渡せるデータとLangChain連携](<AI-READINESS.md>) | Goldとsnapshotの根拠をJSON・LangChain Documentへ渡し、推論やRAGとの境界を確認する。 |
| [異常シナリオを選んで素材を作る](<ANOMALY-SCENARIOS.md>) | 行・業務値とファイル転送の異常を選び、生成診断と下流の期待挙動を比較する。 |
| [GUI操作APIとSwagger](<API.md>) | Swaggerの認証と固定APIを使い、設定保存・素材生成・非同期job・参照を行う。 |
| [会社への導入・受入ガイド](<COMPANY-ROLLOUT.md>) | 会社側の担当分担、Samba、外部SQL、業務照合と導入受入条件を決める。 |
| [MES・ERPのダミーデータ投入](<DEMO-DATA.md>) | 6種のMES・ERP素材、画像、SQL投入、有限streamの生成仕様とCLIを確認する。 |
| [GUIからダミーデータを投入する](<DEMO-GUI.md>) | Streamlitで素材生成条件を選び、投入jobと生成結果を確認する。 |
| [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>) | 公開Markdown全件の固有の目的・読者・読む時点を、目的別と読者別に引く。 |
| [用語を役割と実際の操作へ結び付ける](<GLOSSARY.md>) | 保存・処理・配置・操作の用語を、同梱実装の役割へ結び付ける。 |
| [GrafanaのGUI編集と保存](<GRAFANA.md>) | GrafanaのGUIでdashboardを編集・保存し、収集対象とPromQLの意味を確認する。 |
| [GUIで設定・操作する選択肢](<GUI-OPTIONS.md>) | 操作・オーケストレーション・列変換のGUIを分け、同梱機能と追加候補を比較する。 |
| [n8n・Airflow・Grafanaを画面から使う](<GUI-WALKTHROUGHS.md>) | n8n・Airflow・Grafanaで接続、認証、手動実行、履歴、保存を順に確かめる。 |
| [初めての理解から運用・設計へ進む道順](<LEARNING-PATH.md>) | 理解・操作・運用・設計の段階ごとに、練習と到達条件を選ぶ。 |
| [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>) | 6契約の取り込み、最新版採用、不正行隔離、Gold集計と再投入の規則を追う。 |
| [PoC操作ポータル](<POC-PORTAL.md>) | 操作ポータルの各ページから、サービス入口・データ・診断・履歴へ移動する。 |
| [BI・Notebook・ベクトル検索・データ系統を追加する](<POC-TOOLS.md>) | Superset・Jupyter・Qdrant・MarquezのGUIと合成デモを追加して用途別に検証する。 |
| [既存SQL Serverからランディングへ抽出する](<SOURCE-SQLSERVER.md>) | 既存SQL Serverを読取専用SELECTで抽出し、6契約のlanding素材を作成する。 |
| [固定SQL sourceで疑似ERP・MESを検証する](<SQLSERVER-FIXED-DEMO.md>) | 所有権付き固定SQL表への合成データ投入と、読取専用バッチ・CDCの段階的な受入を行う。 |
| [症状から確認する](<TROUBLESHOOTING.md>) | 観測した症状から設定・起動・通信・job・データの失敗箇所を絞り込む。 |
| [PoCの評価計画と受入条件](<reports/EVALUATION-PLAN.md>) | 配置・品質・CDC・AI・復旧の評価範囲と受入条件を、実施前に記入する。 |
| [合成入力を最初に作る](<../samples/README.md>) | 実データを使わずに最初の6契約素材を生成し、出力と後続処理の境界を確認する。 |

### 初めて使う人

| 文書 | 目的 |
| --- | --- |
| [Lakehouse Foundation](<../README.md>) | MES・ERP向けPoC基盤の機能、構築入口、任意機能と報告素材を見渡す。 |
| [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>) | 保存・変換・操作の構成とprofileの責務を、データと制御の経路から把握する。 |
| [GUIからダミーデータを投入する](<DEMO-GUI.md>) | Streamlitで素材生成条件を選び、投入jobと生成結果を確認する。 |
| [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>) | 公開Markdown全件の固有の目的・読者・読む時点を、目的別と読者別に引く。 |
| [用語を役割と実際の操作へ結び付ける](<GLOSSARY.md>) | 保存・処理・配置・操作の用語を、同梱実装の役割へ結び付ける。 |
| [Linuxサーバーを最初に準備する](<HOST-SETUP.md>) | 新規Ubuntuで操作端末・権限・Docker・Git・uvの前提を準備する。 |
| [初めての理解から運用・設計へ進む道順](<LEARNING-PATH.md>) | 理解・操作・運用・設計の段階ごとに、練習と到達条件を選ぶ。 |
| [PoC操作ポータル](<POC-PORTAL.md>) | 操作ポータルの各ページから、サービス入口・データ・診断・履歴へ移動する。 |
| [BashとZshの両方で手順を実行する](<SHELLS.md>) | 会社VMのZshとBashでコマンドを使い分け、対話入力・コメント・shebang・引用の違いを確認する。 |
| [初めて構築する人の一本道ガイド](<START-HERE.md>) | 新規Linux環境の準備からcore配置と合成データ検証までを順に進める。 |
| [合成入力を最初に作る](<../samples/README.md>) | 実データを使わずに最初の6契約素材を生成し、出力と後続処理の境界を確認する。 |

### 基盤構築担当

| 文書 | 目的 |
| --- | --- |
| [Lakehouse Foundation](<../README.md>) | MES・ERP向けPoC基盤の機能、構築入口、任意機能と報告素材を見渡す。 |
| [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>) | 保存・変換・操作の構成とprofileの責務を、データと制御の経路から把握する。 |
| [BuildKit auth session timeout: 公開前の診断draft](<BUILD-KIT-AUTH-DIAGNOSTIC.md>) | PR11後も報告されたBuildKit timeoutの事実・仮説・次の診断を分離し、未解決として引き継ぐ。 |
| [SQL Server の変更を Iceberg へ反映する](<CDC.md>) | SQL ServerのsnapshotとI/U/DをDebezium・Kafka・consumer経由でIcebergへ反映する。 |
| [Cloudflareを使う場所と社内に残す場所](<CLOUDFLARE.md>) | Cloudflareの入口保護・外部保存・AI候補を、社内に残す処理と分けて評価する。 |
| [会社のプロキシ・CAを設定する](<COMPANY-PROXY.md>) | Git・uv・Docker・build・runtimeごとに社内proxyとCAを準備し、通信を診断する。 |
| [会社への導入・受入ガイド](<COMPANY-ROLLOUT.md>) | 会社側の担当分担、Samba、外部SQL、業務照合と導入受入条件を決める。 |
| [会社VMの修正を取り込み、Dokployへ再配置する](<COMPANY-UPDATE.md>) | 既存の設定・鍵・volumeを保持し、会社VMの修正を取り込んでDokployへ再配置する。 |
| [バージョンと連携互換性](<COMPATIBILITY.md>) | 固定image・Python環境・Spark/Iceberg等の組合せと互換性の確認方法を調べる。 |
| [Notebook workspace準備と明示Spark検証](<COMPUTE-VALIDATION.md>) | 安全なworkspace plan/apply/probe、既存projectのSpark smoke、結果とpull policyを確認する。 |
| [サイト設定リファレンス](<CONFIGURATION.md>) | 生成器・Compose・CLIが使う設定項目と、保存先・認証・反映先を照合する。 |
| [導入・検証・停止](<DEPLOYMENT.md>) | private設定、host構成、CLIによる起動・検証・停止の操作を確認する。 |
| [ファイルの場所と変更のしかた](<DIRECTORY-GUIDE.md>) | 目的に対応するファイル、実行経路、公開コードとprivate状態の置き場を探す。 |
| [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>) | 公開Markdown全件の固有の目的・読者・読む時点を、目的別と読者別に引く。 |
| [Dokploy 0.31.0でGitから配置する](<DOKPLOY.md>) | Dokploy 0.31.0の画面で、生成appName・個別Environment・7つのComposeを揃えて配置する。 |
| [Linuxサーバーを最初に準備する](<HOST-SETUP.md>) | 新規Ubuntuで操作端末・権限・Docker・Git・uvの前提を準備する。 |
| [実装・配置・検証の追跡表](<IMPLEMENTATION-STATUS.md>) | 依頼された機能のソース実装、配置、既存検証、残る受入を別々に追跡する。 |
| [平時・更新・復旧の運用手順](<OPERATIONS.md>) | 平時点検・更新・backup・復旧を、鍵・DB・object・volumeの状態を揃えて行う。 |
| [プロキシ設定の場所と反映方法](<PROXY.md>) | 通信主体別のproxy設定ファイル、CA受渡しと反映方法を参照する。 |
| [採用版の公式リファレンスと文書監査](<REFERENCES.md>) | 採用版の公式仕様・tag・公開実装・検証記録の根拠を照合する。 |
| [BashとZshの両方で手順を実行する](<SHELLS.md>) | 会社VMのZshとBashでコマンドを使い分け、対話入力・コメント・shebang・引用の違いを確認する。 |
| [既存SQL Serverからランディングへ抽出する](<SOURCE-SQLSERVER.md>) | 既存SQL Serverを読取専用SELECTで抽出し、6契約のlanding素材を作成する。 |
| [固定SQL sourceで疑似ERP・MESを検証する](<SQLSERVER-FIXED-DEMO.md>) | 所有権付き固定SQL表への合成データ投入と、読取専用バッチ・CDCの段階的な受入を行う。 |
| [SSRF対策と内部接続の設定](<SSRF.md>) | n8nの宛先制御、内部worker通信、proxyと固定APIの境界を設定する。 |
| [初めて構築する人の一本道ガイド](<START-HERE.md>) | 新規Linux環境の準備からcore配置と合成データ検証までを順に進める。 |
| [症状から確認する](<TROUBLESHOOTING.md>) | 観測した症状から設定・起動・通信・job・データの失敗箇所を絞り込む。 |
| [PoCの評価計画と受入条件](<reports/EVALUATION-PLAN.md>) | 配置・品質・CDC・AI・復旧の評価範囲と受入条件を、実施前に記入する。 |
| [報告書用の実装構成一覧](<reports/IMPLEMENTATION-INVENTORY.md>) | 34サービス・11profile・port・volume・接続主体を公開定義から技術付録として照合する。 |

### 評価・報告担当

| 文書 | 目的 |
| --- | --- |
| [Lakehouse Foundation](<../README.md>) | MES・ERP向けPoC基盤の機能、構築入口、任意機能と報告素材を見渡す。 |
| [会社への導入・受入ガイド](<COMPANY-ROLLOUT.md>) | 会社側の担当分担、Samba、外部SQL、業務照合と導入受入条件を決める。 |
| [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>) | 公開Markdown全件の固有の目的・読者・読む時点を、目的別と読者別に引く。 |
| [実装・配置・検証の追跡表](<IMPLEMENTATION-STATUS.md>) | 依頼された機能のソース実装、配置、既存検証、残る受入を別々に追跡する。 |
| [採用版の公式リファレンスと文書監査](<REFERENCES.md>) | 採用版の公式仕様・tag・公開実装・検証記録の根拠を照合する。 |
| [外部レビュー指摘の対応と確認](<REVIEW-FOLLOWUP.md>) | PR #3の外部レビュー指摘と、再現・修正・回帰確認の対応を追跡する。 |
| [検証記録](<VALIDATION.md>) | 既存の合成環境・公開clone・CI・互換性修正で確認した条件と限界を読む。 |
| [構成図とロゴの出典](<assets/README.md>) | 全体図・AI候補図・公式ロゴの出典と、詳細図素材の配置を確認する。 |
| [図キャプションと根拠の台帳](<reports/CAPTIONS.md>) | 各図の説明文、表す範囲、引用する実装と根拠を図番号から引く。 |
| [PoCの意思決定記録テンプレート](<reports/DECISION-RECORD-TEMPLATE.md>) | 選択肢・根拠・制約・承認・再評価条件を、後から追える意思決定記録に残す。 |
| [PoCの評価計画と受入条件](<reports/EVALUATION-PLAN.md>) | 配置・品質・CDC・AI・復旧の評価範囲と受入条件を、実施前に記入する。 |
| [報告する主張と根拠の対応表](<reports/EVIDENCE-MATRIX.md>) | 報告する主張を既存検証・合成fixture・導入先受入・未評価に分けて根拠へ結ぶ。 |
| [報告書用の実装構成一覧](<reports/IMPLEMENTATION-INVENTORY.md>) | 34サービス・11profile・port・volume・接続主体を公開定義から技術付録として照合する。 |
| [データレイクハウスPoC 報告書テンプレート](<reports/POC-REPORT-TEMPLATE.md>) | 業務目的、評価条件、観測結果、残る条件、次段階の判断を報告書へ整理する。 |
| [PoC説明用のスライド構成](<reports/PRESENTATION-OUTLINE.md>) | 読み手と持ち時間に合わせ、14図の選択・説明順・話す論点を組み立てる。 |
| [PoC報告書・発表に使う図版と雛形](<reports/README.md>) | 用途別14図・キャプション・雛形を選び、PNG・PDF・PPTXへ書き出す。 |
| [PoC報告書に載せる画面の撮り方](<reports/SCREENSHOT-GUIDE.md>) | 各GUIで残す画面、先に確認する条件、写さない秘密や実データを決める。 |

### 運用担当

| 文書 | 目的 |
| --- | --- |
| [BuildKit auth session timeout: 公開前の診断draft](<BUILD-KIT-AUTH-DIAGNOSTIC.md>) | PR11後も報告されたBuildKit timeoutの事実・仮説・次の診断を分離し、未解決として引き継ぐ。 |
| [会社のプロキシ・CAを設定する](<COMPANY-PROXY.md>) | Git・uv・Docker・build・runtimeごとに社内proxyとCAを準備し、通信を診断する。 |
| [会社VMの修正を取り込み、Dokployへ再配置する](<COMPANY-UPDATE.md>) | 既存の設定・鍵・volumeを保持し、会社VMの修正を取り込んでDokployへ再配置する。 |
| [Notebook workspace準備と明示Spark検証](<COMPUTE-VALIDATION.md>) | 安全なworkspace plan/apply/probe、既存projectのSpark smoke、結果とpull policyを確認する。 |
| [サイト設定リファレンス](<CONFIGURATION.md>) | 生成器・Compose・CLIが使う設定項目と、保存先・認証・反映先を照合する。 |
| [導入・検証・停止](<DEPLOYMENT.md>) | private設定、host構成、CLIによる起動・検証・停止の操作を確認する。 |
| [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>) | 公開Markdown全件の固有の目的・読者・読む時点を、目的別と読者別に引く。 |
| [Dokploy 0.31.0でGitから配置する](<DOKPLOY.md>) | Dokploy 0.31.0の画面で、生成appName・個別Environment・7つのComposeを揃えて配置する。 |
| [GrafanaのGUI編集と保存](<GRAFANA.md>) | GrafanaのGUIでdashboardを編集・保存し、収集対象とPromQLの意味を確認する。 |
| [n8n・Airflow・Grafanaを画面から使う](<GUI-WALKTHROUGHS.md>) | n8n・Airflow・Grafanaで接続、認証、手動実行、履歴、保存を順に確かめる。 |
| [平時・更新・復旧の運用手順](<OPERATIONS.md>) | 平時点検・更新・backup・復旧を、鍵・DB・object・volumeの状態を揃えて行う。 |
| [PoC操作ポータル](<POC-PORTAL.md>) | 操作ポータルの各ページから、サービス入口・データ・診断・履歴へ移動する。 |
| [症状から確認する](<TROUBLESHOOTING.md>) | 観測した症状から設定・起動・通信・job・データの失敗箇所を絞り込む。 |
| [検証記録](<VALIDATION.md>) | 既存の合成環境・公開clone・CI・互換性修正で確認した条件と限界を読む。 |
| [PoC報告書に載せる画面の撮り方](<reports/SCREENSHOT-GUIDE.md>) | 各GUIで残す画面、先に確認する条件、写さない秘密や実データを決める。 |

### 開発・設計担当

| 文書 | 目的 |
| --- | --- |
| [変更する人のための読みやすさと検証規約](<../CONTRIBUTING.md>) | 読みやすいコード・コメント・CLI説明と、専用uv環境での品質検査規約を揃える。 |
| [AIへ渡せるデータとLangChain連携](<AI-READINESS.md>) | Goldとsnapshotの根拠をJSON・LangChain Documentへ渡し、推論やRAGとの境界を確認する。 |
| [異常シナリオを選んで素材を作る](<ANOMALY-SCENARIOS.md>) | 行・業務値とファイル転送の異常を選び、生成診断と下流の期待挙動を比較する。 |
| [GUI操作APIとSwagger](<API.md>) | Swaggerの認証と固定APIを使い、設定保存・素材生成・非同期job・参照を行う。 |
| [Lakehouse Foundation アーキテクチャ](<ARCHITECTURE.md>) | 保存・変換・操作の構成とprofileの責務を、データと制御の経路から把握する。 |
| [SQL Server の変更を Iceberg へ反映する](<CDC.md>) | SQL ServerのsnapshotとI/U/DをDebezium・Kafka・consumer経由でIcebergへ反映する。 |
| [Cloudflareを使う場所と社内に残す場所](<CLOUDFLARE.md>) | Cloudflareの入口保護・外部保存・AI候補を、社内に残す処理と分けて評価する。 |
| [バージョンと連携互換性](<COMPATIBILITY.md>) | 固定image・Python環境・Spark/Iceberg等の組合せと互換性の確認方法を調べる。 |
| [サイト設定リファレンス](<CONFIGURATION.md>) | 生成器・Compose・CLIが使う設定項目と、保存先・認証・反映先を照合する。 |
| [ソース・指標・API・計算基盤を拡張する](<CUSTOMIZATION.md>) | Gold指標・入力契約・API・計算環境の変更を、実装箇所と回帰条件へ落とし込む。 |
| [MES・ERPのダミーデータ投入](<DEMO-DATA.md>) | 6種のMES・ERP素材、画像、SQL投入、有限streamの生成仕様とCLIを確認する。 |
| [ファイルの場所と変更のしかた](<DIRECTORY-GUIDE.md>) | 目的に対応するファイル、実行経路、公開コードとprivate状態の置き場を探す。 |
| [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>) | 公開Markdown全件の固有の目的・読者・読む時点を、目的別と読者別に引く。 |
| [GUIで設定・操作する選択肢](<GUI-OPTIONS.md>) | 操作・オーケストレーション・列変換のGUIを分け、同梱機能と追加候補を比較する。 |
| [実装・配置・検証の追跡表](<IMPLEMENTATION-STATUS.md>) | 依頼された機能のソース実装、配置、既存検証、残る受入を別々に追跡する。 |
| [初めての理解から運用・設計へ進む道順](<LEARNING-PATH.md>) | 理解・操作・運用・設計の段階ごとに、練習と到達条件を選ぶ。 |
| [AIクライアントからMCPで操作する](<MCP.md>) | 認証付きMCPから固定の業務参照・運用APIを呼び、AIクライアントへ接続する。 |
| [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>) | 6契約の取り込み、最新版採用、不正行隔離、Gold集計と再投入の規則を追う。 |
| [BI・Notebook・ベクトル検索・データ系統を追加する](<POC-TOOLS.md>) | Superset・Jupyter・Qdrant・MarquezのGUIと合成デモを追加して用途別に検証する。 |
| [プロキシ設定の場所と反映方法](<PROXY.md>) | 通信主体別のproxy設定ファイル、CA受渡しと反映方法を参照する。 |
| [採用版の公式リファレンスと文書監査](<REFERENCES.md>) | 採用版の公式仕様・tag・公開実装・検証記録の根拠を照合する。 |
| [外部レビュー指摘の対応と確認](<REVIEW-FOLLOWUP.md>) | PR #3の外部レビュー指摘と、再現・修正・回帰確認の対応を追跡する。 |
| [レビュー指摘を改善へ取り込む](<REVIEW-WORKFLOW.md>) | issue・PR・依頼文書の指摘を対象commitで確認し、安全な回帰と差分へつなげる。 |
| [BashとZshの両方で手順を実行する](<SHELLS.md>) | 会社VMのZshとBashでコマンドを使い分け、対話入力・コメント・shebang・引用の違いを確認する。 |
| [既存SQL Serverからランディングへ抽出する](<SOURCE-SQLSERVER.md>) | 既存SQL Serverを読取専用SELECTで抽出し、6契約のlanding素材を作成する。 |
| [固定SQL sourceで疑似ERP・MESを検証する](<SQLSERVER-FIXED-DEMO.md>) | 所有権付き固定SQL表への合成データ投入と、読取専用バッチ・CDCの段階的な受入を行う。 |
| [SSRF対策と内部接続の設定](<SSRF.md>) | n8nの宛先制御、内部worker通信、proxyと固定APIの境界を設定する。 |
| [検証記録](<VALIDATION.md>) | 既存の合成環境・公開clone・CI・互換性修正で確認した条件と限界を読む。 |
| [構成図とロゴの出典](<assets/README.md>) | 全体図・AI候補図・公式ロゴの出典と、詳細図素材の配置を確認する。 |
| [PoCの意思決定記録テンプレート](<reports/DECISION-RECORD-TEMPLATE.md>) | 選択肢・根拠・制約・承認・再評価条件を、後から追える意思決定記録に残す。 |
| [図と出力スクリプトを変更する人へ](<reports/EDITING-GUIDE.md>) | figures.json、描画・exportコード、生成物の責務と変更後の検証を理解する。 |
| [報告する主張と根拠の対応表](<reports/EVIDENCE-MATRIX.md>) | 報告する主張を既存検証・合成fixture・導入先受入・未評価に分けて根拠へ結ぶ。 |
| [報告書用の実装構成一覧](<reports/IMPLEMENTATION-INVENTORY.md>) | 34サービス・11profile・port・volume・接続主体を公開定義から技術付録として照合する。 |
| [PoC報告書・発表に使う図版と雛形](<reports/README.md>) | 用途別14図・キャプション・雛形を選び、PNG・PDF・PPTXへ書き出す。 |

## 文書を追加・変更するとき

1. 本文の初H1と役割を決め、[JSON正本](documentation-manifest.json)に1項目を追加・更新します。
2. purpose・audience・when_to_useはその文書固有の説明にします。prerequisitesとnextには登録済みMarkdownの相対pathだけを指定します。
3. リポジトリの端末で `python3 scripts/documentation.py` を実行し、全目的欄と本一覧を生成します。マーカーの外側の本文は保持されます。
4. `python3 scripts/documentation.py --check` で登録・生成差分・ローカルリンク・見出しを検査します。外部URLの到達性は別確認です。
5. 図の生成キャプションは共通のpurpose_block APIを使います。本文生成器の出力とこの検査を両方確認します。

削除・改名時は相互参照と本文リンクも更新します。実環境の設定・証跡・記入済み報告書は登録せずGit外に保管します。
Gitのignore対象は列挙されません。既に追跡済みのファイルはignore設定だけでは対象外になりません。
削除・改名は `git rm` / `git mv`、または対象を確認した `git add -u` でindexにも反映してから生成します。追跡済みの未stage削除は欠落として検出されます。

## リンク検査の範囲と限界

inline/reference形式のMarkdownリンクと画像、HTMLのhref/src、見出しIDと明示id/nameを検査します。コードフェンス内の例は対象外です。参照先の存在、重複見出しの連番、repository外への逸脱を確認します。
外部URLへ通信しないため到達性は保証しません。独立した軽量検査であり、全CommonMark/GFM構文やGitHubの表示処理への完全適合を保証するものではありません。複雑な記法を追加した場合はGitHub上の表示とリンクも確認します。

記法の一次資料: [GFMのコードフェンス](https://github.github.com/gfm/#fenced-code-blocks)、[GitHubの見出しリンク](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#section-links)、[GitHubの相対リンク](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#relative-links)。
