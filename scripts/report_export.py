"""公開SVGを報告書へ貼りやすい画像・文書形式へ書き出す補助モジュール。

入口はrender_report_assets.pyの--export-dir。ここへ直接CLIを追加しない。
処理は、安全検査→個別形式→PDF集→PPTX→説明文→hash台帳→ZIPの順。
描画用依存は専用uv環境だけに置き、稼働アプリの依存と混在させない。

公開の図仕様と生成済みSVGだけを入力にする。site.envや実GUIを収集せず、
出力はGit外の新規ディレクトリに限定する。既存報告書は上書きしない。
"""

import hashlib
import io
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "docs/reports"
PROJECT = "Lakehouse Foundation"
# PDF uses points (72 points/inch); SVG/PNG dimensions use pixels.
A3_LANDSCAPE_POINTS = (1190.55, 841.89)
A4_LANDSCAPE_POINTS = (841.89, 595.28)
PDF_MARGIN_POINTS = 18
STANDARD_FIGURE_WIDTH_PX = 1920
PNG_EXPORT_WIDTH_PX = 2880
SLIDE_WIDTH_INCHES = 13.333
SLIDE_HEIGHT_INCHES = 7.5


def external_destination(value: Path) -> Path:
    """Return a validated destination without creating it.

    Check lexical and resolved paths: a symlink must not turn a seemingly
    external path into an in-repository or existing report destination.
    """
    path = value.expanduser().absolute()
    if not value.expanduser().is_absolute():
        raise ValueError("Use an absolute export directory.")
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("Export report binaries outside the repository.")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Export paths must not use symlinks.")
    if path.exists():
        raise ValueError("Choose a new export directory; nothing is replaced.")
    return path


def validate_report_svg(content: str) -> ElementTree.Element:
    """Return parsed SVG after rejecting active or external content.

    Only the seven attributed local logo payloads may be embedded. A general
    data URL allowlist would also admit an unrelated screenshot or secret.
    Raises ValueError before a renderer can resolve a URL or local file.
    """
    if "<!doctype" in content.lower() or "<!entity" in content.lower():
        raise ValueError("Report SVG must not contain a DTD or entity.")
    root = ElementTree.fromstring(content)
    from render_architecture import logo

    allowed_logos = set()
    for name in (
        "iceberg",
        "spark",
        "trino",
        "grafana",
        "prometheus",
        "lakekeeper",
        "cloudflare",
    ):
        allowed_logos.add(re.search(r'href="([^"]+)"', logo(name, 0, 0))[1])
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name in {"script", "foreignObject", "iframe", "style"}:
            raise ValueError("Active SVG content is not a report figure.")
        for key, value in element.attrib.items():
            local = key.rsplit("}", 1)[-1].lower()
            if local.startswith("on"):
                raise ValueError("SVG event handlers are not permitted.")
            if local == "href" and value not in allowed_logos:
                raise ValueError("Only attributed local logos are allowed.")
            if "url(" in value and not re.fullmatch(
                r"url\(#[a-z0-9-]+\)", value
            ):
                raise ValueError("External SVG resources are not permitted.")
    return root


def write_pdf(
    svg_text, pdf_path, title, pdf_timestamp, svg_renderer, pdf_module
):
    """Fit a vector to A3/A4 landscape and replace volatile metadata."""
    raw = svg_renderer.svg2pdf(bytestring=svg_text.encode("utf-8"))
    reader = pdf_module.PdfReader(io.BytesIO(raw))
    original = reader.pages[0]
    # Dense service map uses A3; focused figures use A4 landscape.
    large = (
        float(validate_report_svg(svg_text).attrib["width"])
        > STANDARD_FIGURE_WIDTH_PX
    )
    width, height = A3_LANDSCAPE_POINTS if large else A4_LANDSCAPE_POINTS
    scale = min(
        (width - 2 * PDF_MARGIN_POINTS) / float(original.mediabox.width),
        (height - 2 * PDF_MARGIN_POINTS) / float(original.mediabox.height),
    )
    writer = pdf_module.PdfWriter()
    page = writer.add_blank_page(width, height)
    x = (width - float(original.mediabox.width) * scale) / 2
    y = (height - float(original.mediabox.height) * scale) / 2
    transform = pdf_module.Transformation().scale(scale).translate(x, y)
    page.merge_transformed_page(original, transform)
    writer.add_metadata(
        {
            "/Title": title,
            "/Author": PROJECT,
            "/Creator": PROJECT + " report exporter",
            "/Producer": PROJECT,
            "/CreationDate": pdf_timestamp,
            "/ModDate": pdf_timestamp,
        }
    )
    with pdf_path.open("wb") as stream:
        writer.write(stream)


