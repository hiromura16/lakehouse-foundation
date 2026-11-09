#!/usr/bin/env python3
"""Render standalone SVG architecture figures from attributed upstream logos.

Only the Python standard library is required. SVG logos are embedded as data
images so GitHub and offline document viewers need no external image requests.
"""

import base64
import re
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs/assets"
FONT = "Noto Sans CJK JP, Hiragino Sans, Yu Gothic, sans-serif"
INK = "#14263d"
BLUE = "#216bc4"


def text(x, y, value, size=18, fill=INK, weight=400):
    """Escape diagram labels before writing them as SVG text nodes."""
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-family="{FONT}" '
        f'font-size="{size}" font-weight="{weight}">{escape(value)}</text>'
    )


def rect(x, y, width, height, fill="#fff", stroke="#d7e0ec", radius=16):
    """Draw a consistent rounded container."""
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'rx="{radius}" fill="{fill}" stroke="{stroke}"/>'
    )


def logo(name, x, y, width=52, height=52):
    """Embed the unchanged upstream mark and contain its aspect ratio."""
    content = (ASSETS / "logos" / (name + ".svg")).read_text()
    # Dimension-only upstream marks need an equivalent scaling viewport.
    opening = re.search(r"<svg\b[^>]*>", content).group()
    if "viewBox=" not in opening:
        width_value = re.search(r'\bwidth="([0-9.]+)', opening).group(1)
        height_value = re.search(r'\bheight="([0-9.]+)', opening).group(1)
        replacement = opening[:-1] + (
            f' viewBox="0 0 {width_value} {height_value}">'
        )
        content = content.replace(opening, replacement, 1)
    encoded = base64.b64encode(content.encode()).decode()
    return (
        f'<image x="{x}" y="{y}" width="{width}" height="{height}" '
        'preserveAspectRatio="xMidYMid meet" '
        f'href="data:image/svg+xml;base64,{encoded}"/>'
    )


def arrow(points, dashed=False, color=BLUE):
    """Draw directed routes, using dashes for optional integrations."""
    dash = ' stroke-dasharray="7 6"' if dashed else ""
    return (
        f'<path d="{points}" fill="none" stroke="{color}" '
        f'stroke-width="3"{dash} marker-end="url(#arrow)"/>'
    )


def canvas(width, height, title, subtitle):
    """Start an accessible figure with common styling and metadata."""
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="figure-title figure-description">',
        f'<title id="figure-title">{escape(title)}</title>',
        f'<desc id="figure-description">{escape(subtitle)}</desc>',
        '<defs><marker id="arrow" markerWidth="9" markerHeight="9" '
        'refX="7" refY="3" orient="auto" markerUnits="strokeWidth">'
        '<path d="M0,0 L0,6 L8,3 z" fill="#216bc4"/></marker></defs>',
        rect(0, 0, width, height, "#f5f8fc", "none", 0),
        text(36, 62, title, 32, weight=700),
        text(36, 100, subtitle, 17, "#54677e"),
    ]


