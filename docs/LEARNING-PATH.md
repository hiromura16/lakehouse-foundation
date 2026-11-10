# 初めての理解から運用・設計へ進む道順

<!-- doc-purpose:start -->
> **役割:** 理解・操作・運用・設計の段階ごとに、練習と到達条件を選ぶ。
>
> **対象読者:** 初めて使う人 / データ利用担当 / 開発・設計担当
>
> **読むタイミング:** 読む順番が分からないときや、次に学ぶ操作を決めるとき。
>
> **前提となる資料:** [Lakehouse Foundation](<../README.md>)
>
> **次に読む資料:** [用語を役割と実際の操作へ結び付ける](<GLOSSARY.md>) / [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

このページは、読みたい文書が分からないときの入口です。構築を実行する一本道は [START-HERE](START-HERE.md)、用語は [GLOSSARY](GLOSSARY.md)、全ファイルの役割は [文書マニフェスト](DOCUMENTATION-MANIFEST.md) で確認します。

## 1. 初学者: 役割を分けて1回の往復を理解する

Zsh端末で手順を進める場合は、先に [Bash / Zshガイド](SHELLS.md) のコメント準備と入力方法を確認します。各例の共通/専用ラベルを見て、自分のシェルを選びます。

最初の目標は全製品の管理ではなく、「素材を作る」「受け取る」「整える」「結果を見る」が別の操作だと説明できることです。会社データを使わず、専用の合成検証環境で行います。

| 順序 | 実際にすること | 理解できたと判断する条件 | 文書 |
| --- | --- | --- | --- |
| 1 | 保存・処理・管理の役割を読む | S3、Iceberg、catalog、Trino、workerを区別できる | [全体構成](ARCHITECTURE.md) |
| 2 | hostとDocker/uvを準備 | 通常ユーザーからDocker Serverへアクセスできる | [host準備](HOST-SETUP.md) |
| 3 | private設定を生成してcore配置 | 設定検査、healthy、one-shot0を確認できる | [初回構築](START-HERE.md) |
| 4 | 隔離verifyを実行 | 11検査と再投入7skipの意味が分かる | [メダリオン](MEDALLION.md) |
| 5 | demo/monitoringを追加 | GUI/API/Grafanaを正しいhost portへ開ける | [GUI入口](POC-PORTAL.md) |
| 6 | GUIからclean素材を1回だけ生成 | manifestとjob成功を確認し、まだ変換前と説明できる | [素材GUI](DEMO-GUI.md) |
| 7 | 明示medallion→Gold/品質を見る | Silver/隔離/Gold、粒度、snapshotと抜粋を区別できる | [データ処理](MEDALLION.md) |

`verify`は通常業務表とは別のnamespaceで固定fixtureを処理します。GUI runは独立IDの素材を通常のBronzeへ加えるため、GUIの累積件数をfixture155行と比較しません。失敗したボタンを連打せず、jobとworkerの状態を先に見ます。

## 2. 実務担当: 同じ操作を繰り返して状態を引き継ぐ

| 課題 | 小さな練習 | 確認すること |
| --- | --- | --- |
| 不正値の理由 | cleanと品質異常を別の試験環境/範囲で比較 | 元文字列・validation_reason、最新版の扱い |
| 設定変更 | 空いているport等の非秘密設定を保存し、反映待ちを読む | 保存だけでは稼働設定が変わらないこと |
| 自動化の入口 | n8nかAirflowの同梱手動処理を1回使う | workerへ認証し、成功/失敗と結果を確認 |
| 保存 | Grafanaの題名やpanelを編集してSave | 初回seedとGUI保存の関係、再初期化での保持 |
| API | SwaggerでGET→POST受付→job poll | 202は受付、job成功/DB commit/Iceberg反映は別段階 |
| 会社SQL | 新しい空の会社用環境でread-only抽出 | TLS・権限・6表map・同じ範囲の業務照合 |

練習で変更した値・対象・結果を私有記録へ残します。DB passwordや暗号鍵の単独書換えを、通常のport変更と同じ扱いにしません。定期実行はn8n/Airflowのどちらか一つを担当にします。[運用](OPERATIONS.md)・[API](API.md)・[会社SQL](SOURCE-SQLSERVER.md)

## 3. 設計・開発担当: 境界を理解して変更する

| 設計上の問い | 読む実装・文書 | 変更の受入 |
| --- | --- | --- |
| 別のMES/ERPを受け入れるには | sources.json、抽出map、Bronze/Silver/隔離SQL | schema・主キー・時刻・旧データのmigrationを別環境で確認 |
| 指標を追加するには | 03_gold.sql、固定品質checks、catalog/API型 | 粒度・分母0・多重JOIN・期待値・利用側を揃える |
| CDCを止めて再開するには | consumer/state、Kafka offset/history、DB保持 | raw重複・現在値・派生表・checkpoint・lagを同じ範囲で照合 |
| Sparkを更新するには | Spark minor/Scala/Java/Iceberg JAR/Python依存 | remote executor・Arrow・S3・Trino・Historyを組で検証 |
| AIへ何を渡すか | snapshot固定context、MCP read/write、権限 | 抜粋と全件、根拠と生成版、Documentと推論を区別 |
| 本番へ進めるか | 認証/認可、性能、監視、backup/復元 | 会社で測定した条件と結果で判断。PoC構成名で代用しない |

[拡張ガイド](CUSTOMIZATION.md) に変更対象と確認順、[互換性](COMPATIBILITY.md) に版の組合せ、[評価計画](reports/EVALUATION-PLAN.md) に受入の記入欄があります。コードを読む際は [CONTRIBUTING](../CONTRIBUTING.md) のdocstring/help/コメント規約を使います。

## 4. 報告する人: 機能・実測・計画を分ける

1. [図版集](reports/README.md) から読み手に必要な3〜6枚を選びます。
2. [根拠対応表](reports/EVIDENCE-MATRIX.md) で、既存記録・固定fixture・今回の受入・未評価を分けます。
3. [報告書雛形](reports/POC-REPORT-TEMPLATE.md) へ今回の目的、条件、結果、判断を記入します。
4. [意思決定記録](reports/DECISION-RECORD-TEMPLATE.md) で、次に進む条件と担当を合意します。

図13の合成値を会社で測った値へ読み替えません。性能・費用・運用工数・モデル精度は、比較対象と測定条件がない場合は未評価として残します。

## 5. 途中で止まったときの戻り先

| 状況 | まず戻る場所 |
| --- | --- |
| 何を入力するか分からない | 該当文書冒頭の役割/前提と [設定一覧](CONFIGURATION.md) |
| localhostで開けない | [初回ガイド](START-HERE.md) のSSH転送、公開port、選択profile |
| SQLや処理が途中で止まった | [障害対応](TROUBLESHOOTING.md)。timeoutを停止と断定しない |
| データが消えたように見える | appName、project、volume prefix、bucket/warehouseの対応 |
| 設定更新がbackupで止まる | [更新手順](COMPANY-UPDATE.md)。既存backupを確認・退避 |
| source/テストを直す | [開発規約](../CONTRIBUTING.md) と [レビュー対応](REVIEW-FOLLOWUP.md) |

理解を確認するため、操作した内容を別の担当者へ「入力」「確定したもの」「残る状態」「次の確認」の4点で説明します。製品名やコマンドを暗記することより、二重実行・誤接続・不正な結果判断を防ぐことを重視します。
