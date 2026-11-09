"""Render docs/architecture.yaml into docs/architecture.svg.

Usage, from the repo root:
    uv run python scripts/render_architecture.py          # write the SVG
    uv run python scripts/render_architecture.py --check  # fail if it is out of date

Plain Python, no browser: text width is estimated, so a box that would overflow
is reported as an error instead of being drawn badly.
"""

import argparse
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "architecture.yaml"
TARGET = ROOT / "docs" / "architecture.svg"

MARKS = ("built", "simulated", "todo")
ARROW_KINDS = {
    "live": ("#2563eb", False),
    "planned": ("#94a3b8", True),
    "async": ("#d97706", True),
    "control": ("#dc2626", True),
    "probe": ("#7c3aed", True),
}
AREA_STYLES = ("band", "host", "group")
STATUS = {
    "built": ("Built", "#16a34a", "#f0fdf4", "#15803d", False),
    "partial": ("Partly built", "#d97706", "#fffbeb", "#b45309", False),
    "todo": ("Not built", "#94a3b8", "#f8fafc", "#64748b", True),
}
FONT = "Helvetica, Arial, sans-serif"
# Average glyph width as a share of the font size. Kept generous on purpose.
CHAR = 0.53
BOLD_CHAR = 0.6
PAD = 11
ITEM_SIZE = 14.5
ITEM_LINE = 18.5
OWNER = "#6d28d9"
UNOWNED = "#b91c1c"
MUTED = "#64748b"


class ArchitectureError(ValueError):
    """The YAML is invalid, or a box is too small for its text."""


def num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def width(text: str, size: float, bold: bool = False) -> float:
    return len(text) * size * (BOLD_CHAR if bold else CHAR)


def wrap(text: str, max_width: float, size: float, bold: bool = False) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if line and width(candidate, size, bold) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return [*lines, line] if line else lines


def text(
    x: float,
    y: float,
    content: str,
    size: float,
    fill: str = "#0f172a",
    bold: bool = False,
    anchor: str = "start",
    extra: str = "",
) -> str:
    weight = ' font-weight="700"' if bold else ""
    return (
        f'<text x="{num(x)}" y="{num(y)}" font-size="{num(size)}" fill="{fill}"'
        f'{weight} text-anchor="{anchor}"{extra}>{escape(content)}</text>'
    )