def add_slides(data, directory, presentation):
    """Use PNG for faithful diagrams and editable captions/title text."""
    from pptx.util import Inches, Pt

    deck = presentation()
    deck.slide_width, deck.slide_height = (
        Inches(SLIDE_WIDTH_INCHES),
        Inches(SLIDE_HEIGHT_INCHES),
    )
    properties = deck.core_properties
    properties.title = PROJECT + " | PoC報告書用図版集"
    properties.author = properties.last_modified_by = PROJECT
    properties.subject = "Public implementation diagrams; synthetic evidence"
    properties.keywords = "lakehouse,PoC,Iceberg,report"
    stamp = datetime.fromisoformat(data["reviewed_on"])
    properties.created = properties.modified = stamp
    properties.revision = 1
    for figure in data["figures"]:
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        title = slide.shapes.add_textbox(
            Inches(0.3), Inches(0.08), Inches(12.7), Inches(0.38)
        ).text_frame
        paragraph = title.paragraphs[0]
        paragraph.text = figure["id"][:2] + "  " + figure["title"]
        paragraph.font.name = "Yu Gothic"
        paragraph.font.size = Pt(20)
        image_width = min(12.7, 5.6 * figure["width"] / figure["height"])
        image_height = image_width * figure["height"] / figure["width"]
        slide.shapes.add_picture(
            str(directory / "png" / (figure["id"] + ".png")),
            Inches((SLIDE_WIDTH_INCHES - image_width) / 2),
            Inches(0.54),
            width=Inches(image_width),
            height=Inches(image_height),
        )
        caption = slide.shapes.add_textbox(
            Inches(0.35), Inches(6.2), Inches(12.6), Inches(1.02)
        ).text_frame
        caption.word_wrap = True
        paragraph = caption.paragraphs[0]
        paragraph.text = figure["caption"]
        paragraph.font.name = "Yu Gothic"
        paragraph.font.size = Pt(12)
        notes = slide.notes_slide.notes_text_frame
        notes.text = (
            "用途: "
            + figure["audience"]
            + "\n"
            + "\n".join(figure["notes"])
            + "\n根拠:\n"
            + "\n".join(figure["sources"])
            + "\n"
            + "図の編集は同梱SVG/Mermaid/figures.jsonを使用。"
        )
    deck.save(directory / "lakehouse-poc-figures.pptx")


def contact_sheet(data, directory, image_module):
    """Create an image-only overview; each full-size figure stays separate."""
    width, cell_w, cell_h = 1920, 480, 300
    rows = (len(data["figures"]) + 3) // 4
    canvas = image_module.new("RGB", (width, rows * cell_h), "#edf2f7")
    for index, figure in enumerate(data["figures"]):
        with image_module.open(
            directory / "png" / (figure["id"] + ".png")
        ) as source:
            thumb = source.convert("RGB")
            thumb.thumbnail((cell_w - 16, cell_h - 16))
        x = index % 4 * cell_w + (cell_w - thumb.width) // 2
        y = index // 4 * cell_h + (cell_h - thumb.height) // 2
        canvas.paste(thumb, (x, y))
    canvas.save(directory / "contact-sheet.png", optimize=True)