def platform():
    """Show source, ingestion, shared tables, engines and operations."""
    parts = canvas(
        1680,
        1030,
        "Lakehouse Foundation | 全体構成",
        "SQL Server・ファイル・イベントを共通のIcebergへ。"
        "GUI/APIと業務SQLを分離。",
    )
    lanes = [
        (36, 300, "01  データソース"),
        (366, 300, "02  取り込み・実行"),
        (696, 430, "03  共通データと正本"),
        (1156, 488, "04  分析・AI・操作"),
    ]
    for x, width, title in lanes:
        parts += [
            rect(x, 150, width, 610, "#edf3fa", "#dce5f0"),
            text(x + 18, 187, title, 20, weight=700),
        ]
    parts += [
        rect(54, 220, 264, 130),
        text(72, 256, "既存 SQL Server", 23, weight=700),
        text(72, 289, "工場MES / ERP", 18),
        text(72, 322, "SELECT バッチ / CDC", 16, "#54677e"),
        rect(54, 390, 264, 130),
        text(72, 426, "Streamlit + FastAPI", 21, weight=700),
        text(72, 459, "疑似バッチ / 有限ストリーム", 16),
        text(72, 492, "素材投入だけを実行", 16, "#54677e"),
        rect(54, 570, 264, 130),
        text(72, 607, "Samba landing", 23, weight=700),
        text(72, 641, "CSV / JSON / 画像", 18),
        text(72, 675, "原本 + SHA-256 ready", 16, "#54677e"),
        rect(384, 220, 264, 130),
        text(402, 255, "Debezium + Kafka", 22, weight=700),
        text(402, 289, "INSERT / UPDATE / DELETE", 15),
        text(402, 322, "raw CDC / 再開checkpoint", 15, "#54677e"),
        rect(384, 390, 264, 130),
        text(402, 427, "取り込み worker", 23, weight=700),
        text(402, 461, "完了マーカー / 冪等性", 17),
        text(402, 494, "版管理されたメダリオンSQL", 15, "#54677e"),
        rect(384, 570, 264, 130),
        text(402, 607, "n8n / Airflow", 23, weight=700),
        text(402, 641, "手動実行 / 自動化の入口", 17),
        text(402, 675, "workerへ明示的に依頼", 16, "#54677e"),
        rect(714, 220, 394, 130),
        logo("lakekeeper", 731, 243, 68, 76),
        text(812, 257, "Lakekeeper", 25, weight=700),
        text(812, 291, "Iceberg REST catalog", 17),
        text(812, 322, "PostgreSQL / 暗号鍵", 16, "#54677e"),
        rect(714, 390, 394, 310),
        rect(729, 406, 108, 62, "#14263d", "none", 8),
        logo("iceberg", 735, 411, 96, 52),
        text(847, 442, "Iceberg v2 / Parquet", 22, weight=700),
    ]
    layers = [
        (732, 486, "Bronze", "原本・変更履歴", "#dfecfb"),
        (926, 486, "Silver", "型・現在値・品質", "#e5eaf1"),
        (732, 573, "Gold", "生産・出荷の指標", "#fff1cc"),
        (926, 573, "Quarantine", "不正行と理由", "#ffe4dc"),
    ]
    for x, y, title, detail, fill in layers:
        parts += [
            rect(x, y, 164, 70, fill, "none", 10),
            text(x + 12, y + 27, title, 20, weight=700),
            text(x + 12, y + 53, detail, 14),
        ]
    parts += [
        text(734, 680, "SeaweedFS S3: 原本・データ・metadata", 17),
        rect(1174, 220, 452, 130),
        logo("trino", 1193, 242, 53, 75),
        text(1262, 260, "Trino", 25, weight=700),
        text(1262, 294, "SQL参照・変換", 16),
        rect(1425, 237, 177, 56, "#14263d", "none", 9),
        logo("spark", 1436, 242, 150, 45),
        text(1427, 322, "Spark 4.1 / Iceberg 1.12", 14),
        rect(1174, 390, 452, 130),
        text(1193, 426, "LangChain / ローカルLLM", 25, weight=700),
        text(1193, 461, "Gold事実 + 固定snapshot + 引用", 17),
        text(1193, 494, "モデル実行は別段階・明示的に選択", 16, "#54677e"),
        rect(1174, 570, 452, 130),
        text(1193, 607, "GUI / API / Swagger UI", 25, weight=700),
        text(1193, 641, "設定・投入・実行状態・AI context", 17),
        text(1193, 675, "秘密をマスク / 保存と反映を区別", 16, "#54677e"),
        arrow("M318 270 H374", True),
        arrow("M186 520 V560"),
        arrow("M318 630 H349 V454 H374"),
        arrow("M648 454 H704"),
        arrow("M648 270 H680 V420 H704", True),
        arrow("M516 570 V530", True),
        arrow("M1108 270 H1164"),
        arrow("M1108 420 H1140 V318 H1164"),
        arrow("M1108 454 H1164", True),
        rect(36, 800, 1608, 140, "#14263d", "none"),
        logo("prometheus", 60, 829, 70, 70),
        logo("grafana", 153, 829, 70, 70),
        text(250, 843, "Prometheus + Grafana", 24, "#fff", 700),
        text(
            250,
            881,
            "状態・件数・失敗を監視 / ダッシュボードはGUI保存",
            17,
            "#d9e5f5",
        ),
        text(1030, 843, "Dokploy / private設定", 24, "#fff", 700),
        text(1030, 881, "Git配置・永続volume・proxy / CA", 17, "#d9e5f5"),
        text(
            38,
            982,
            "実線: データ経路   破線: 任意profile / 拡張連携",
            16,
            "#54677e",
        ),
        text(
            1030,
            982,
            "実装・実機検証の範囲は docs/VALIDATION.md",
            15,
            "#54677e",
        ),
        "</svg>",
    ]
    return "\n".join(parts)


