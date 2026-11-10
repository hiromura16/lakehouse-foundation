"""Share SVG primitives while preserving each diagram family's styling.

Coordinate formatting and font inheritance are explicit: changing either can
create noisy generated diffs even when a drawing looks the same to a reader.
The public wrappers in each renderer keep their existing defaults.
"""

from html import escape

# These palettes intentionally differ; report figures need darker print text.
COLORS = {
    "architecture_ink": "#14263d",
    "architecture_blue": "#216bc4",
    "report_ink": "#152a43",
    "report_muted": "#536780",
}
ATTRIBUTED_LOGOS = (
    "iceberg",
    "spark",
    "trino",
    "grafana",
    "prometheus",
    "lakekeeper",
    "cloudflare",
)


def svg_text(
    x, y, value, size, fill, weight, *, font=None, coordinate_format=""
):
    """Escape a text label, optionally inheriting the root element's font."""
    font_attribute = f' font-family="{font}"' if font is not None else ""
    return (
        f'<text x="{format(x, coordinate_format)}" '
        f'y="{format(y, coordinate_format)}" fill="{fill}"{font_attribute} '
        f'font-size="{size}" font-weight="{weight}">{escape(value)}</text>'
    )


def svg_rectangle(
    x, y, width, height, fill, stroke, radius, *, coordinate_format=""
):
    """Draw the same rounded panel for either coordinate formatting style."""
    return (
        f'<rect x="{format(x, coordinate_format)}" '
        f'y="{format(y, coordinate_format)}" '
        f'width="{format(width, coordinate_format)}" '
        f'height="{format(height, coordinate_format)}" '
        f'rx="{radius}" fill="{fill}" stroke="{stroke}"/>'
    )