def require_japanese_font():
    """Fail clearly rather than silently exporting missing Japanese glyphs."""
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{family}", "Noto Sans CJK JP"],
            check=True,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError(
            "Install fontconfig and fonts-noto-cjk before exporting."
        ) from None
    if "Noto Sans CJK JP" not in result.stdout:
        raise ValueError("Install the Noto Sans CJK JP font first.")


def portable_markdown(content: str, source: Path) -> str:
    """Keep report links local and send code/reference links to public Git."""

    def replace(match):
        """Rewrite one local Markdown link without fetching its target."""
        label, target = match.group(1), match.group(2)
        if "://" in target or target.startswith("#"):
            return match.group(0)
        location, separator, anchor = target.partition("#")
        resolved = (source.parent / location).resolve()
        if resolved.is_relative_to(REPORTS):
            relative = resolved.relative_to(REPORTS).as_posix()
        elif resolved.is_relative_to(ROOT):
            relative = (
                "https://github.com/hiromura16/lakehouse-foundation/blob/main/"
                + resolved.relative_to(ROOT).as_posix()
            )
        else:
            raise ValueError("Report link escapes the public repository.")
        return "[" + label + "](" + relative + separator + anchor + ")"

    return re.sub(r"\[([^\]\n]*)\]\(([^\s)]+)\)", replace, content)


def render_figure_exports(data, svgs, target, date):
    """Write one SVG/Mermaid/PNG/PDF set per figure.

    PNGs preserve the rendered pixels, but intentionally omit metadata.
    PDFs retain searchable text and embedded fonts; the detailed map uses A3.
    """
    import cairosvg
    import pypdf
    from PIL import Image

    for figure in data["figures"]:
        slug, svg = figure["id"], svgs[figure["id"]]
        (target / "svg" / (slug + ".svg")).write_text(svg)
        (target / "mermaid" / (slug + ".mmd")).write_text(figure["mermaid"])
        raw = cairosvg.svg2png(
            bytestring=svg.encode("utf-8"), output_width=PNG_EXPORT_WIDTH_PX
        )
        # Re-encode pixels without extra text/host metadata.
        with Image.open(io.BytesIO(raw)) as image:
            image.convert("RGB").save(
                target / "png" / (slug + ".png"), optimize=True
            )
        write_pdf(
            svg,
            target / "pdf" / (slug + ".pdf"),
            figure["title"],
            date,
            cairosvg,
            pypdf,
        )


def merge_pdf_book(data, target, date):
    """Join individual pages in manifest order without adding new results."""
    import pypdf

    writer = pypdf.PdfWriter()
    for figure in data["figures"]:
        writer.append(target / "pdf" / (figure["id"] + ".pdf"))
    writer.add_metadata(
        {
            "/Title": PROJECT + " | PoC図版集14図",
            "/Author": PROJECT,
            "/Creator": PROJECT + " report exporter",
            "/CreationDate": date,
            "/ModDate": date,
        }
    )
    with (target / "lakehouse-poc-figures.pdf").open("wb") as stream:
        writer.write(stream)


def copy_portable_documents(data, outputs, target):
    """Copy only named public templates and adapt their relative links.

    Report-to-report links stay inside the export directory. References to
    implementation files become public GitHub links, not local host paths.
    """
    for name in (
        "README.md",
        "POC-REPORT-TEMPLATE.md",
        "EVALUATION-PLAN.md",
        "EVIDENCE-MATRIX.md",
        "PRESENTATION-OUTLINE.md",
        "DECISION-RECORD-TEMPLATE.md",
        "IMPLEMENTATION-INVENTORY.md",
        "CAPTIONS.md",
        "SCREENSHOT-GUIDE.md",
        "EDITING-GUIDE.md",
    ):
        source = REPORTS / name
        (target / name).write_text(
            portable_markdown(source.read_text(), source), encoding="utf-8"
        )
    for name in (
        "figure-index.csv",
        "figures.json",
        "requirements-export.txt",
        "requirements-export.lock",
    ):
        shutil.copyfile(REPORTS / name, target / name)
    html = outputs[REPORTS / "gallery.html"].replace(
        'href="../../',
        'href="https://github.com/hiromura16/lakehouse-foundation/blob/main/',
    )
    (target / "gallery.html").write_text(html, encoding="utf-8")
    # gallery links retain their checkout-relative destinations. A standalone
    # export also provides the figures directory for its download links.
    (target / "figures").mkdir(mode=0o700)
    for figure in data["figures"]:
        shutil.copyfile(
            target / "svg" / (figure["id"] + ".svg"),
            target / "figures" / (figure["id"] + ".svg"),
        )


