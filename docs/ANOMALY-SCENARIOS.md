# 異常シナリオを選んで素材を作る

<!-- doc-purpose:start -->
> **役割:** 行・業務値とファイル転送の異常を選び、生成診断と下流の期待挙動を比較する。
>
> **対象読者:** データ利用担当 / 開発・設計担当
>
> **読むタイミング:** 正常素材との差分、隔離、重複、破損・未完成入力を試す計画を立てるとき。
>
> **前提となる資料:** [GUIからダミーデータを投入する](<DEMO-GUI.md>) / [MES・ERPのダミーデータ投入](<DEMO-DATA.md>)
>
> **次に読む資料:** [MES・ERPデータをBronzeからGoldへ整える](<MEDALLION.md>) / [PoCの評価計画と受入条件](<reports/EVALUATION-PLAN.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

この機能は、異常を含む合成素材を意図して生成し、取り込み・隔離・重複排除・Goldの変化を確認するためのものです。会社の実データを改変する機能ではありません。素材生成から後続処理は起動せず、生成結果と下流の期待挙動を分けて確認します。

確認日: 2026-11-10（JST）。操作GUIの起動は [初回ガイド](START-HERE.md) を使います。以下のシナリオは本基盤固有で、StreamlitやSQL Serverの標準機能一覧ではありません。

共有の正本は [source_scenarios.py](../scripts/source_scenarios.py) です。APIの `GET /scenarios`、GUI、CLI、生成処理はこのカタログと検査を使います。現在は行・業務値の19ケースと、ファイル・転送の7ケースを用意しています。

## 最初の試し方

1. 専用の検証環境とランディングを選び、「素材の投入」を開きます。
2. 「異常シナリオのプリセット」から目的を選びます。原因を見分ける最初の比較には正常な `clean` を基礎にします。
3. カテゴリで候補を絞り、複数選択欄で追加・除外します。カテゴリを変えても既に選んだ異常は残り、外すまで適用します。
4. 表にある「素材で変わること」と「期待する下流の挙動」を読み、影響率を決めます。
5. ファイル異常は明示同意が必要です。画像破損には画像素材の生成も選びます。
6. 素材生成を開始し、ジョブの診断表・対象件数・対象ファイル・manifestを確認します。
7. 素材が完成してから必要な後続処理を明示実行し、QuarantineやGold、拒否・無視の結果を照合します。

「標準・手動で選ぶ」の初期値は従来の `edge`、追加異常なしです。従来の `clean` / `edge` に追加異常を指定しなければ、既存の固定検証に使う値を変更しません。presetは選択の出発点で、後から個別に編集できます。別のpresetを選んだ場合にだけbaseと選択集合を置き換えます。

## プリセット

| ID | 画面の名前 | 基礎データ | 追加する異常ID |
| --- | --- | --- | --- |
| `clean` | 正常データ | `clean` | なし |
| `legacy_edge` | 従来のedgeデータ | `edge` | なし |
| `quality` | 型・必須・参照エラー | `clean` | `missing_required`, `invalid_number`, `broken_reference`, `invalid_datetime` |
| `revisions` | 重複・改訂・順序逆転 | `clean` | `duplicate_row`, `conflicting_revision`, `out_of_order` |
| `factory_losses` | 不良増・長時間停止 | `clean` | `reject_spike`, `long_downtime` |
| `shipping_delay` | 出荷遅延 | `clean` | `delayed_shipment` |
| `transfer_faults` | 未完了・文字コード異常 | `clean` | `missing_ready`, `invalid_encoding` |

正常、従来edge、品質、改訂、工場ロス、出荷遅延、転送の切り分けを入口にしています。品質presetと工場ロスpresetを最初から重ねるより、一つずつの基準値を確認してから組み合わせます。

## 行・業務値の19ケース

これらはlanding、sqlserver、bothで使えます。SQLへの書き込みは従来と同じ専用DEMO DBと明示同意を必要とし、nvarcharの素材表へ保持します。下流の期待挙動は既存のメダリオンSQLを実行した場合のものです。

| ID・名前 | 対象 | 実際に変える内容 | 期待する下流の挙動 |
| --- | --- | --- | --- |
| `missing_required` 必須キー欠損 | `mes_events` | 必須キー欠損を選択した合成ソースへ注入します。 | 最新版のキー欠損をmissing_keyとして隔離します。 |
| `invalid_number` 数値の型不正 | `mes_events` | 数値の型不正を選択した合成ソースへ注入します。 | 数値へ変換できずinvalid_typeとして隔離します。 |
| `negative_quantity` 負の数量 | `mes_events` | 負の数量を選択した合成ソースへ注入します。 | 負数をnegative_quantityとして隔離し、生産集計から除きます。 |
| `duplicate_row` 完全重複 | `mes_events` | 完全重複を選択した合成ソースへ注入します。 | 同じevent_idは1件採用され、Goldを二重計上しません。 |
| `conflicting_revision` 後着の有効改訂 | `mes_events` | 後着の有効改訂を選択した合成ソースへ注入します。 | 有効な元行では新しい改訂を採用し、Goldの良品数が10増えます。 |
| `invalid_latest` 最新版だけ不正 | `mes_events` | 最新版だけ不正を選択した合成ソースへ注入します。 | 最新版をinvalid_typeで隔離し、古い有効版へは戻しません。 |
| `broken_reference` 指図への参照切れ | `mes_events` | 指図への参照切れを選択した合成ソースへ注入します。 | 参照先がなくunknown_work_orderとして隔離します。 |
| `invalid_datetime` 日時の型不正 | `mes_events` | 日時の型不正を選択した合成ソースへ注入します。 | 日時へ変換できずinvalid_timestampとして隔離します。 |
| `late_arrival` 過去イベントの遅延到着 | `mes_events` | 過去イベントの遅延到着を選択した合成ソースへ注入します。 | 有効な遅延到着として過去日のGoldへ集計します。自動隔離はしません。 |
| `out_of_order` ファイル内の順序逆転 | `mes_events` | ファイル内の順序逆転を選択した合成ソースへ注入します。 | 順序が変わっても主キーとupdated_atによる採用・Goldは変わりません。 |
| `clock_skew` 更新時計が1年進む | `mes_events` | 更新時計が1年進むを選択した合成ソースへ注入します。 | 将来時計も現行SQLでは受理します。自動隔離・自動警告はありません。 |
| `reject_spike` 不良数の急増 | `mes_events` | 不良数の急増を選択した合成ソースへ注入します。 | Goldの不良数が増え歩留まりが下がります。業務異常の自動警告はありません。 |
| `long_downtime` 12時間の設備停止 | `mes_downtime` | 12時間の設備停止を選択した合成ソースへ注入します。 | 停止時間を720分として開始日に集計します。自動隔離はしません。 |
| `overproduction` 計画を大幅に超える良品 | `mes_events` | 計画を大幅に超える良品を選択した合成ソースへ注入します。 | Goldの良品数が増え、計画達成率が100%を超え得ます。自動隔離はしません。 |
| `zero_production` 良品・不良ともゼロ | `mes_events` | 良品・不良ともゼロを選択した合成ソースへ注入します。 | Goldの生産量が減り、日全体で総数0なら歩留まりはNULLになります。 |
| `delayed_shipment` 30日遅れの出荷 | `erp_shipments` | 30日遅れの出荷を選択した合成ソースへ注入します。 | Goldの遅延出荷数・遅延数量へ計上します。自動隔離はしません。 |
| `missing_shipment` 受注の出荷が全て欠落 | `erp_shipments` | 受注の出荷が全て欠落を選択した合成ソースへ注入します。 | その受注のGoldは出荷0・未出荷数=受注数・履行率0%になります。 |
| `over_shipment` 受注を大幅に超える出荷 | `erp_shipments` | 受注を大幅に超える出荷を選択した合成ソースへ注入します。 | 履行率が100%を超え、未出荷数は0で下限処理します。自動隔離はしません。 |
| `invalid_price` 単価の型不正 | `erp_sales_orders` | 単価の型不正を選択した合成ソースへ注入します。 | 受注をinvalid_typeで隔離し、その出荷も参照切れとして隔離します。 |

clock_skewや過剰生産・過剰出荷などは、現在のルールで有効な値として採用されるケースです。異常に見える値をすべて自動隔離する実装とは扱いません。検知を追加したい場合は、業務ルールと閾値をSQL・検証へ明示します。

## ファイル・転送の7ケース

landingまたはbothのみで使えます。bothでは行異常をSQL素材にも適用しますが、ファイル破損はファイル側だけです。高度なファイル異常への同意を選ぶまではGUIから送信できず、APIでも `allow_file_faults: true` が必要です。選んだファイル異常の集合を変更するとGUIの同意を解除します。

| ID・名前 | 対象 | 実際に変える内容 | 期待する下流の挙動 |
| --- | --- | --- | --- |
| `extra_column` 余分なCSV列 | `csv/erp_products.csv` | 余分なCSV列を選択した合成ソースへ注入します。 | readyのhashは正しくても、列契約不一致で取り込みを拒否します。SQLには影響しません。 |
| `missing_column` CSV列の欠落 | `csv/erp_products.csv` | CSV列の欠落を選択した合成ソースへ注入します。 | readyのhashは正しくても、列契約不一致で取り込みを拒否します。SQLには影響しません。 |
| `missing_ready` 完了マーカー欠落 | `csv/erp_products.csv` | 完了マーカー欠落を選択した合成ソースへ注入します。 | そのファイルは取り込み対象から無視されます。他ファイルは別に処理され得ます。 |
| `wrong_checksum` 完了ハッシュ不一致 | `csv/erp_products.csv` | 完了ハッシュ不一致を選択した合成ソースへ注入します。 | 完了hashの不一致で取り込みを拒否します。SQLには影響しません。 |
| `truncated_json` 途中で切れたJSON | `json/mes_events.jsonl` | 途中で切れたJSONを選択した合成ソースへ注入します。 | 内容のready hashは一致しますが、JSON解析で取り込みを拒否します。 |
| `invalid_encoding` UTF-8以外のバイト | `json/mes_events.jsonl` | UTF-8以外のバイトを選択した合成ソースへ注入します。 | 内容のready hashは一致しますが、UTF-8復号で取り込みを拒否します。 |
| `corrupt_image` 破損した検査画像 | `images/inspection.png` | 破損した検査画像を選択した合成ソースへ注入します。 | 内容のready hashは一致しますが、画像検証で取り込みを拒否します。 |

表のpathはソース契約上の名前です。GUIが生成した実際のpathにはrun ID・batch番号が入るため、診断結果の `file_faults[].path` を確認します。corrupt_imageだけは `images: true` も必要です。

ファイル異常は生成ジョブの失敗を意味しません。たとえばwrong_checksumを選んで不一致のmarkerを正しく作れた場合、生成はsucceededで、後続の取り込み拒否が想定した結果です。missing_readyは無視されることを確認するケースです。通常の転送失敗や認証失敗による生成ジョブのfailedとは区別します。

## 影響率・seed・組み合わせ

`anomaly_rate` は1〜100、既定10です。対象表から、行数×率を切り上げて選びます。対象があれば通常は最低1行、順序逆転では対象数の範囲内で最低2行です。削除、複製、改訂、同じ行への複数操作を含むため、最終的な行数やQuarantine率が指定率と一致する保証はありません。ファイル異常は対象ファイル全体に作用し、影響率によって壊すバイトの割合を変えません。

行の選択はseed、batch番号、異常IDから決定します。同じ入力条件で再現できます。GUIは別run IDごとにキーを分離するため、同じseedでも別runは独立した素材です。件数比較ではbase、seed、orders、days、batch、選択異常、率と、参照したmanifestを記録します。

異常はIDの安定した順序で適用します。互いに値を上書きする次の組み合わせは、一つのgroupから2種類以上を選べません。

- `extra_column`、`missing_column`
- `missing_ready`、`wrong_checksum`
- `invalid_encoding`、`truncated_json`
- `invalid_number`、`negative_quantity`、`overproduction`、`zero_production`
- `reject_spike`、`zero_production`
- `invalid_datetime`、`late_arrival`
- `clock_skew`、`late_arrival`
- `delayed_shipment`、`missing_shipment`、`over_shipment`

GUIは競合内容を表示して送信を止め、API/CLIも同じ共通検査で拒否します。許可された組み合わせでも、同じ行へ複数の変化が重なることがあるため、各ケースの件数・Gold影響を単純加算しません。まず単独ケースのcleanとの比較から始めます。

## APIとCLIで指定する

認証付き `GET /scenarios` はversion、シナリオ、preset、競合groupを返します。`POST /jobs` の例です。

```json
{
  "scenario": "clean",
  "anomalies": ["missing_required", "invalid_number"],
  "anomaly_rate": 20,
  "destination": "landing"
}
```

追加異常は最大32個で、重複・未知IDは拒否します。ファイル異常には `allow_file_faults: true`、画像破損には `images: true` を追加します。詳細な認証とジョブ進捗は [API.md](API.md) を参照してください。

CLIでも同じケースを指定できます。以下は素材を作るだけで、workerを呼びません。既存出力へ上書きせず、専用の新しい出力先を選びます。

```bash
python3 scripts/generate-demo.py \
  --output /srv/lakehouse/anomaly-tests/quality \
  --scenario clean --seed 42 --orders 20 --days 7 \
  --anomaly missing_required --anomaly invalid_number \
  --anomaly-rate 20

python3 scripts/generate-demo.py \
  --output /srv/lakehouse/anomaly-tests/transfer \
  --scenario clean --anomaly wrong_checksum \
  --allow-file-faults
```

SQL接続が必要なCLI環境は [DEMO-DATA.md](DEMO-DATA.md) のuv手順を使います。これらの出力先を既存の会社用landingへ混在させず、後続処理を使う場合はその検証用環境の `LANDING_DIR` と整合させます。

## 診断結果の読み方

GUIの「意図した異常の生成結果」とmanifestの `diagnostics` に同じ情報を残します。

| 項目 | 意味 |
| --- | --- |
| `anomalies` | 実際に適用したID。安定した順序で保持 |
| `anomaly_rate` | 指定した行選択率 |
| `row_changes[].id/table/affected` | ケース、対象表、生成時に操作した対象数 |
| `file_faults[].id/path/expected_effect` | ケース、生成先の実ファイル名、想定される下流の扱い |
| `intentional_file_faults` | ファイル異常を意図して生成した印。生成失敗のフラグではない |

`affected` は生成側の操作数です。重複排除、改訂選択、表間参照、複数条件が関係するため、最終のSilver除外件数やGold差分は別に照合します。追加異常が空なら従来と同じmanifest形式を保持し、diagnosticsを追加しません。

想定した拒否が起きても、先に処理された別ファイルや前回の結果を確認します。破損素材を直して業務の正常ファイルとして扱う場合は、元の試験manifestを残し、別の正常runで生成し直します。既存共有のデータ削除やvolume削除で試験をリセットしません。

## 期待する結果を比較する手順

正常runと異常runのmanifestを別々に保存し、同じseed・件数・期間で比較します。GUI runはIDが異なるため、両方を同じnamespaceに追加した後のGold合計をそのまま差分と解釈しません。厳密な比較には別の検証環境/namespace、または対象の業務キー範囲を使います。

ファイル破損ケースでpipelineが止まったら、既に処理済みの表やファイルを先に確認します。修正対象は診断に出たファイルとreadyだけです。全landingやvolumeを削除して初期化しません。通常の期待値は [MEDALLION.md](MEDALLION.md)、GUI/APIの認証・非同期・エラーは [API.md](API.md)、採用版の公式資料は [REFERENCES.md](REFERENCES.md) を参照します。

## 原因を切り分けるための3つの比較

異常とは「生成に失敗したこと」ではなく、下流の規則を試すための入力です。初めて試す人はcleanを基準にして、次のいずれか1つを選びます。GUIの投入先は検証用landing、実行は素材→明示変換→対象の照合の順です。

| 比較 | 素材で変わるもの | 下流で見るもの | そこから言えること |
| --- | --- | --- | --- |
| clean と `invalid_number` | 良品を数値へ変換できない文字にする | `quarantine.mes_events` の理由/元文字列 | 型規則が機能する。工場の不良品率とは別 |
| clean と `invalid_latest` | 同じキーの後着最新版だけを不正にする | 当該キーがSilverから外れること | 過去の正常版へ戻さない採用方針を確認 |
| clean と `reject_spike` | 型として有効な不良数を増やす | Goldの不良数と歩留まり | 業務値の変化を反映する。自動警報の実装ではない |

`invalid_latest` は行の追加、`duplicate_row` は複製、`missing_shipment` は削除を含みます。`affected` を最終行数の増減や隔離件数としてそのまま使いません。親の受注を不正にすると、有効な型の出荷でも親参照がなくなり子側で隔離されることがあります。

### 破損ファイルの試験は行の品質試験と分ける

`wrong_checksum` は内容とreadyの不一致なので、CSVを解析する前の受入境界で拒否します。`extra_column` はhashが正しくても契約列が違うため解析後に拒否します。`missing_ready` は対象ファイルを未完成として無視します。3つを同じ「取込エラー数」へまとめず、どの境界まで進んだかを記録してください。

前の試験のGoldが残っている場合、新しい破損素材を生成してもGoldが変わらないことがあります。それは正常なGoldを新素材で更新できた証明ではありません。入力run/ファイル、job、Bronze/Silver/Goldの版を対応付けます。

## 異常を追加するときに書く仕様

追加実装の正本は [source_scenarios.py](../scripts/source_scenarios.py) です。たとえば「設備停止が24時間を超える」ケースを増やすなら、最初に次を決めます。これは実装済みIDではなく設計例です。

| 仕様 | 決める内容 |
| --- | --- |
| データ上の変化 | start/endのどちらをどう変えるか、対象の選び方 |
| 既存SQLとの関係 | 有効停止として集計するか、新しい隔離規則を追加するか |
| 率と件数 | 選択行数、追加/削除/更新の操作数、最終行数の違い |
| 対応する投入先 | landing/sqlserver/both、画像やfile同意の要否 |
| 競合 | 時刻逆転等の別ケースと同時指定してよいか |
| 期待する観測 | 元文字列、理由、対象キー、Goldのどの指標を見るか |

実装後は、catalog、入力拒否、同seedでの再現、manifest診断、GUIの選択と同意、API/CLI、実際の後続SQLを別々に確認します。生成器と同じ式を期待値へ丸写しせず、小さい入力で独立計算を用意します。[変更手順](CUSTOMIZATION.md)・[評価計画](reports/EVALUATION-PLAN.md)
