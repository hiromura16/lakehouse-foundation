# PoC説明用のスライド構成

基準日: 2026-11-10（JST）。図をPowerPoint等へ配置するときの構成案です。スライド本文・結果・判断は、今回の評価に合わせて記入します。図は説明用の素材で、導入先での評価結果は [証跡台帳](EVIDENCE-MATRIX.md) から引用します。[報告書雛形](POC-REPORT-TEMPLATE.md)

## 読み手に合わせて枚数を選ぶ

| 読み手・場面 | 構成 | 伝えること |
| --- | --- | --- |
| 採用判断・短時間の説明 | 下表の01、02、04、07、13、14 | 目的、処理の意味、AIの境界、確認結果、残る条件 |
| 実装・運用のレビュー | 下表の01〜14 | 保存、通信、権限、実行、復旧までの担当と受入 |
| 会社での導入引継ぎ | 01、02、08〜12、14 | 接続準備、配置、設定反映、監視、保管、担当 |

最初のスライドで今回の判断を明記します。構成図だけを長く説明してから結論を出す流れにせず、各図をその判断の根拠へ結び付けます。

## 14枚の構成案と話す内容

| 順 | 見出し案 | 図・素材 | 話す内容と確認する点 |
| --- | --- | --- | --- |
| 01 | 今回何を判断するPoCか | [詳細構成](figures/01-platform-detail.svg) | 対象のMES/ERP、読み手の問い、今回の判断。任意profileと未接続の外部候補を明示 |
| 02 | 素材投入と変換を分けて試せる | [バッチ経路](figures/02-batch-flow.svg) | ファイル完成→ready→原本→Bronze。素材生成は下流を起動しない。SQLの読取と書込は別接続 |
| 03 | 業務データの契約を先に固定する | [報告書の6契約・指標説明](POC-REPORT-TEMPLATE.md) | 6ソースの粒度、主キー、更新時刻、UTC。会社の実表名は共有版へ入れない |
| 04 | 最新版・不正値・集計を説明できる | [メダリオン](figures/03-medallion.svg) | 最新候補を検証し不正を隔離。過去の正常版へ戻さない。Goldは工場日/受注別で多重JOINを避ける |
| 05 | CDCでI/U/Dと再開を確認する | [CDC](figures/04-cdc-sequence.svg) | 初回snapshotと変更、rawと現在値、checkpoint/offset。会社のAgent/権限/保持は別受入 |
| 06 | 設定・投入・状態をGUI/APIから扱う | [GUI/API](figures/05-control-api.svg) | 保存と反映、非同期jobとpoll、1実行制御、20件メモリ履歴。操作APIへ任意SQLを渡さない |
| 07 | AIへ渡す根拠と実行権限を分ける | [AI context](figures/07-ai-context.svg) | 表ごとのmain snapshot、上限、LangChain Document、MCP既定read-only。推論と精度評価は別段階 |
| 08 | SparkとSQLを同じIcebergへ接続する | [常駐Spark](figures/06-spark-compute.svg) | Master/Worker/HistoryとJupyter、remote/localの明示、Python/Arrow一致。3行smokeは性能値ではない |
| 09 | 社内proxyと企業CAを主体ごとに設定する | [proxy/CA](figures/08-proxy-ca.svg) | daemon pull、build、runtime、Java。NO_PROXYは経路、SSRFは接続許可。実会社の通信結果を別記 |
| 10 | 管理者・利用者・データの境界を確認する | [セキュリティ](figures/09-security-boundaries.svg) | ホスト管理と操作ポータル、共有token、非公開設定、内部サービス。企業IdP/テーブル認可の残課題 |
| 11 | 稼働とデータ品質を別々に見る | [監視](figures/10-observability.svg) | target up、ジョブ成功、品質件数、GUI保存。通知・ホスト全体・会社SQL監視の追加要否 |
| 12 | 公開Gitから配置し、状態を一組で保管する | [配置](figures/11-deployment-lifecycle.svg)・[保存と復旧](figures/12-persistence-recovery.svg) | appName、private設定、lock、再配置。原本/Iceberg/DB/暗号鍵/GUI状態、別環境復元の確認 |
| 13 | 既存検証と今回の受入を分けて示す | [検証概要](figures/13-evidence-summary.svg)・[根拠対応表](EVIDENCE-MATRIX.md) | 合成fixture、実機機能確認、模擬proxy、今回の会社受入、未測定を別欄へ。全結果の単一成功率は出さない |
| 14 | 次段階へ進む条件を決める | [次段階](figures/14-poc-roadmap.svg)・[意思決定記録](DECISION-RECORD-TEMPLATE.md) | 合格条件、残る論点、担当/期限。負荷・復元・認可・AI精度を必要性で選ぶ |

## 各スライドの配置

1. 上部へ、そのページで答える問いか結果を一文で置きます。
2. 中央に図を1点配置します。構成が細かい図は分割せず、詳細資料へ回して拡大できる形式で添付します。
3. 図の下に、何を示し何を示さないかの短いキャプションを置きます。
4. 下部に根拠ID、評価commit、観測条件を簡潔に置きます。本文の文字を小さくして全資料を詰め込みません。
5. 会社の実測を追加する場合は、既存公開の合成検証と色や列見出しだけでなく文字でも区別します。

SVGは拡大できる編集用、PNGは貼り付けやすい画像用、PDFは配布・印刷用として選びます。採用するアプリのSVG表示や日本語フォントに問題があれば、同梱PNG/PDFを使って実際の投影・印刷サイズで確認します。

## 数値を掲載するときの文例

**機能の確認:** 「固定の合成入力で、原本hash、型/隔離/集計、同じ入力の再実行を照合した。会社データと最大負荷での受入は別に行う。」

**CDCの確認:** 「専用合成DBのI/U/D各6件を確認し、consumer再起動後のraw重複が増えないことを確認した。これを会社の長時間停止・DDL変更・性能保証へ拡張しない。」

**AIの確認:** 「表とsnapshot付きcontextの生成・Document化・MCP参照を確認した。モデルの回答正確性、意味検索品質、OCRは別に評価する。」

**継続運用の判断:** 「GUI状態の再起動保持を確認した。整合したbackupを別環境へ復元する試験は未実施で、RPO/RTOは未確定である。」

各文例の根拠は [EVIDENCE-MATRIX.md](EVIDENCE-MATRIX.md) にあります。測っていない「高速化」「運用工数削減」「費用削減」の数値を追加しません。

## 画面を添えるときの撮影メモ

| 画面 | 撮ると説明しやすい場面 | 伏せる対象 |
| --- | --- | --- |
| Streamlit | 合成素材の選択→進捗→品質/Gold | 私有設定、接続先、実業務値、path |
| Dokploy | 対象serviceのrunning/one-shot0 | 実appName、URL、Environment、管理者名 |
| n8n / Airflow | 同梱の手動実行と成功履歴 | credentials、token、通知先、業務ログ |
| Grafana | 合成ジョブ前後の指標とGUI保存 | 実接続先、利用者、組織内のラベル |
| Superset / Jupyter | 合成Goldの図、remote Sparkの結果 | 会社dataset、Notebook出力、token、URI |
| Spark History / Kafbat | 計算履歴、topic/lagの確認 | 実job名、実table/topic名、接続設定 |

画面写真は今回の評価用環境で撮影します。図やモック画面を、実際のGUI操作の証拠に見せかけません。公開版の素材は合成・一般化したものとし、社内用の追記は別に管理します。
