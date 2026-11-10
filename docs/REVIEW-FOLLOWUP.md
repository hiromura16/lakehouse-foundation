# 外部レビュー指摘の対応と確認

<!-- doc-purpose:start -->
> **役割:** PR #3の外部レビュー指摘と、再現・修正・回帰確認の対応を追跡する。
>
> **対象読者:** 開発・設計担当 / 評価・報告担当
>
> **読むタイミング:** 既存の指摘がどう解決されたか確認し、同種の回帰を避けるとき。
>
> **前提となる資料:** [レビュー指摘を改善へ取り込む](<REVIEW-WORKFLOW.md>)
>
> **次に読む資料:** [検証記録](<VALIDATION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

対象は [PR #3](https://github.com/hiromura16/lakehouse-foundation/pull/3) の並行レビューです。指摘を実装と再現で確認し、会社の値や秘密を使わない回帰試験で対応します。新着確認の進め方は [REVIEW-WORKFLOW](REVIEW-WORKFLOW.md) を参照します。

## issueごとの修正

| 指摘 | 原因と修正 | 保持した契約 | 回帰・受入 |
| --- | --- | --- | --- |
| [#4 upgrade backup衝突](https://github.com/hiromura16/lakehouse-foundation/issues/4) | 既存.before-upgradeへO_EXCLで作成し、FileExistsErrorが未処理だった。衝突だけを明確な退避・再実行案内へ変更 | backupを上書きしない。旧envも変更しない。変更不要のupgradeは既存backupがあっても成功 | byte/0600保持、余分なfileなし、traceback/秘密/実pathなし、no-op成功 |
| [#5 追加依存なしのテスト](https://github.com/hiromura16/lakehouse-foundation/issues/5) | optional依存のrootが本当に存在しない場合だけSkipTest＋lock導入案内 | 導入済み依存の不具合、別依存欠落、アプリimport不具合をskipしない | 開発lock環境で全267テスト、真の欠落5＋壊れた依存/別欠落/アプリ不具合15の隔離20ケース |
| [#6 env不在の案内](https://github.com/hiromura16/lakehouse-foundation/issues/6) | Git内とファイル不在を同じ条件で拒否していた。Git内拒否を維持し、外部の不在は別の非秘密メッセージへ | private設定はGit外。エラーへ実path/内容を表示しない | 外部不在とGit内の既存/不在を区別。CLI表示にも意図したmessage |

#4/#6の4回帰を既存の [互換性テスト](../tests/test_company_compatibility.py) へ追加しました。#5はAPI/ポータル/UI/MCPの5テストmoduleのimport境界だけを変更し、本体の追加依存を必須化していません。

## 利用者が取る操作

### upgradeがbackup衝突で止まった場合

エラーは保存処理に進む前の拒否です。既存のenvとbackupを社内の管理先で確認し、前回backupを別の名前または保管場所へ退避してから再実行します。自動で古いbackupを削除したり、鍵を再生成したりしません。変更がない場合は、そのまま再実行しても新しいbackupを作りません。[更新手順](COMPANY-UPDATE.md)

### システムPythonでskipが出た場合

skipはその機能を検証した結果ではありません。すべての開発テストを行うには [CONTRIBUTING](../CONTRIBUTING.md) のuv環境へrequirements-dev.lockを同期します。依存を導入してもimportに失敗する場合は、環境破損やアプリの不具合として調査します。ImportErrorを広く無視して成功にしません。

### envファイルが見つからない場合

選択した環境の `LAKEHOUSE_ENV_FILE` / `--env-file`、ファイルの生成有無、パスのtypoを確認します。既存環境では新たな秘密値生成で上書きせず、正しい私有設定の保管先を確認します。新規構築だけが [初回設定](START-HERE.md) のconfigureを使います。

## 上級者向けの変更境界

コメント・help改善は説明部分を除くAST一致で確認した段階（`d71221d183eacbb6566ae10cfbb43ac682b4da71`）と、上の例外処理/import境界の動作修正を分けています。267件は4回帰追加後の時点の結果で、将来のCI件数は対象commitのrunを確認します。会社ネットワーク・負荷・復元・モデル推論の成功を追加したものではありません。

今後の指摘も、対象commit、再現条件、根拠、変更範囲、検証、未解決点を同じ単位で記録します。sourceやissueへ実接続先・秘密値・生ログを転記しません。

## 次のレビュー: レポートの可搬性と編集時の拒否

[issue #7](https://github.com/hiromura16/lakehouse-foundation/issues/7) の文字コード依存をUTF-8明示で修正しました。[issue #8](https://github.com/hiromura16/lakehouse-foundation/issues/8) は必須キー/型/日付の早期検査、コードフェンス内リンクの保持、ロゴcacheと検証済みSVG幅の受渡し、共通描画部品の整理で対応しました。対象はレポート生成経路で、稼働コンテナへの変更はありません。

18件のレポート回帰で以下を確認しました。ASCIIが既定の実子プロセスでも日本語を含む生成/検査が成功し、不正定義はexport先を作る前に簡潔なエラーで拒否します。詳細14図と既存2図の生成byte列も維持しています。最新の全体検査は [VALIDATION](VALIDATION.md) を参照し、既存267件という過去の件数と区別します。

| 境界 | 確認すること |
| --- | --- |
| 文字コード | 生成・照合・外部exportの日本語テキストをUTF-8で扱う |
| 編集ミス | 欠けたキーと不正な値を編集箇所の分かるエラーにし、export前に拒否 |
| リンク変換 | 通常本文の相対リンクを可搬化し、コードブロックの記入例は保持 |
| 効率整理 | 同じSVG/ロゴの不要な再読込を避け、公開図の内容を維持 |
| 機密 | Git外の出力先と公開素材のみの梱包という既存の境界を維持 |

利用者はUTF-8でJSON/MDを保存し、図を変えたら通常生成と `--check` を実行します。エラーを消す目的で必須項目を空文字へ置き換えず、[図編集ガイド](reports/EDITING-GUIDE.md) に沿って値を修正します。外部exportは新しいディレクトリへ行い、失敗先を既存の報告書の上書き先として使いません。
