# 合成入力を最初に作る

<!-- doc-purpose:start -->
> **役割:** 実データを使わずに最初の6契約素材を生成し、出力と後続処理の境界を確認する。
>
> **対象読者:** 初めて使う人 / データ利用担当
>
> **読むタイミング:** 空の検証用landingに小さい固定条件の合成入力を初めて用意するとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<../docs/START-HERE.md>)
>
> **次に読む資料:** [MES・ERPのダミーデータ投入](<../docs/DEMO-DATA.md>) / [GUIからダミーデータを投入する](<../docs/DEMO-GUI.md>) / [MES・ERPデータをBronzeからGoldへ整える](<../docs/MEDALLION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../docs/DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

<!-- shell-usage:start -->
**コマンドのシェル:** 「Bash / Zsh共通」は同じ内容を使い、専用例は自分のシェルを選びます。[詳しい使い分け](../docs/SHELLS.md)。

Zshの対話端末では、コメント付きの例を貼る前に一度だけ次を実行します。Bashでは不要です。

**Zsh**

```zsh
setopt INTERACTIVE_COMMENTS
```
<!-- shell-usage:end -->

確認日: 2026-11-10（JST）。実データのsampleは同梱しません。[初回ガイド](../docs/START-HERE.md) で配置した検証用環境のGUIから、6つのMES/ERP素材を作れます。会社データ用とは別の環境を使います。

CLIではリポジトリのルートで、既存ファイルのない外部ディレクトリを選びます。Python標準ライブラリだけでファイル生成できます。

**Bash / Zsh共通**

```bash
python3 scripts/generate-demo.py --destination landing \
  --output /srv/lakehouse/landing \
  --seed 42 --orders 20 --days 7 --scenario edge
```

出力先は検証用private設定の `LANDING_DIR` と一致させ、実行ユーザーが書き込めることを先に確認します。同名素材がある場合は上書きせず停止します。6素材とready/manifestが成功条件です。生成は取込や変換を起動しません。

後続の明示操作・SQL投入・画像・有限streamは [素材仕様](../docs/DEMO-DATA.md)・[GUI](../docs/DEMO-GUI.md)、型/隔離/集計は [メダリオン](../docs/MEDALLION.md) を使います。各バージョンの根拠は [公式資料一覧](../docs/REFERENCES.md) にあります。

## 最初に試す順番

| 段階 | 選ぶ素材 | 観察すること |
| --- | --- | --- |
| 初回 | cleanのbatchをGUIから1回 | 6素材のmanifestができ、変換前のGoldは変わらない |
| 品質 | 型不正や必須欠損を一つずつ | 変換後の隔離理由とSilver対象行 |
| 業務 | 不良増、停止増、出荷遅延などを個別に | Goldの数量・比率と母数の変化 |
| 改訂 | 遅れて届く更新、重複、最新版不正 | 最新候補の選択と再計上の防止 |
| 連続到着 | 上限を決めた有限stream | batchごとのmanifest、明示した後続処理の範囲 |
| CDC | 専用SQL検証DBで固定I/U/D | rawイベントと現在値、削除後のGoldの対応 |

clean/edgeの固定fixtureと、GUIで追加するrunは別の比較条件です。通常runは蓄積するので、以前の素材がある状態ではseedだけを揃えて同じ総件数を期待しません。基盤の固定期待値は隔離した `verify` を使います。

## 入力契約を変える場合

自社MES/ERPの表名をこのディレクトリへcopyして公開しません。専用の入力mapで6ソースの列・単位・時刻・業務キーへ対応させ、[拡張手順](../docs/CUSTOMIZATION.md) に沿って独立照合を追加します。新しい種類のファイルを置くだけで、Bronze/Silver/Goldの処理が自動生成される構成ではありません。

素材の生成は下流の起動を兼ねません。素材GUI/APIの成功と、取込・変換・品質検査の成功を別々に記録し、どの段階で期待から外れたかを追えるようにします。
