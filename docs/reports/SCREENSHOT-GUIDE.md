# PoC報告書に載せる画面の撮り方

図版は公開実装の説明で、実GUIの証跡画像ではありません。会社での実施結果は、対象環境・commit・操作・期待結果を私有記録へ残し、必要な画面だけを社内報告書へ添えます。以下は撮影計画です。未取得画像を「検証済みの実画面」として扱いません。

| 画面・箇所 | 何を示すか | 先に確認すること | 画像に出さないもの |
| --- | --- | --- | --- |
| Dokploy Containers | core稼働とone-shot正常終了 | 対象Project/Environment、配置commit | 管理URL、実appName、実host、ログの秘密 |
| Streamlit 素材の投入 | batch/有限stream・異常選択 | 合成検証用の環境、素材だけの操作 | SQL接続先、private path、実在顧客名 |
| Streamlit 実行結果 | 生成jobの状態とmanifest | job ID・run条件、処理jobとの区別 | 実schema、ソースの実path |
| Streamlit データ | catalog・品質件数・Gold抜粋 | 粒度・snapshot・各表上限 | 業務明細、製品/受注/工場の実ID |
| Swagger | 固定APIと202→job poll | Authorize欄を閉じる、確認用GETから開始 | token、curl例のAuthorizationヘッダー |
| n8n workflow/Executions | workerへの手動依頼と結果 | credentialsを閉じる、実行範囲 | Header Auth値、実接続URL、payload実データ |
| Airflow Grid/Logs | 手動DAG/taskの成功 | run時刻と対象を私有記録で確認 | Connection password、初期ログイン情報 |
| Spark Master/Worker | worker登録・remote実行 | Notebook側のmasterと実executor | 実host名、内部IP、入力の実path |
| Spark History | 終了後に残った実行履歴 | event logと実行IDの対応 | SQL内の実名、環境変数・秘密 |
| Kafbat Connect/Topics | connector/task・topic・lag | I/U/D各段階とraw/現在値を別照合 | connector資格情報、実DB、業務イベント本文 |
| Grafana | worker指標・GUI編集の保存 | 収集対象2種と時間範囲 | datasource秘密、内部host、無関係な業務情報 |
| Superset SQL Lab/Dashboard | Gold2表の探索と保存物 | SQL実行・Dataset/Chart/Dashboardを区別 | SQLに含む実名、顧客/価格の明細 |
| JupyterLab | remote Spark・保存workspace | Notebookの出力も点検 | token入りURL、実path、実データ、env全表示 |
| Qdrant Dashboard/Console | 合成3点の検索と権限制限 | 合成vectorであることを注記 | API key、業務payload、個人の文書本文 |
| Marquez Graph | 手動demo lineageの表示 | 自動採取ではないことを注記 | 実namespace・job名・組織/host識別子 |
| AIの準備 | 表名・snapshot付きJSON | 推論ではなくcontext取得であること | 業務行、外部AI送信先、secret |

## 撮影の前後に行うこと

1. 合成試験の専用環境を使い、画面の対象・表示条件・期待結果を記録します。会社データ用環境で合成素材を追加しません。
2. アドレスバー、タブ、サイドバー、ユーザーアイコン、通知、clipboard表示、開発者ツールを点検します。必要な内容だけを切り出します。
3. 元の画面は会社の管理された保存先へ置き、報告用copyで識別子を一般化します。ぼかしだけでなく、秘密部分の画素を不透明な面で完全に置き換えます。
4. 画像に埋め込まれたメタデータ、代替テキスト、スライドノート、PDF内テキストにも秘密が残らないことを確認します。GUI exportのJSONやNotebookの出力も別に点検します。
5. キャプションへ「対象環境の種別」「操作」「確認できたこと」「確認していないこと」を書きます。社内の実URLは公開Gitへ記載しません。

公開リポジトリへ実画面を追加する場合は、画像の目視とメタデータの確認、会社の公開可否確認、秘密値/実データの検査を別途行います。現在の素材集はSVGから作る説明図だけを自動出力し、実画面の撮影・収集を実行しません。[素材集の使い方](README.md)