def mark(kind: str, cx: float, cy: float) -> str:
    if kind == "built":
        return (
            f'<circle cx="{num(cx)}" cy="{num(cy)}" r="9" fill="#16a34a"/>'
            f'<path d="M{num(cx - 4)} {num(cy)} L{num(cx - 1)} {num(cy + 3)} '
            f'L{num(cx + 4.5)} {num(cy - 3.5)}" stroke="#fff" stroke-width="2" '
            'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    if kind == "simulated":
        return f'<circle cx="{num(cx)}" cy="{num(cy)}" r="9" fill="#f59e0b"/>' + text(
            cx, cy + 4, "M", 11, "#451a03", bold=True, anchor="middle"
        )
    return (
        f'<circle cx="{num(cx)}" cy="{num(cy)}" r="8" fill="#fff" '
        f'stroke="{MUTED}" stroke-width="1.5"/>'
        f'<circle cx="{num(cx)}" cy="{num(cy)}" r="2.5" fill="none" '
        f'stroke="{MUTED}" stroke-width="1.2"/>'
    )


def item_mark(item: dict[str, str]) -> tuple[str, str]:
    ((kind, label),) = item.items()
    return kind, label


def box_status(items: list[dict[str, str]]) -> str:
    kinds = [item_mark(item)[0] for item in items]
    if all(kind == "built" for kind in kinds):
        return "built"
    if "built" not in kinds:
        return "todo"
    return "partial"


def check_keys(
    where: str, value: Any, required: set[str], optional: set[str]
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{where}: expected a mapping"]
    errors = [f"{where}: missing `{key}`" for key in sorted(required - set(value))]
    errors += [
        f"{where}: unknown key `{key}`"
        for key in sorted(set(value) - required - optional)
    ]
    return errors


def is_numbers(value: Any, count: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) == count
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
    )


def validate(doc: Any) -> None:
    errors = check_keys(
        "file", doc, {"title", "subtitle", "components", "layout"}, set()
    )
    if errors:
        raise ArchitectureError("\n".join(errors))
    components = doc["components"]
    if not isinstance(components, dict) or not components:
        raise ArchitectureError("components: expected a non-empty mapping")
    for name, comp in components.items():
        where = f"components.{name}"
        found = check_keys(where, comp, {"title", "owner", "items"}, {"tag", "style"})
        errors += found
        if found:
            continue
        if comp["owner"] is not None and not isinstance(comp["owner"], str):
            errors.append(f"{where}.owner: a name, or null if nobody owns it")
        if comp.get("style") not in (None, "stretch"):
            errors.append(f"{where}.style: only `stretch` is allowed")
        items = comp["items"]
        if not isinstance(items, list) or not items:
            errors.append(f"{where}.items: expected at least one item")
            continue
        for index, item in enumerate(items):
            ok = (
                isinstance(item, dict)
                and len(item) == 1
                and next(iter(item)) in MARKS
                and isinstance(next(iter(item.values())), str)
            )
            if not ok:
                errors.append(
                    f"{where}.items[{index}]: write `- built: text`, "
                    "`- simulated: text` or `- todo: text`"
                )
    layout = doc["layout"]
    errors += check_keys(
        "layout", layout, {"size", "boxes"}, {"areas", "notes", "arrows"}
    )
    if errors:
        raise ArchitectureError("\n".join(errors))
    if not is_numbers(layout["size"], 2):
        errors.append("layout.size: expected [width, height]")
    boxes = layout["boxes"] or {}
    for name in sorted(set(components) - set(boxes)):
        errors.append(f"layout.boxes: no position for `{name}`")
    for name in sorted(set(boxes) - set(components)):
        errors.append(f"layout.boxes: `{name}` is not a component")
    for name, at in boxes.items():
        if not is_numbers(at, 4):
            errors.append(f"layout.boxes.{name}: expected [x, y, width, height]")
    for index, area in enumerate(layout.get("areas") or []):
        where = f"layout.areas[{index}]"
        found = check_keys(where, area, {"at", "style", "title"}, {"note"})
        errors += found
        if not found and (
            not is_numbers(area["at"], 4) or area["style"] not in AREA_STYLES
        ):
            errors.append(f"{where}: needs `at: [x, y, w, h]` and a known style")
    for index, note in enumerate(layout.get("notes") or []):
        where = f"layout.notes[{index}]"
        found = check_keys(where, note, {"at", "text"}, {"color"})
        errors += found
        if not found and not is_numbers(note["at"], 3):
            errors.append(f"{where}.at: expected [x, y, width]")
    for index, arrow in enumerate(layout.get("arrows") or []):
        where = f"layout.arrows[{index}]"
        found = check_keys(where, arrow, {"kind", "points"}, {"label", "label_at"})
        errors += found
        if found:
            continue
        if arrow["kind"] not in ARROW_KINDS:
            errors.append(f"{where}.kind: one of {', '.join(ARROW_KINDS)}")
        points = arrow["points"]
        if not isinstance(points, list) or len(points) < 2:
            errors.append(f"{where}.points: at least two [x, y] points")
        elif not all(is_numbers(point, 2) for point in points):
            errors.append(f"{where}.points: each point is [x, y]")
        if ("label" in arrow) != ("label_at" in arrow) or (
            "label_at" in arrow and not is_numbers(arrow["label_at"], 2)
        ):
            errors.append(f"{where}: `label` and `label_at: [x, y]` go together")
    if errors:
        raise ArchitectureError("\n".join(errors))


def render_box(name: str, comp: dict[str, Any], at: list[float]) -> str:
    x, y, w, h = at
    status = box_status(comp["items"])
    label, stroke, fill, label_color, dashed = STATUS[status]
    if comp.get("style") == "stretch":
        fill = "#faf5ff" if status == "todo" else fill
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    parts = [
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="7" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="2"{dash}/>',
        text(x + w - PAD, y + 22, label, 12, label_color, bold=True, anchor="end"),
    ]
    title_width = w - 2 * PAD - width(label, 12, bold=True) - 10
    tag = comp.get("tag")
    title_lines = wrap(comp["title"], title_width, 16.5, bold=True)
    cursor = y + 24
    for index, line in enumerate(title_lines):
        last = index == len(title_lines) - 1
        tag_svg = (
            f'<tspan dx="8" font-size="12" font-weight="400" fill="#475569">'
            f"{escape(tag)}</tspan>"
            if tag and last
            else ""
        )
        parts.append(
            f'<text x="{num(x + PAD)}" y="{num(cursor)}" font-size="16.5" '
            f'font-weight="700" fill="#0f172a">{escape(line)}{tag_svg}</text>'
        )
        cursor += 20
    owner = comp["owner"]
    owner_text = f"Owner: {owner}" if owner else "Owner: not assigned"
    for line in wrap(owner_text, w - 2 * PAD, 12.5, bold=True):
        parts.append(
            text(x + PAD, cursor - 2, line, 12.5, OWNER if owner else UNOWNED, True)
        )
        cursor += 16
    cursor += 6
    text_x = x + PAD + 25
    for item in comp["items"]:
        kind, content = item_mark(item)
        lines = wrap(content, w - 2 * PAD - 25, ITEM_SIZE)
        parts.append(mark(kind, x + PAD + 9, cursor - 5))
        color = MUTED if kind == "todo" else "#0f172a"
        for line in lines:
            parts.append(text(text_x, cursor, line, ITEM_SIZE, color))
            cursor += ITEM_LINE
        cursor += 4
    if cursor - ITEM_LINE + 6 > y + h:
        overflow = cursor - ITEM_LINE + 6 - (y + h)
        raise ArchitectureError(
            f"layout.boxes.{name}: the text needs about {num(overflow)} px more "
            "height; make the box taller or shorten an item"
        )
    return f'<g id="{escape(name)}">{"".join(parts)}</g>'


def render_area(area: dict[str, Any]) -> str:
    x, y, w, h = area["at"]
    style = area["style"]
    if style == "band":
        shape = 'fill="#f8fafc" stroke="#cbd5e1" stroke-width="1" rx="12"'
    elif style == "host":
        shape = (
            f'fill="none" stroke="{MUTED}" stroke-width="2" '
            'stroke-dasharray="7 5" rx="12"'
        )
    else:
        shape = 'fill="#fcfcfd" stroke="#64748b" stroke-width="2" rx="9"'
    note = area.get("note")
    note_svg = (
        f'<tspan dx="8" font-size="15" font-weight="400" fill="{MUTED}">'
        f"{escape(note)}</tspan>"
        if note
        else ""
    )
    return (
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" {shape}/>'
        f'<text x="{num(x + 18)}" y="{num(y + 30)}" font-size="21" font-weight="700" '
        f'fill="#0f172a">{escape(area["title"])}{note_svg}</text>'
    )


def render_arrow(arrow: dict[str, Any]) -> str:
    color, dashed = ARROW_KINDS[arrow["kind"]]
    points = " ".join(f"{num(px)},{num(py)}" for px, py in arrow["points"])
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    svg = (
        f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"'
        f'{dash} marker-end="url(#arrow-{arrow["kind"]})"/>'
    )
    if "label" in arrow:
        lx, ly = arrow["label_at"]
        fill = "#475569" if arrow["kind"] == "planned" else color
        svg += text(
            lx,
            ly,
            arrow["label"],
            14,
            fill,
            anchor="middle",
            extra=' font-style="italic" stroke="#fff" stroke-width="5" '
            'paint-order="stroke"',
        )
    return svg


def render_legend(y: float) -> str:
    parts: list[str] = []
    x = 32.0
    for kind, label in (
        ("built", "Built on main"),
        ("simulated", "Works, but simulated"),
        ("todo", "Not built"),
    ):
        parts.append(mark(kind, x + 9, y - 5))
        parts.append(text(x + 25, y, label, 16, "#334155"))
        x += 25 + width(label, 16) + 26
    for label, color in (("Owner: name", OWNER), ("Owner: not assigned", UNOWNED)):
        parts.append(text(x, y, label, 12.5, color, bold=True))
        x += width(label, 12.5, bold=True) + 26
    x += 10
    for status in ("built", "partial", "todo"):
        label, stroke, fill, _, dashed = STATUS[status]
        dash = ' stroke-dasharray="5 3"' if dashed else ""
        parts.append(
            f'<rect x="{num(x)}" y="{num(y - 14)}" width="30" height="18" rx="4" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"{dash}/>'
        )
        parts.append(text(x + 38, y, label, 16, "#334155"))
        x += 38 + width(label, 16) + 26
    x += 10
    for kind, label in (("live", "Works today"), ("planned", "Planned")):
        color, dashed = ARROW_KINDS[kind]
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        parts.append(
            f'<line x1="{num(x)}" y1="{num(y - 5)}" x2="{num(x + 40)}" '
            f'y2="{num(y - 5)}" stroke="{color}" stroke-width="2"{dash}/>'
        )
        parts.append(text(x + 48, y, label, 16, "#334155"))
        x += 48 + width(label, 16) + 26
    return "".join(parts)


def render(doc: dict[str, Any]) -> str:
    validate(doc)
    layout = doc["layout"]
    w, h = layout["size"]
    markers = "".join(
        f'<marker id="arrow-{kind}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="7" markerHeight="7" orient="auto">'
        f'<path d="M0 0L10 5L0 10z" fill="{color}"/></marker>'
        for kind, (color, _) in ARROW_KINDS.items()
    )
    body = [
        f'<rect width="{num(w)}" height="{num(h)}" fill="#fff"/>',
        text(30, 50, doc["title"], 34, bold=True),
        text(32, 84, doc["subtitle"], 18, "#475569"),
        render_legend(118),
        *(render_area(area) for area in layout.get("areas") or []),
        *(
            render_box(name, comp, layout["boxes"][name])
            for name, comp in doc["components"].items()
        ),
    ]
    for note in layout.get("notes") or []:
        nx, ny, nw = note["at"]
        color = UNOWNED if note.get("color") == "red" else "#334155"
        for index, line in enumerate(wrap(note["text"], nw, 14.5)):
            body.append(text(nx, ny + 14 + index * 19, line, 14.5, color))
    body += [render_arrow(arrow) for arrow in layout.get("arrows") or []]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{num(w)}" height="{num(h)}" '
        f'viewBox="0 0 {num(w)} {num(h)}" font-family="{FONT}">\n'
        "<!-- Generated by scripts/render_architecture.py from docs/architecture.yaml."
        " Edit the YAML, not this file. -->\n"
        f"<title>{escape(doc['title'])}</title>\n"
        f"<defs>{markers}</defs>\n" + "\n".join(body) + "\n</svg>\n"
    )


def load(path: Path = SOURCE) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ArchitectureError(f"{path.name} is not valid YAML: {exc}") from exc
    validate(doc)
    return doc  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="fail if the SVG is out of date"
    )
    args = parser.parse_args(argv)
    try:
        svg = render(load())
    except ArchitectureError as exc:
        print(f"docs/architecture.yaml has a problem:\n{exc}", file=sys.stderr)
        return 2
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != svg:
            print(
                "docs/architecture.svg is out of date. Run:\n"
                "  uv run python scripts/render_architecture.py",
                file=sys.stderr,
            )
            return 1
        print("docs/architecture.svg matches docs/architecture.yaml")
        return 0
    TARGET.write_text(svg, encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
