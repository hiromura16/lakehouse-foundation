# 実装・配置・検証の追跡表

更新日: 2026-11-10（JST）。依頼履歴を照合し、ソース実装、実環境への配置、検証を別々に追跡します。進行中の機能を、起動確認だけで完了とは扱いません。

| 依頼 | ソース・構成 | 実機の状態／残る確認 |
| --- | --- | --- |
| 会社で公開Gitから導入 | 公開Git、fresh履歴、外部private設定、公開検査 | 認証不要clone・原本build・core検証成功 |
| 自宅の名前・IP・パスを除去 | 変数化、sourceと履歴と実秘密値の照合 | 公開commitごとに検査を継続 |
| DokployとProject整理 | 一つのProject内で基盤と試験ソースを分離 | 公開Gitから主基盤を配置済み |
| SQL Server Developerは試験用だけ | 公開構成にDB本体を含めず、外部SQL設定を提供 | 既存12件・checksum・volumeを保持して独立化 |
| Sambaランディング | 外部LANDING_DIR、既存認証を維持する設定 | path切替・testparm・SMB交渉成功。認証付きコピーは別確認 |
| Icebergメダリオン | Bronze/Silver/Quarantine/Gold、6MES/ERPソース | 固定期待値・画像原本・再投入skip成功 |
| SQLの素材投入・読取 | 専用投入schema、読み取り専用抽出、TLS/CA | 両ツール実機成功、GUI用DB限定writerを用意 |
| n8nを新構成で保持 | 専用PostgreSQL・暗号鍵・volume、現行安定版固定 | 同梱workflow実行成功。旧構成は削除、データ控えは保持 |
| GrafanaのGUI保存 | 初回だけimport、既存dashboardは上書きしない | 編集・再起動・再初期化後の保持を確認 |
| Spark・Airflowの互換性 | 版・JAR・Provider・Pythonを組で固定 | 公開Git uv imageのSpark/Trino3行、手動DAG成功 |
| Sparkの常時起動 | Master/Worker/History、Jupyter remote/local明示、履歴volume | 実worker Python/Arrow、Iceberg/Trino、Master UI、History再起動保持を確認 |
| SSRFの設定と説明 | n8n保護有効・workerだけ内部例外、GUI6設定・専用手順 | 実HTTPノードの拒否と正規workflow成功、誤入力拒否を確認 |
| Python環境をuvへ | uv固定・hash付きlock・自作venv、Airflow公式venv保持 | 原本Gitからのuv buildとサービス起動成功 |
| Streamlit・FastAPI・Swagger | 投入/処理/設定/状態/AI、非同期job、ローカルSwagger asset | unit/AppTest、公開Git配置後のSQL/landing/stream/処理/設定成功 |
| 異常シナリオを拡充 | 26種類・7preset・複数選択・影響率・高度fault同意 | 26生成とGUI試験、配置APIで選択投入・Gold非変更を確認 |
| CDC | Debezium/Kafka/consumer/Kafbat、LSN・offset・状態保持 | I/U/D各6、raw168・重複0、Silver/Gold・再起動成功 |
| AIレディー・LangChain | main snapshot固定context、Document、任意Ollama例 | 34Document生成成功。モデル推論は別途評価 |
| AIから操作するMCP | 9read tools・2resources、任意の3write tools | 公式SDKのHTTP/Bearer/全readと任意write同意を確認 |
| PoC向けOSS | Superset・Spark/Jupyter・Qdrant・Marquez任意profiles | 公開Gitから主基盤へ配置、native API/kernel/検索/系統と再起動後の保存保持を確認 |
| 操作ポータル・API充実 | サービス入口・catalog・capabilities・品質・helpを実装 | GUI AppTestと本体の固定API13経路を確認 |
| 社内proxy・CA | daemon/build/runtime/Java/uvの設定を分ける | 認証CONNECT/TLS模擬試験、企業CA build/n8n対応。実社内は現地確認 |
| GitHubの見栄えと図 | 公式ロゴ7種・SVG構成図2枚、詳細文書 | SVG hero・実CI badge・機能ナビ・説明文書を公開 |
| Cloudflare用途 | Tunnel/Access/R2/Basin/AI機能の適用案 | 資格情報・外部公開・課金は未設定。採用時に別途接続確認 |

## 文書の版監査

2026-11-10に全公開Markdownを再監査しました。Dokploy 0.31.0の固定tagで画面名、生成後appName、サービス変数と `.env` 生成を確認し、初心者の作業順を [START-HERE.md](START-HERE.md) に統一しました。他の採用版の一次資料と文書ごとの照合内容は [REFERENCES.md](REFERENCES.md) にあります。今回の文書修正のためにlive環境を更新した、または会社接続を再検証したという意味ではありません。

## 完了時に確認すること

公開ソースの品質検査、Git履歴の秘密値検査、原本Dockerfileからのbuild、各GUIとAPI、疑似データの一連経路、永続データの保持を確認し、[検証記録](VALIDATION.md) と機械可読結果へ反映します。

会社の本番SQL・proxy/CA・IdP、負荷、冗長化、バックアップ復元は、導入先で確認した結果を別途残します。接続先や資格情報、生ログはこの表へ掲載しません。
