# 構成図とロゴの出典

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
