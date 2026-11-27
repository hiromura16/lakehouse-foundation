# 外部ファイルのLanding入力契約

<!-- doc-purpose:start -->
> **役割:** CSV・JSON Lines・画像の契約と完成印を確認する。
>
> **対象読者:** データ利用担当 / 開発・設計担当
>
> **読むタイミング:** 外部アプリからLandingへファイルを届けるとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>)
>
> **次に読む資料:** [GUIで外部データの処理を確認する](<DEMO-GUI.md>) / [外部入力の受入・隔離を検証する](<ANOMALY-SCENARIOS.md>) / [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

外部アプリが原本を配置し、Foundationが取り込みます。[外部データの入口](../README.md#外部データの入口) に各外部アプリを整理しています。

## 6つの入力契約

正本は [sources.json](../medallion/sources.json) です。CSVにはヘッダーを付け、JSON Linesは1行1オブジェクトです。すべてUTF-8で、値は数値・日付も含め文字列です。時刻はUTCのISO 8601、日付は `YYYY-MM-DD` です。

| ソース | ファイル | 主キー・参照 |
| --- | --- | --- |
| ERP製品マスター | `csv/erp_products.csv` | `product_id` |
| ERP製造指図 | `csv/erp_work_orders.csv` | `work_order_id`、`product_id`を参照 |
| ERP受注 | `csv/erp_sales_orders.csv` | `sales_order_id`、`product_id`を参照 |
| ERP出荷 | `csv/erp_shipments.csv` | `shipment_id`、`sales_order_id`を参照 |
| MES製造実績 | `json/mes_events.jsonl` | `event_id`、`work_order_id`を参照 |
| MES停止履歴 | `json/mes_downtime.jsonl` | `downtime_id`、`work_order_id`を参照 |

全ソースの `updated_at` は、その業務キーの版の新しさを示します。MESの `event_time` や停止開始・終了時刻は実際の事象時刻です。この区別により遅れて到着した実績を元の操業日に集計できます。原本をBronzeへ取り込む際に、取り込み側が `_source_file`・`_source_hash`・`_ingested_at` を付けます。

## 完成印、画像、再処理

原本の書き込みが終わってから、同じ場所へ `原本名.ready` を公開します。内容は原本のSHA-256（小文字64文字と改行）です。書きかけのファイルは処理しません。画像は `images/` に置き、拡張子・デコード・ハッシュを確認します。対応する業務パス・列を持たない任意のファイルは、業務Silverへは自動変換されません。

外部ファイル生成アプリの既定形式も受け付けます。Landing内の配送ルート（既定はrun/job単位）にある `manifests/*.ready.json` は `source.file-manifest.v2` と `artifacts`（その配送ルートからの相対path、bytes、sha256）を宣言します。全原本のサイズとSHA-256、対応する形式（業務CSV/JSONLの列、非圧縮JSON/JSONLの構文、CSVのUTF-8、画像のデコード・拡張子）を検査してから受領対象にします。任意階層や圧縮・その他ファイルは業務列を自動判定せず、認識できない形式はRaw原本として保存します。原本は1件16MiB、manifest自体は2MiB、原本総量は1バッチ64MiB、最大1024件です。宣言と不一致のバッチは受領せず、完了印・原本を移動せず残して他のバッチの処理を続けます。結果に拒否件数と最大20件の安全な識別子・理由を返します。識別子はLandingから見たマーカーパスのSHA-256の先頭16桁です。監視用の論理隔離であり、別ディレクトリへの移動や元ファイルの削除はしません。修正と再配送後に再試行します。正常な完成印でも業務契約を満たさない行は既存のQuarantineルールに従います。Raw原本の受領、業務パス・列の解析、Silver/Gold変換は別々に確認します。既定のrun/job配下のCSV・JSON・画像はRaw（画像は寸法等も記録）に受け入れます。型付きの製品処理では外部アプリの互換モードが置く正規の増分名 `csv/erp_products__<hex12>_<4桁>.csv` と隣接 `.ready` を使い、ファイルのベース名だけで契約を推定しません。

[Sambaの受け口とready](DEPLOYMENT.md#8-sambaのランディング共有を追加する) · [変換・再処理・Quarantine](MEDALLION.md) · [固定の契約fixture](../samples/README.md)
