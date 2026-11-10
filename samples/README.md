# 合成入力を最初に作る

確認日: 2026-11-10（JST）。実データのsampleは同梱しません。[初回ガイド](../docs/START-HERE.md) で配置した検証用環境のGUIから、6つのMES/ERP素材を作れます。会社データ用とは別の環境を使います。

CLIではリポジトリのルートで、既存ファイルのない外部ディレクトリを選びます。Python標準ライブラリだけでファイル生成できます。

```bash
python3 scripts/generate-demo.py --destination landing \
  --output /srv/lakehouse/landing \
  --seed 42 --orders 20 --days 7 --scenario edge
```

出力先は検証用private設定の `LANDING_DIR` と一致させ、実行ユーザーが書き込めることを先に確認します。同名素材がある場合は上書きせず停止します。6素材とready/manifestが成功条件です。生成は取込や変換を起動しません。

後続の明示操作・SQL投入・画像・有限streamは [素材仕様](../docs/DEMO-DATA.md)・[GUI](../docs/DEMO-GUI.md)、型/隔離/集計は [メダリオン](../docs/MEDALLION.md) を使います。各バージョンの根拠は [公式資料一覧](../docs/REFERENCES.md) にあります。