def write_export_manifest(data, target):
    """Record relative filenames and SHA-256 before packaging the bundle.

    The manifest deliberately excludes itself and the later ZIP to avoid a
    circular hash. It contains neither absolute paths nor environment values.
    """
    entries = []
    for path in sorted(target.rglob("*")):
        if path.is_file():
            entries.append(
                {
                    "path": path.relative_to(target).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    manifest = {
        "reviewed_on": data["reviewed_on"],
        "reviewed_source_commit": data["reviewed_source_commit"],
        "figure_count": len(data["figures"]),
        "files": entries,
        "scope": "public diagrams and templates; no deployment settings",
    }
    (target / "export-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )


def package_exports(target):
    """Archive only files in the newly created, controlled output directory."""
    with zipfile.ZipFile(
        target / "lakehouse-poc-report-assets.zip", "w", zipfile.ZIP_DEFLATED
    ) as bundle:
        for path in sorted(target.rglob("*")):
            if path.is_file() and path.suffix != ".zip":
                bundle.write(path, path.relative_to(target).as_posix())


def apply_private_permissions(target):
    """Leave local exports readable only by the operator who created them."""
    for path in target.rglob("*"):
        if path.is_file():
            path.chmod(0o600)


def export_bundle(
    specification: dict,
    generated_assets: dict[Path, str],
    export_directory: Path,
) -> None:
    """Run the export stages after validating every input and destination.

    Args:
        specification: Reviewed public figures.json content.
        generated_assets: SVG/text content produced by the renderer.
        export_directory: New absolute output directory outside this checkout.

    Raises:
        ValueError: Unsafe path/vector, missing export packages or CJK font.
        OSError: A local write failed; partial output remains for inspection.

    The destination is checked before creating files. Existing directories
    and symlinks are refused, so a mistaken argument cannot replace a report.
    """
    target = external_destination(export_directory)
    try:
        import cairosvg  # noqa: F401 - validate optional dependencies early.
        import pypdf  # noqa: F401 - no output directory exists yet.
        from PIL import Image
        from pptx import Presentation
    except ImportError:
        raise ValueError(
            "Install docs/reports/requirements-export.lock with uv first."
        ) from None
    require_japanese_font()
    svgs = {
        figure["id"]: generated_assets[
            REPORTS / "figures" / (figure["id"] + ".svg")
        ]
        for figure in specification["figures"]
    }
    for svg in svgs.values():
        validate_report_svg(svg)

    # Only after all preflight checks do we create our own output tree.
    target.mkdir(parents=True, mode=0o700)
    for kind in ("svg", "png", "pdf", "mermaid"):
        (target / kind).mkdir(mode=0o700)
    date = "D:" + specification["reviewed_on"].replace("-", "") + "000000Z"

    # Keep the orchestration readable as the same steps listed in the guide.
    render_figure_exports(specification, svgs, target, date)
    merge_pdf_book(specification, target, date)
    add_slides(specification, target, Presentation)
    contact_sheet(specification, target, Image)
    copy_portable_documents(specification, generated_assets, target)
    write_export_manifest(specification, target)
    package_exports(target)
    apply_private_permissions(target)
