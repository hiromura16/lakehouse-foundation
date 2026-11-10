#!/usr/bin/env python3
"""公開図仕様からPoC報告書の図版・説明・索引を生成する。

処理順序:
    1. figures.jsonの図ID・参照ファイル・パネル領域を検査する。
    2. 同じ仕様からSVG、Mermaid、キャプション、CSV、HTMLを組み立てる。
    3. 通常は生成物を保存し、--checkでは差分の有無だけを確認する。
    4. --export-dir指定時だけ、専用依存でPNG/PDF/PPTX/ZIPを出力する。

標準の図生成はPython標準ライブラリだけで動作する。設定ファイル、
DB、稼働サービス、ブラウザー画面を読み取る処理は含めない。
図の意味とレイアウトの編集方法はdocs/reports/EDITING-GUIDE.mdを参照。
"""

import argparse
import csv
import io
import json
import math
import re
import unicodedata
from datetime import date
from html import escape
from pathlib import Path

from documentation import purpose_block
from svg_primitives import (
    ATTRIBUTED_LOGOS,
    COLORS,
    svg_rectangle,
    svg_text,
)

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "docs/reports"
SPEC = REPORTS / "figures.json"
FONT = "Noto Sans CJK JP, Noto Sans JP, Yu Gothic, sans-serif"
INK = COLORS["report_ink"]
MUTED = COLORS["report_muted"]
TONES = {
    "blue": ("#edf4ff", "#b8cee8", "#245fa6"),
    "teal": ("#eaf7f3", "#a4d1c1", "#126c59"),
    "purple": ("#f1edfc", "#c6b9e9", "#63449b"),
    "bronze": ("#faf0e7", "#d9baa1", "#85512a"),
    "silver": ("#eef2f6", "#bbc8d4", "#455b71"),
    "gold": ("#fff7dd", "#dfca85", "#806000"),
    "red": ("#fff0ee", "#e7b6af", "#9c3c32"),
    "gray": ("#f5f6f8", "#c8cdd5", "#586271"),
}
EDGES = {
    "data": ("#2469a8", ""),
    "control": ("#70608e", "8 5"),
    "optional": ("#78818c", "3 7"),
}


def width_units(value: str) -> float:
    """Estimate CJK text width without font or rendering dependencies."""
    return sum(
        1 if unicodedata.east_asian_width(char) in "WFA" else 0.56
        for char in value
    )


def wrap(value: str, max_units: float) -> list[str]:
    """Wrap labels while keeping short technical words on the same line."""
    lines, current = [], ""
    tokens = re.findall(r"[A-Za-z0-9_./:$+{}-]+|.", value)
    for token in tokens:
        if current and width_units(current + token) > max_units:
            lines.append(current.rstrip())
            current = ""
        for char in token:
            if current and width_units(current + char) > max_units:
                lines.append(current.rstrip())
                current = ""
            current += char
    if current:
        lines.append(current.rstrip())
    return lines or [""]


def text(x, y, value, size=24, color=INK, weight=400):
    """Escape labels so XML markup cannot be inserted as diagram content.

    Text stays text rather than paths, preserving SVG editing and PDF search.
    """
    return svg_text(
        x, y, str(value), size, color, weight, coordinate_format="g"
    )


def rectangle(x, y, width, height, fill, stroke="none", radius=16):
    """Render a simple print-friendly panel without shadows."""
    return svg_rectangle(
        x, y, width, height, fill, stroke, radius, coordinate_format="g"
    )


def spec_error(location, instruction):
    """Identify an editable JSON location instead of exposing a traceback."""
    return ValueError(f"{SPEC.name}: {location}: {instruction}")


def require_fields(value, location, fields):
    """Check an object's required fields before accessing any of them."""
    if not isinstance(value, dict):
        raise spec_error(location, "Use a JSON object.")
    for field, expected in fields.items():
        child = f"{location}.{field}"
        if field not in value:
            raise spec_error(child, "Add this required field.")
        if expected == "number":
            require_number(value[field], child)
        elif type(value[field]) is not expected:
            names = {str: "string", list: "array", int: "integer"}
            raise spec_error(child, f"Use a JSON {names[expected]}.")


