# 構成図とロゴの出典

<!-- doc-purpose:start -->
> **役割:** 全体図・AI候補図・公式ロゴの出典と、詳細図素材の配置を確認する。
>
> **対象読者:** 開発・設計担当 / 評価・報告担当
>
> **読むタイミング:** 図やロゴを引用するとき、素材の出典と編集先を調べるとき。
>
> **前提となる資料:** [Lakehouse Foundation アーキテクチャ](<../ARCHITECTURE.md>)
>
> **次に読む資料:** [PoC報告書・発表に使う図版と雛形](<../reports/README.md>) / [図と出力スクリプトを変更する人へ](<../reports/EDITING-GUIDE.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<../DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

- [全体構成図](architecture.svg)
- [AI・Cloudflareの選択肢](ai-cloudflare.svg)

図はSVGなので、拡大・印刷・オフライン閲覧に使えます。図の状態表示だけで実装・本番運用・外部サービス接続が検証済みとは扱わず、[検証記録](../VALIDATION.md) を併せて確認します。

図を再生成するときは、リポジトリのルートで `python3 scripts/render_architecture.py` を実行します。Python標準ライブラリだけでSVGを生成し、ロゴを内部へ埋め込みます。図の閲覧時に外部の画像サーバーへ接続する必要はありません。

## 公式配布元

| ロゴ | 取得した公式配布元 |
| --- | --- |
| Apache Spark | [spark.apache.org](https://spark.apache.org/images/spark-logo-rev.svg) |
| Apache Iceberg | [iceberg.apache.org](https://iceberg.apache.org/assets/images/Iceberg-logo.svg) |
| Lakekeeper | [lakekeeper.io](https://lakekeeper.io/assets/bear.svg) |
| Trino | [trino.io](https://trino.io/assets/images/trino-logo/trino-ko_tiny-alt.svg) |
| Prometheus | [prometheus.io](https://prometheus.io/_next/static/media/prometheus-logo.7aa022e5.svg) |
| Grafana | [公式GitHub](https://github.com/grafana/grafana/blob/main/public/img/grafana_icon.svg) |
| Cloudflare | [cloudflare.com](https://www.cloudflare.com/img/logo-cloudflare-dark.svg) |

取得日、URL、取得したバイト列のSHA-256は [logos/sources.json](logos/sources.json) にあります。個別ファイルは公式配布物を保存し、図の埋め込み時には縦横比を維持します。viewBoxがない配布物には同じ寸法のviewBoxを与えて縮尺を合わせています。

ロゴ・商標の権利は各権利者に属します。製品を識別する説明図として使用しており、各プロジェクトによる認定・提携を示すものではありません。図の箱や矢印、説明文はこのリポジトリの構成を説明するものです。

## 今回の文書監査

2026-11-10に、同梱SVGとロゴ7種の記録、再生成スクリプトの参照、表示上の実装/任意拡張の区分を確認しました。heroはREADMEの入口、architectureは実装構成、ai-cloudflareは選択案を示します。図を再生成した場合は、`git diff` でSVGの文字列・外部リンクと本文の一致も確認します。取得URLの将来の移動と、同梱ファイルのhash/出典記録は分けて扱います。[採用版と文書監査](../REFERENCES.md)

## 報告書向けの詳細図

[reportsの図版集](../reports/README.md)に14種の詳細・用途別SVGとMermaid、自己完結HTML、キャプション・根拠台帳を追加しています。このページの公式ロゴを変更せず埋め込み、同じ出典・商標の扱いを引き継ぎます。PNG/PDF/PPTXは専用uv環境からGit外へ生成できます。

## 閲覧・利用・保守の使い分け

| 使う人 | 行うこと | 確認する資料 |
| --- | --- | --- |
| 初めて全体を見る人 | READMEのheroからarchitectureを読み、coreと任意拡張を区別 | [構成の説明](../ARCHITECTURE.md) |
| 報告書を書く人 | 主題に合う用途別図を選び、captionと制約を一緒に転載 | [報告素材](../reports/README.md) |
| 図を直す人 | 生成元の箱・矢印・説明を変更し、出典と内容を照合 | [編集ガイド](../reports/EDITING-GUIDE.md) |
| 版を更新する人 | 上流の配布物と保存hashを確認し、ロゴの追加/変更をレビュー | [公式資料](../REFERENCES.md) |

ロゴを新たに取得する場合は公式の配布元を選び、取得日・URL・SHA-256を台帳へ追加します。SVGの外部参照・script・フォント依存を確認し、ネットワークなしで表示できる公開素材にします。既存exportの許可ロゴも更新が必要なので、ファイルを置くだけでは追加できません。

配色や共通描画を変更したら、簡易図と14詳細図の両方を再生成して確認します。SVGのテキスト比較だけで、矢印と文字の重なり、日本語の欠け、PPTXでの読める大きさまで合格とはしません。報告先の表示サイズと出力形式でも確認します。
