# 検証記録

検証日: 2026-11-10（JST）。Linux x86-64の単一ホスト上に、既存アプリと別のネットワーク・DB・S3・資格情報・ランディング先を持つ環境を作成しました。公開用の機械可読結果は [verification.json](verification.json) に保存しています。接続先、管理APIの記録、認証情報、生ログは公開しません。

公開HTTPS URLからの認証不要clone、原本Compose、原本Dockerfileの `uv pip sync --require-hashes` ビルドが成功しました。この別環境でも画像保存・メダリオンの全期待値・再投入を検証済みです。[公開CI](https://github.com/hiromura16/lakehouse-foundation/actions) にもPython書式、SQL、全profileのCompose検査を定義しています。現在のローカル同等検査では260テストが成功しています。

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

GUIとAPIを含む公開Git原本を主基盤へ配置しました。SQL/landingへのbatch、2回の有限stream、秘密値を返さない設定API、保存と適用の区別、同梱Swagger asset、明示したメダリオンと隔離verifyを確認しました。素材生成直後にGold snapshotが変わらないことも照合しています。

## 操作ポータル・追加OSS・MCPの受入

公開Git原本から全7buildを実行し、全11profileを同一Composeプロジェクトへ統合しました。34service定義のうち一時smokeのspark-checkはreplicas=0、33containerは29稼働と4つの正常終了one-shotです。設定追加・再配置時に既存の秘密値・データvolume・landing先を保持しました。

- ポータルの13経路で、capabilities・catalog・snapshot preview・固定品質SQL・service一覧・allowlistヘルプ・オフライン構成図・直近履歴を確認しました。
- 26異常・7presetのcatalog、GUI/APIでの選択を実装し、実APIから `reject_spike` を指定して素材生成とGold非変更を確認しました。意図的に壊すファイルは追加同意が必要です。
- Supersetのnative認証・CSRF・Trino接続・Gold2dataset・SQL Lab、Jupyterのnative API/実kernelとSpark/Iceberg1行の読書き、Qdrantの3点検索とread-only書込み403、MarquezのGUI/APIと手動の系統eventを確認しました。vectorは合成数値の疎通試験で、意味検索の精度試験ではありません。系統eventはデモseedで、自動採取の保証ではありません。
- MCPの公式SDK2.3.0で実HTTP接続し、Bearerなし401、既定の9read tools・2resources・構造化応答と、配置済みAPIへの参照を確認しました。一時プロセスだけ書込みを有効化し、同意なし拒否、同意付き素材生成、job poll、Gold非変更を確認しています。主MCPは参照専用です。
- 公開GitからbuildしたSparkで3行をIcebergへ書き、Trinoの同じ3行を確認しました。一時検証アプリは削除しました。

追加4サービスだけを再起動し、SupersetのGUI作成dashboard、Jupyterのworkspace、Qdrantの3点、Marquezのjob保持を確認しました。他コンテナのIDと状態、主基盤の他サービスと試験SQLの起動時刻は保持しています。その後、n8n手動workflowとAirflow同梱DAGも最新構成で再実行して成功し、ポータルの全16サービス入口が利用可能でした。

## 再配置と社内proxyの検証

再配置の実試験で既存CDC登録が停止する問題を修正しました。既存connectorの設定29項目を厳密比較し、Connectが追加する同一nameだけを除いて一致する場合に無変更成功します。差異・認証失敗・HTTP失敗では更新せず停止します。offsetや履歴のリセット、PUT/PATCH/DELETEは行いません。修正後に主基盤の再配置とconsumer再稼働が成功しました。

企業proxyはローカルのBasic認証付きCONNECT・TLS模擬サーバーで、正常接続、407、CA欠落の拒否、内部bypass、redirect拒否、秘密値非表示を試験しました。企業CA未指定で全原本imageがbuildでき、n8nの任意追加CA設定も未設定・空欄・CA指定のComposeとNode動作で確認しました。GUIからPyPI/Maven/npm/GitHubの固定4HTTPS先への直接通信も成功しました。これは実会社proxy・認証方式・HTTPS検査・daemon・全Java/SDK通信の成功を意味しません。[会社での設定・受入](COMPANY-PROXY.md) に従って現地で確認します。


受入後、一時のCDC・PoC検証5アプリを削除し、主基盤と分離した試験SQLを保持しました。削除対象のvolumeが他の稼働アプリと共有されないことを事前に確認し、旧構成のデータ控えは削除対象へ含めていません。公開する設定・手順・コードと、私有の資格情報・実行データ・生ログは分けて保管しています。


## 常駐SparkとSSRFの最終受入

`compute` でSpark Master/Worker/Historyを常駐させました。Jupyterの実kernelからstandalone masterへ接続し、Python RDDが別hostのworker上で動くこと、Python minorとPandas/Arrow/NumPyの固定版一致を確認しました。remote mapInPandasで6行、専用Iceberg表へ3行を書き、Trinoから同じ3行を照合しています。Masterの実アプリreverseProxy UI、完了したHistoryと9jobsを確認し、Historyだけを再起動しても同じ履歴が残り、Master/Workerの起動時刻が変わらないことを確認しました。別kernel内だけcomputeを無効にしたlocal[2]分岐も、明示表示・2行処理・共有Historyへの記録を実動確認しました。

n8nのSSRF保護を有効化し、同梱workflow用のhostname例外を `worker` に限定しました。実HTTP Requestノードから未許可の内部監視healthを参照すると、SSRFのrestrictedエラーで拒否されました。秘密値を付けない専用試験workflowは検証後にarchive→deleteしました。許可されたworkerへの同梱メダリオンworkflowは同じ設定で成功しています。実会社proxy経由・独自nodeの保護をこの結果から保証しません。設定と受入は [SSRF.md](SSRF.md) を参照してください。


共通Spark imageのbuildは一つのserviceへ集約し、複数serviceが同じtagへ並行exportしないことを実YAMLのmerge後に検査しています。通常の再配置ではdaemonが再作成される場合があり、保存履歴と無停止性を同じ保証として扱いません。


## Docker 29・Bake・会社proxy互換性修正

2026-11-10に260自動テスト、書式・SQL検査、core6／全34サービスのCompose検査を実行しました。古いshellのlegacy NO_PROXY、大小caseの異なる追加値、private設定upgrade・GUI編集と削除、Dokploy相当のraw Compose、cwdに依存しない空CAとBake pathを確認しています。proxyとoptional CA以外のサービス名・image・volume・profile・port・commandは、修正前後の実Composeモデルで一致しました。

公開作業ブランチの原本workerを、通常サービスを起動しない一時アプリでキャッシュなしBake buildし、空CA helperのskipとビルド成功を確認しました。既存workerとTrinoの一時プロセスには到達不能proxyを設定し、内部S3への直接通信、Trinoへの依頼、隔離verify11項目を確認しました。稼働プロセスの設定やSwarmは変更していません。

既存Swarmの取得・pool検証は、Docker 29互換の厳密CLI stubと実config.pyを接続して正常・不一致・不正JSONを確認しました。ホスト直接のDocker socket権限がないため、実Swarmへのdocker infoは未実行です。会社の実proxy・DNS・認証方式の受入は引き続き現地で行います。[会社VMの更新手順](COMPANY-UPDATE.md) に、ローカル一時修正の退避、private設定更新、Dokploy Environmentへの転記、再配置と確認を記載しています。