def require_number(value, location, *, positive=False):
    """Refuse booleans, nonfinite coordinates and nonpositive dimensions."""
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (positive and value <= 0):
        expected = "positive finite number" if positive else "finite number"
        raise spec_error(location, "Use a " + expected + ".")


def require_strings(values, location):
    """Check each list item before wrapping text or resolving source paths."""
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise spec_error(f"{location}[{index}]", "Use a JSON string.")


def validate_panel(panel, location, figure):
    """Validate one card's content, logo and drawable geometry."""
    require_fields(
        panel,
        location,
        {
            "key": str,
            "x": "number",
            "y": "number",
            "width": "number",
            "height": "number",
            "tone": str,
            "title": str,
            "lines": list,
            "tag": str,
            "logo": str,
        },
    )
    if not panel["key"]:
        raise spec_error(location + ".key", "Use a nonempty panel key.")
    require_strings(panel["lines"], location + ".lines")
    for dimension in ("width", "height"):
        require_number(
            panel[dimension], location + "." + dimension, positive=True
        )
    x, y = panel["x"], panel["y"]
    if (
        x < 32
        or y < 185
        or x + panel["width"] > figure["width"] - 32
        or y + panel["height"] > figure["height"] - 175
    ):
        raise spec_error(location, "Keep this panel inside the content area.")
    if panel["tone"] not in TONES:
        raise spec_error(location + ".tone", "Choose: " + ", ".join(TONES))
    if panel["logo"] and panel["logo"] not in ATTRIBUTED_LOGOS:
        raise spec_error(location + ".logo", "Use an attributed logo or ''.")


def validate_edge(edge, location, keys):
    """Check endpoints and optional waypoints before rendering a route."""
    require_fields(
        edge,
        location,
        {"source": str, "target": str, "label": str, "kind": str},
    )
    for endpoint in ("source", "target"):
        if edge[endpoint] not in keys:
            raise spec_error(
                location + "." + endpoint, "Use an existing panel key."
            )
    if edge["kind"] not in EDGES:
        raise spec_error(location + ".kind", "Choose: " + ", ".join(EDGES))
    if "points" not in edge:
        return
    points = edge["points"]
    if not isinstance(points, list) or len(points) < 2:
        raise spec_error(
            location + ".points", "Use at least two [x, y] pairs."
        )
    for index, point in enumerate(points):
        point_location = f"{location}.points[{index}]"
        if not isinstance(point, list) or len(point) != 2:
            raise spec_error(point_location, "Use one [x, y] coordinate pair.")
        for coordinate, number in enumerate(point):
            require_number(number, f"{point_location}[{coordinate}]")


def validate_figure(figure, location):
    """Validate a figure independently; return its ID for duplicate checks."""
    require_fields(
        figure,
        location,
        {
            "id": str,
            "title": str,
            "subtitle": str,
            "caption": str,
            "audience": str,
            "width": "number",
            "height": "number",
            "panels": list,
            "edges": list,
            "notes": list,
            "sources": list,
            "mermaid": str,
        },
    )
    slug = figure["id"]
    if not re.fullmatch(r"[0-9]{2}-[a-z-]+", slug):
        raise spec_error(location + ".id", "Use an ID such as 01-platform.")
    for dimension in ("width", "height"):
        require_number(
            figure[dimension], location + "." + dimension, positive=True
        )
    require_strings(figure["notes"], location + ".notes")
    require_strings(figure["sources"], location + ".sources")
    for index, source in enumerate(figure["sources"]):
        path = ROOT / source
        if (
            not source
            or Path(source).is_absolute()
            or ".." in Path(source).parts
            or not path.exists()
            or not path.resolve().is_relative_to(ROOT)
        ):
            raise spec_error(
                f"{location}.sources[{index}]",
                "Use an existing repository-relative path.",
            )
    keys = set()
    for index, panel in enumerate(figure["panels"]):
        panel_location = f"{location}.panels[{index}]"
        validate_panel(panel, panel_location, figure)
        if panel["key"] in keys:
            raise spec_error(panel_location + ".key", "Use a unique key.")
        keys.add(panel["key"])
    for index, edge in enumerate(figure["edges"]):
        validate_edge(edge, f"{location}.edges[{index}]", keys)
    return slug


