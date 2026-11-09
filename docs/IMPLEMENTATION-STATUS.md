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
| Spark・Airflowの互換性 | 版・JAR・Provider・Pythonを組で固定 | Spark相互参照・手動DAG成功。uv原本配置後の再検証を継続 |
| Python環境をuvへ | uv固定・hash付きlock・自作venv、Airflow公式venv保持 | 原本Gitからのuv buildとサービス起動成功 |
| Streamlit・FastAPI・Swagger | 投入/処理/設定/状態/AI、非同期job、ローカルSwagger asset | unit/AppTest成功。配置後の一連操作を継続確認 |
| 異常シナリオを拡充 | 26種類・7preset・複数選択・影響率・高度fault同意 | 全ケース生成・API/GUIの163試験成功。配置反映待ち |
| CDC | Debezium/Kafka/consumer/Kafbat、LSN・offset・状態保持 | I/U/D各6、raw168・重複0、Silver/Gold・再起動成功 |
| AIレディー・LangChain | main snapshot固定context、Document、任意Ollama例 | 34Document生成成功。モデル推論は別途評価 |
| AIから操作するMCP | 型付きread toolsと任意mutating toolsを追加中 | SDKとHTTP/authを実装・検証中 |
| PoC向けOSS | Superset・Spark/Jupyter・Qdrant・Marquezを追加中 | 互換lock・GUI/API・seed・起動検証を進行中 |
| 操作ポータル・API充実 | サービス入口・catalog・capabilities・品質・helpを追加中 | GUI・MCPとの統合を進行中 |
| 社内proxy・CA | daemon/build/runtime/Java/uvの設定を分ける | 模擬proxy試験と会社向け手順を拡充中。実社内環境は現地確認 |
| GitHubの見栄えと図 | 公式ロゴ7種・SVG構成図2枚、詳細文書 | hero・badge・機能ナビゲーションを整備中 |
| Cloudflare用途 | Tunnel/Access/R2/Basin/AI機能の適用案 | 資格情報・外部公開・課金は未設定。採用時に別途接続確認 |

## 完了時に確認すること

公開ソースの品質検査、Git履歴の秘密値検査、原本Dockerfileからのbuild、各GUIとAPI、疑似データの一連経路、永続データの保持を確認し、[検証記録](VALIDATION.md) と機械可読結果へ反映します。

会社の本番SQL・proxy/CA・IdP、負荷、冗長化、バックアップ復元は、導入先で確認した結果を別途残します。接続先や資格情報、生ログはこの表へ掲載しません。
