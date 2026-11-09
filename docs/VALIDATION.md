# 検証記録

検証日: 2026-11-09。Linux x86-64の単一ホスト上に、既存アプリと別のネットワーク・DB・S3・資格情報・ランディング先を持つ環境を作成しました。公開用の機械可読結果は [verification.json](verification.json) に保存しています。接続先、管理APIの記録、認証情報、生ログは公開しません。

公開HTTPS URLからの認証不要clone、原本Compose、原本Dockerfileの `pip --require-hashes` ビルドが成功しました。この別環境でも画像保存・メダリオンの全期待値・再投入を検証済みです。[公開CI](https://github.com/hiromura16/lakehouse-foundation/actions) にもPython書式、SQL、全profileのCompose検査を定義しています。現在のローカル同等検査では140テストが成功しています。

## 実機で成功した経路

| 経路 | 実行した確認 |
| --- | --- |
| SQL Serverの素材投入 | 専用の一時DBに新規schemaと6表を作り、件数とlanding原本のハッシュを確認。素材投入器から取り込み・変換を起動しないことを確認 |
| SQL Serverの読み取り | 別の読み取り用ツールで6表を抽出し、元のCSV/JSONと行集合を照合 |
| ファイル → S3 / Bronze | 完了マーカー付き6ソースと画像1枚を取り込み、原本バイト列・SHA-256・件数を照合 |
| Silver / Quarantine / Gold | 型不正、欠損、参照不整合、重複、更新、遅延到着、分割出荷の10組の期待値を照合 |
| 同じ入力の再実行 | 初回7件取り込み、次回0件取り込み・7件skip。再変換後の集計が変わらないことを照合 |
| n8n | 同梱の手動ワークフローをGUI側DBにインポートし、暗号化した資格情報でworkerを呼び出し実行成功 |
| Airflow | 3.4.0の実コンテナから同梱DAGを手動実行し、task実行時のHTTP Connectionでworkerへ依頼してDAG成功 |
| Spark / Trino | Spark 4.1.4 + Iceberg 1.12.0で日本語・decimalを含む3行を書き、SparkとTrinoから同じ値を参照 |
| 監視 | Prometheusのworker/Prometheus自身のtargetがup、GrafanaのDBヘルスが正常 |
| GrafanaのGUI保存 | 新APIで作成・編集後、再起動と初期化の再実行でも編集を保持。dashboard providerのファイルmountがないことも確認 |

固定のedgeデータではBronzeが6表合計155行、Silverが145行、Quarantineが8行です。重複の採用・除外があるため、SilverとQuarantineの合計をBronzeと同じ件数にする検証にはしません。Goldの照合値は [expected-demo.json](../medallion/expected-demo.json) と独立したチェックSQLを正とします。

## 構成を直して再確認した点

- LakekeeperとPyIceberg間のS3アクセス方式を `client-managed` へ明示し、書き込み後のmanifestを読み戻せることを確認しました。
- TrinoのHTTP APIへ渡すSQLは末尾のセミコロンを除去し、コメント付きSQLファイルをそのまま管理できるようにしました。
- 無効行の判定列を `validation_reason` とし、MES停止理由の業務列 `reason_code` と衝突しないようにしました。
- Airflowの検証用パスワードファイルは書き込み可能なstate領域へ配置しました。公開構成の既定の初期パスワードもstate volumeへ生成します。

PythonはRuff、79文字のpycodestyle、unittest、SQLはSQLFluffのTrino方言、Composeは全profileを結合した `config` で検証します。公開GitHub Actionsにも同じ検査を定義しています。

## 導入先で確認すること

会社の本物のSQL Server、企業proxy・CA、会社LAN、IdP、業務データの量や例外、バックアップからの復旧、並列writer、冗長化、会社DBへのCDC接続とモデル推論は今回の成功結果に含めません。既存Sambaアカウントのパスワードを変更していないため、認証付きSMBコピーは別途確認が必要です。

Airflowのstandalone、固定S3資格情報、内部の未認証Trino/カタログは閉域の導入検証向けです。共有・本番利用前にはアクセス制御と運用要件を適用します。検証が成功したことだけで本番運用の準備が完了したとは扱いません。

## GUI・API・AI・CDCの追加検証

- Streamlit 1.67.0をPython 3.12/3.14で描画し、投入・設定・処理・状態・AIの操作をAppTestで確認しました。
- FastAPIは認証、秘密値非表示、0600のatomic保存、未反映状態、有限ジョブ、非同期処理、CDC固定操作、snapshot参照をunit testで確認しました。
- Swagger UI 5.34.1は公式npm archiveのSHA-512を照合し、JS/CSSをイメージ内から配信します。
- Goldのmain refを取得して版を固定し、34行を実際のLangChain Documentへ変換しました。モデル推論は実行していません。export側のコード・参照SQLのhashは、既存snapshotを作った変換版の保証には使いません。
- 別の専用試験DBでSQL Server CDCを有効化し、通常TLSと証明書名照合でDebezium 3.7.1 / Kafka 4.4.0へ接続しました。初回144行、INSERT/UPDATE/DELETE各6行を、raw・Silver・Goldで照合しました。
- CDC consumerの再起動後もrawは168件かつdistinct168件で増えず、Silverの6表が144行の初期状態へ戻ること、Kafbatでconsumer lagが0であることを確認しました。
- SQL投入GUI用には試験DBだけの専用ユーザーを作り、sysadmin権限がないこと、通常TLSと証明書名照合が成功することを確認しました。

GUIとAPIを含む公開Git原本コンテナの受け入れ結果は、最終配置後にこの記録へ追記します。