def hybrid():
    """Distinguish local facts from optional Cloudflare service choices."""
    parts = canvas(
        1680,
        880,
        "Lakehouse Foundation | AI・Cloudflare連携の選択肢",
        "社内の正本と認証境界を維持し、必要なWeb入口・保存・推論だけを選ぶ。",
    )
    parts += [
        rect(36, 150, 742, 530, "#edf3fa"),
        text(60, 190, "社内基盤", 25, weight=700),
        rect(60, 225, 694, 110),
        rect(75, 240, 109, 69, "#14263d", "none", 8),
        logo("iceberg", 82, 248, 95, 53),
        text(200, 267, "Iceberg Gold / 原本 / snapshot", 25, weight=700),
        text(200, 306, "Trinoで事実を固定し、元データへの参照を保持", 17),
        rect(60, 375, 325, 130),
        text(80, 414, "Streamlit / FastAPI", 23, weight=700),
        text(80, 454, "操作・Swagger・状態", 18),
        text(80, 484, "Bearer / private設定", 16, "#54677e"),
        rect(425, 375, 329, 130),
        text(444, 414, "LangChain / Ollama", 23, weight=700),
        text(444, 454, "根拠付きDocument", 18),
        text(444, 484, "ローカル推論の接続例", 16, "#54677e"),
        text(
            65, 569, "標準配置のデータと資格情報は社内で管理", 21, weight=700
        ),
        text(
            65, 611, "外部への送信・保存・閲覧は採用する機能ごとに決める", 17
        ),
        rect(888, 150, 756, 530, "#fff4e9", "#f3d8b9"),
        rect(912, 170, 244, 67, "#ffffff", "#f3d8b9", 9),
        logo("cloudflare", 928, 177, 210, 54),
        text(1180, 211, "任意の連携先", 23, weight=700),
        rect(912, 266, 708, 94),
        text(932, 302, "R2 + Basin Catalog / Basin SQL", 24, weight=700),
        text(932, 336, "許可したデータを保存 / Iceberg catalog / SQL", 17),
        rect(912, 390, 336, 110),
        text(931, 428, "Tunnel + Access", 24, weight=700),
        text(931, 464, "Web入口 / IdPアクセス制御", 16),
        rect(1284, 390, 336, 110),
        text(1303, 428, "Workers AI / Gateway", 23, weight=700),
        text(1303, 464, "選択モデル / 経路・計測", 17),
        rect(912, 534, 708, 94),
        text(932, 571, "Vectorize / AI Search", 24, weight=700),
        text(
            932, 606, "文書検索・索引 / 原本への引用・削除同期・権限filter", 17
        ),
        arrow("M385 440 H405"),
        arrow("M222 335 V365"),
        arrow("M590 335 V365"),
        arrow("M778 304 H902", True),
        arrow("M778 445 H902", True),
        text(796, 280, "任意", 16, BLUE),
        rect(36, 724, 1608, 104, "#14263d", "none"),
        text(60, 763, "設計の境界", 23, "#fff", 700),
        text(
            250,
            763,
            "Cloudflare接続・モデル推論は自動有効化しない。会社の要件に沿って個別に検証。",
            18,
            "#fff",
        ),
        text(
            250,
            798,
            "現行名称: Basin Catalog / Basin SQL"
            "（旧R2 Data Catalog / R2 SQL）。"
            "公式資料と用途はCLOUDFLARE.md。",
            16,
            "#d9e5f5",
        ),
        "</svg>",
    ]
    return "\n".join(parts)


def main():
    """Update the version-controlled, self-contained diagram artifacts."""
    for filename, renderer in [
        ("architecture.svg", platform),
        ("ai-cloudflare.svg", hybrid),
    ]:
        (ASSETS / filename).write_text(renderer() + "\n")
        print("Rendered docs/assets/" + filename)


if __name__ == "__main__":
    main()
