# 外部入力の契約fixture

<!-- doc-purpose:start -->
> **役割:** 固定のCSV・JSON Lines・画像と完成印の役割を説明する。
>
> **対象読者:** 初めて使う人 / データ利用担当
>
> **読むタイミング:** 受入検証に使う固定データの構成を確認するとき。
>
> **前提となる資料:** [初めて構築する人の一本道ガイド](<../docs/START-HERE.md>)
>
> **次に読む資料:** [外部ファイルのLanding入力契約](<../docs/DEMO-DATA.md>) / [GUIで外部データの処理を確認する](<../docs/DEMO-GUI.md>) / [MES・ERPデータをBronzeからGoldへ整える](<../docs/MEDALLION.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../docs/DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

[landing-contract](landing-contract/) はCSV、JSON Lines、PNGを格納したASCIIのBase64、それぞれのSHA-256完了印を含む静的fixtureです。会社データを含みません。生成コマンドでも自動投入用シードでもありません。

workerの `verify` はここを読取り専用で参照し、画像はメモリ内で元のPNGバイト列に戻して、専用namespaceと専用Raw領域で処理します。通常Landingには置かず、既存表や既存ファイルも置換しません。

[ファイル契約](../docs/DEMO-DATA.md) · [取り込み・再処理](../docs/MEDALLION.md)