def load_spec():
    """Reject malformed input before generating assets or creating exports.

    Error messages name the JSON field and an edit the author can make.
    In particular, export dates are checked before optional renderers run.
    """
    try:
        data = json.loads(SPEC.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise spec_error(
            f"line {error.lineno}, column {error.colno}",
            "Fix JSON syntax: " + error.msg,
        ) from None
    require_fields(
        data,
        "$",
        {
            "schema_version": int,
            "reviewed_on": str,
            "reviewed_source_commit": str,
            "figures": list,
        },
    )
    if data["schema_version"] != 1:
        raise spec_error("$.schema_version", "Use supported version 1.")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", data["reviewed_on"]):
        raise spec_error("$.reviewed_on", "Use a valid YYYY-MM-DD date.")
    try:
        date.fromisoformat(data["reviewed_on"])
    except ValueError:
        raise spec_error(
            "$.reviewed_on", "Use a valid YYYY-MM-DD date."
        ) from None
    if not re.fullmatch(r"[0-9a-f]{40}", data["reviewed_source_commit"]):
        raise spec_error(
            "$.reviewed_source_commit",
            "Use the reviewed commit's full 40-digit SHA.",
        )
    if not data["figures"]:
        raise spec_error("$.figures", "Add at least one figure.")
    names = set()
    for index, figure in enumerate(data["figures"]):
        location = f"$.figures[{index}]"
        slug = validate_figure(figure, location)
        if slug in names:
            raise spec_error(location + ".id", "Use a unique figure ID.")
        names.add(slug)
    return data


def embedded_logo(name, x, y, size=46):
    """Use only the seven attributed, repository-local upstream marks."""
    if not name:
        return ""
    if name not in ATTRIBUTED_LOGOS:
        raise ValueError("Unknown attributed logo.")
    # Reuse viewport normalization from the existing architecture generator.
    from render_architecture import logo

    return logo(name, x, y, size, size)


def render_panel(panel, slug):
    """Lay out a card without shrinking its text until it is unreadable.

    Geometry uses the SVG viewBox unit. CJK wrapping estimates full-width
    glyphs; overflow fails explicitly so an editor must fix text or space.
    """
    x, y, w, h = (panel[key] for key in ("x", "y", "width", "height"))
    fill, stroke, accent = TONES[panel["tone"]]
    parts = [rectangle(x, y, w, h, fill, stroke)]
    parts.append(rectangle(x, y + 18, 5, h - 36, accent, radius=2))
    title_width = w - 48 - (58 if panel["logo"] else 0)
    title_lines = wrap(panel["title"], title_width / 27)
    cursor = y + 42
    for line in title_lines:
        parts.append(text(x + 24, cursor, line, 27, INK, 700))
        cursor += 35
    if panel["logo"]:
        parts.append(embedded_logo(panel["logo"], x + w - 65, y + 18))
    cursor += 10
    for value in panel["lines"]:
        for line in wrap(value, (w - 48) / 23):
            parts.append(text(x + 24, cursor, line, 23))
            cursor += 32
    if panel["tag"]:
        cursor += 7
        for line in wrap(panel["tag"], (w - 48) / 20):
            parts.append(text(x + 24, cursor, line, 20, accent, 600))
            cursor += 27
    if cursor - 14 > y + h - 16:
        raise ValueError("Text overflow: " + slug + "/" + panel["key"])
    return "\n".join(parts)


def route_points(edge, panels):
    """Return connector points in viewBox units.

    Adjacent cards use automatic side/center ports. Nonadjacent routes use
    explicit waypoints in figures.json to avoid crossing another card.
    """
    if "points" in edge:
        return edge["points"]
    source, target = panels[edge["source"]], panels[edge["target"]]
    sx, sy = source["x"], source["y"]
    tx, ty = target["x"], target["y"]
    sw, sh = source["width"], source["height"]
    tw, th = target["width"], target["height"]
    if abs((sy + sh / 2) - (ty + th / 2)) < 40:
        if tx > sx:
            return [[sx + sw, sy + sh / 2], [tx, ty + th / 2]]
        return [[sx, sy + sh / 2], [tx + tw, ty + th / 2]]
    if ty > sy:
        return [[sx + sw / 2, sy + sh], [tx + tw / 2, ty]]
    return [[sx + sw / 2, sy], [tx + tw / 2, ty + th]]


def render_edge(edge, panels, slug):
    """Give data, control and optional routes distinct line patterns."""
    points = route_points(edge, panels)
    color, dash = EDGES[edge["kind"]]
    path = "M " + " L ".join(f"{x:g},{y:g}" for x, y in points)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    parts = [
        f'<path d="{path}" fill="none" stroke="{color}" '
        f'stroke-width="3"{dash_attr} '
        f'marker-end="url(#{slug}-{edge["kind"]})"/>'
    ]
    label = edge["label"]
    if label:
        first, second = points[0], points[1]
        x = (first[0] + second[0]) / 2
        y = (first[1] + second[1]) / 2 - 15
        if first[0] == second[0]:
            x += 12
        # Short labels remain readable even when crossing another connector.
        width = width_units(label) * 18 + 12
        parts.extend(
            [
                rectangle(x - width / 2, y - 19, width, 27, "#fff", radius=3),
                '<g text-anchor="middle">'
                + text(x, y, label, 18, color, 600)
                + "</g>",
            ]
        )
    return "\n".join(parts)


def render_svg(figure, data):
    """Create a standalone vector with accessible title and description."""
    slug = figure["id"]
    w, h = figure["width"], figure["height"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" '
        f'height="{h}" viewBox="0 0 {w} {h}" role="img" '
        f'aria-labelledby="{slug}-title {slug}-description" '
        f'font-family="{FONT}">',
        f'<title id="{slug}-title">{escape(figure["title"])}</title>',
        f'<desc id="{slug}-description">'
        + escape(figure["caption"])
        + " 根拠: "
        + escape(", ".join(figure["sources"]))
        + "</desc>",
        "<defs>",
    ]
    for kind, (color, _) in EDGES.items():
        parts.append(
            f'<marker id="{slug}-{kind}" markerWidth="10" '
            'markerHeight="10" refX="8" refY="4" orient="auto">'
            f'<path d="M0,0 L0,8 L9,4 Z" fill="{color}"/></marker>'
        )
    parts += ["</defs>", rectangle(0, 0, w, h, "#fff", radius=0)]
    parts += [
        rectangle(0, 0, 14, h, "#152a43", radius=0),
        text(
            56,
            43,
            "LAKEHOUSE FOUNDATION  /  PoC REPORT ASSETS",
            18,
            MUTED,
            600,
        ),
        text(56, 104, slug[:2] + "  " + figure["title"], 42, INK, 700),
    ]
    # The same route legend appears in every standalone report figure.
    for index, (kind, label) in enumerate(
        (
            ("data", "データ"),
            ("control", "制御・設定"),
            ("optional", "任意・計画"),
        )
    ):
        color, dash = EDGES[kind]
        x = w - 650 + index * 205
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(
            f'<path d="M{x},35 L{x + 50},35" stroke="{color}" '
            f'stroke-width="3"{dash_attr}/>'
        )
        parts.append(text(x + 62, 41, label, 17, MUTED))
    for index, line in enumerate(wrap(figure["subtitle"], (w - 112) / 23)):
        parts.append(text(56, 148 + index * 30, line, 23, MUTED))
    panels = {panel["key"]: panel for panel in figure["panels"]}
    parts.extend(render_panel(panel, slug) for panel in figure["panels"])
    parts.extend(render_edge(edge, panels, slug) for edge in figure["edges"])
    parts.append(rectangle(56, h - 165, w - 112, 1, "#d7dfe8", radius=0))
    cursor = h - 130
    for note in figure["notes"]:
        for line in wrap(note, (w - 112) / 20):
            parts.append(text(56, cursor, line, 20, MUTED))
            cursor += 27
    if cursor > h - 35:
        raise ValueError("Footer overflow: " + slug)
    parts.append(
        text(
            56,
            h - 23,
            "公開実装の説明 / 既存検証記録・計画は図内で区別 / "
            + data["reviewed_on"]
            + " / 根拠・キャプションは同梱台帳へ",
            17,
            MUTED,
        )
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def gallery(data, rendered):
    """Bundle all vectors inline so viewing needs no CDN or image server."""
    parts = [
        '<!doctype html><html lang="ja"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Lakehouse Foundation | PoC報告書用の図版集</title>",
        "<style>body{margin:0;background:#edf2f7;color:#152a43;"
        'font:16px/1.8 "Noto Sans CJK JP","Yu Gothic",sans-serif}'
        "main{max-width:1440px;margin:auto;padding:32px}"
        "h1{font-size:32px;line-height:1.4}a{color:#245fa6}"
        "nav{display:flex;flex-wrap:wrap;gap:8px 20px;margin:24px 0}"
        "article{background:white;padding:24px;margin:32px 0;"
        "border:1px solid #d7dfe8;border-radius:14px}"
        "svg{width:100%;height:auto;display:block}"
        ".caption{font-size:16px}.scope{color:#536780}"
        "details{margin-top:12px}h2{font-size:22px}"
        "@page{size:A4 landscape;margin:10mm}"
        "@media print{body{background:white}main{padding:0}header,nav{"
        "display:none}article{padding:0;margin:0;border:0;border-radius:0;"
        "break-after:page}article h2,details{display:none}"
        "article svg{max-height:155mm}.caption{font-size:9pt}}"
        "</style><main><header><h1>PoC報告書へ使う14種の図版</h1>",
        "<p>図は編集可能なSVG、説明と根拠は同梱のキャプション台帳。"
        "合成検証・会社での受入・追加計画を分けて使います。</p>",
        "<p>SVGはこのHTMLへ埋込済みです。閲覧時の外部通信は不要。"
        "PNG・PDF・PowerPointの一括出力はREADMEの手順を使用します。</p>",
        '</header><nav aria-label="図版一覧">',
    ]
    for figure in data["figures"]:
        parts.append(
            f'<a href="#{figure["id"]}">{escape(figure["title"])}</a>'
        )
    parts.append("</nav>")
    for figure in data["figures"]:
        slug = figure["id"]
        parts += [
            f'<article id="{slug}"><h2>{escape(figure["title"])}</h2>',
            rendered[slug],
            '<p class="caption"><strong>図'
            + slug[:2]
            + ": </strong>"
            + escape(figure["caption"])
            + "</p>",
            "<details><summary>用途・根拠・編集元</summary>",
            '<p class="scope">用途: '
            + escape(figure["audience"])
            + " / 照合日: "
            + escape(data["reviewed_on"])
            + "</p>",
            f'<p><a href="figures/{slug}.svg">SVG</a> / '
            f'<a href="mermaid/{slug}.mmd">Mermaid</a></p><ul>',
        ]
        for source in figure["sources"]:
            parts.append(
                f'<li><a href="../../{escape(source)}">'
                + escape(source)
                + "</a></li>"
            )
        parts.append("</ul></details></article>")
    parts.append("</main></html>\n")
    return "\n".join(parts)


def captions(data):
    """図の用途・制約・根拠を、共通の文書役割欄と一緒に生成する。

    冒頭の役割は文書registryを正本にする。図ごとの説明はfigures.jsonを
    正本にし、手で編集したCAPTIONSが次回生成で消える状態を避ける。
    実環境の結果を読み取ったり、評価済みと補完したりはしない。
    """
    parts = [
        "# 図キャプションと根拠の台帳\n",
        purpose_block("docs/reports/CAPTIONS.md") + "\n",
        "[図版集の使い方](README.md) / [HTML一覧](gallery.html)\n",
        "照合日: " + data["reviewed_on"] + "。生成元はfigures.jsonです。"
        "図の作成は、同日付で全機能を再試験したことを意味しません。\n",
        "## 初めて図を使うとき\n",
        "1. 説明したい問いを決め、下の用途から図を選びます。\n"
        "2. SVG/PNGを貼り、同じ図のキャプションを添えます。\n"
        "3. 合成条件・未評価・表単位commit等の注記も残します。\n"
        "4. 会社での結果は別の証跡IDで示し、図の照合日と分けます。\n",
        "captionは図の説明で、会社での実施結果の代筆ではありません。"
        "全体を示す図01は拡大可能な付録にし、説明の中心には用途別図を"
        "選ぶと読みやすくなります。\n",
        "## 根拠を追って変更するとき\n",
        "専門のレビューでは、各図の根拠からCompose・SQL・API・"
        "検証記録へ遡り、対象commitの差分を確認します。"
        "sourcesは公開ファイルの参照で、今回の会社受入の証跡を"
        "自動収集する機能ではありません。\n",
        "内容を直す場合はfigures.jsonのcaption・notes・sources・"
        "Mermaidを揃え、SVG/HTML/台帳を再生成します。"
        "[編集ガイド](EDITING-GUIDE.md)と"
        "[根拠対応表](EVIDENCE-MATRIX.md)を使います。\n",
    ]
    for figure in data["figures"]:
        slug = figure["id"]
        parts += [
            "## 図" + slug[:2] + " " + figure["title"] + "\n",
            "**用途:** " + figure["audience"] + "\n",
            "**キャプション:** " + figure["caption"] + "\n",
            f"[SVG](figures/{slug}.svg) / [Mermaid](mermaid/{slug}.mmd)\n",
        ]
        parts.extend("- " + note for note in figure["notes"])
        parts.append(
            "\n**根拠:** "
            + "、".join(
                f"[{Path(source).name}](../../{source})"
                for source in figure["sources"]
            )
            + "\n"
        )
    return "\n".join(parts)


def csv_index(data):
    """Write a spreadsheet-friendly index without deployment values."""
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["id", "title", "audience", "caption", "sources"])
    for figure in data["figures"]:
        writer.writerow(
            [
                figure["id"],
                figure["title"],
                figure["audience"],
                figure["caption"],
                " | ".join(figure["sources"]),
            ]
        )
    return stream.getvalue()


def generated_assets(data):
    """Return the complete deterministic text asset set."""
    rendered = {
        figure["id"]: render_svg(figure, data) for figure in data["figures"]
    }
    outputs = {
        REPORTS / "gallery.html": gallery(data, rendered),
        REPORTS / "CAPTIONS.md": captions(data),
        REPORTS / "figure-index.csv": csv_index(data),
    }
    for figure in data["figures"]:
        slug = figure["id"]
        outputs[REPORTS / "figures" / (slug + ".svg")] = rendered[slug]
        outputs[REPORTS / "mermaid" / (slug + ".mmd")] = figure["mermaid"]
    return outputs


def main():
    """Generate or verify public text assets; optionally export externally."""
    parser = argparse.ArgumentParser(
        description="公開図仕様からSVG・Mermaid・図キャプション・HTMLを生成します。",
        epilog=(
            "標準生成は追加依存不要。PNG/PDF/PPTX/ZIPの出力には、"
            "uvでdocs/reports/requirements-export.lockを同期し、"
            "Noto Sans CJK JPを用意してください。編集手順は"
            "docs/reports/EDITING-GUIDE.mdを参照してください。"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="書込みなしで生成元との一致を検査します。差分・不正入力は非0終了。",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        metavar="NEW_ABSOLUTE_DIRECTORY",
        help=(
            "SVG等の標準生成後、PNG/PDF/PPTX/ZIPもGit外の新規絶対dirへ生成します。"
            "既存dir・symlinkは拒否し、上書きしません。"
        ),
    )
    args = parser.parse_args()
    if args.check and args.export_dir:
        parser.error("Use --check and --export-dir separately.")
    data = load_spec()
    outputs = generated_assets(data)
    if args.check:
        stale = [
            path.relative_to(ROOT).as_posix()
            for path, value in outputs.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != value
        ]
        if stale:
            raise SystemExit("Stale report assets: " + ", ".join(stale))
        print(f"PASS: {len(data['figures'])} figures and all text exports.")
        return
    for path, value in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    print(f"Generated {len(data['figures'])} report figures and text exports.")
    if args.export_dir:
        from report_export import export_bundle

        export_bundle(data, outputs, args.export_dir)
        print("PNG, PDF, PowerPoint and contact sheet exported externally.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        # Display the actionable cause without an internal Python traceback.
        raise SystemExit("ERROR: " + str(error)) from None
