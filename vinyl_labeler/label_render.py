"""
Stage 5: render the label image at the printer's native resolution (300dpi)
so it comes out crisp rather than being rescaled by brother_ql at print time.

Pixel dimensions are pulled from brother_ql.devicedependent.label_type_specs
rather than hardcoded, so changing printer.label_size just works.

Design (locked in 2026-09-15, dark-club/DJ-booth readability pass): a
one-time sizing pass, calibrated against a mock 4-track EP so the artist
name reads at 80% of the label's width and a track line at 65%, then fixed
as plain point sizes below. This is NOT a per-record auto-fit -- every
record renders at these same sizes, the same way the original fixed-size
design worked, just recalibrated bigger and locked in. Don't reintroduce
dynamic width-fitting here without being asked.

Font is Oswald: condensed and tall (high x-height), designed for exactly
this kind of high-impact headline legibility, unlike DejaVu Sans's more
neutral proportions. It ships as a single variable-weight file, so weight
is chosen via a font variation axis instead of loading separate
regular/bold files.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from brother_ql.devicedependent import label_type_specs

MARGIN_PX = 16
LINE_HEIGHT_FACTOR = 1.3   # rough ascender-to-descender multiplier for Oswald
GAP_FACTOR = 0.22          # inter-line gap, as a fraction of the taller line's height

FONT_CANDIDATES = [
    str(Path.home() / "Library" / "Fonts" / "Oswald[wght].ttf"),  # macOS, brew cask font-oswald
    "/usr/share/fonts/oswald/Oswald-VariableFont_wght.ttf",        # Linux, common package layout
    "Oswald[wght].ttf",  # last resort: let the OS font matcher resolve it by name
]
FONT_VARIATION = {"regular": b"Regular", "bold": b"Bold"}

# Fixed point sizes, calibrated once against the mock EP (see module docstring).
DEFAULT_STYLE = {
    "artist_font_size": 79,         # "Frankie Knuckles" ~= 80% of a 62mm label's width, bold
    "release_title_font_size": 43,  # "– Baby Wants To Ride EP" -- 0.55x the artist size
    "catno_font_size": 17,          # 0.22x the artist size
    "detail_font_size": 48,         # "A1  Baby Wants To Ride" ~= 65% width, bold
    "bpm_font_size": 77,            # 1.6x the detail size
    "highlight_bold": True,         # highlighted tracks' title (and BPM) render bold
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    else:
        return ImageFont.load_default()
    try:
        font.set_variation_by_name(FONT_VARIATION.get(kind, b"Regular"))
    except OSError:
        pass  # not actually a variable font (e.g. the load_default fallback path)
    return font


def _line_height(font_size: int) -> int:
    return int(font_size * LINE_HEIGHT_FACTOR)


def _gap(font_size: int) -> int:
    return int(font_size * GAP_FACTOR)


def _label_width(label_size: str) -> int:
    return label_type_specs[label_size]["dots_printable"][0]


def _fixed_label_height(label_size: str) -> int:
    """Non-zero for die-cut sizes; 0 for endless (continuous) tape."""
    return label_type_specs[label_size]["dots_printable"][1]


def _truncate_to_width(draw: ImageDraw.ImageDraw, text: str,
                        font: ImageFont.FreeTypeFont, max_width: float) -> str:
    """Safety net for the lines that don't word-wrap (artist, catalog
    number, track detail) -- at these large fixed sizes an unusually long
    string would otherwise run off the label edge."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return text + ellipsis if text else ellipsis


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: float) -> list:
    """Word-wrap only (never mid-word). If wrapping would leave a lone word
    on the last line, pull one word down from the line above so the last
    line always has at least two words when there's a word to spare."""
    words = text.split()
    if not words:
        return [text]
    lines = [[words[0]]]
    for word in words[1:]:
        trial = lines[-1] + [word]
        if draw.textlength(" ".join(trial), font=font) <= max_width:
            lines[-1] = trial
        else:
            lines.append([word])
    if len(lines) >= 2 and len(lines[-1]) == 1 and len(lines[-2]) >= 2:
        lines[-1].insert(0, lines[-2].pop())
    return [" ".join(line) for line in lines]


def highlights_only(tracks: list) -> list:
    """For the "highlighted tracks only" print option -- keeps just the
    tracks flagged highlight=True, e.g. the two cuts worth playing off a
    4-track EP, skipping the rest of the tracklist on the label entirely."""
    return [t for t in tracks if t.get("highlight")]


def render_label(record: dict, label_size: str, style: dict = None) -> Image.Image:
    """
    record shape:
    {
      "artist": str, "release_title": str, "catalog_number": str,
      "tracks": [{"position": str, "title": str, "bpm": float|None,
                   "bpm_source": str, "highlight": bool}]
    }
    Unconfirmed BPM (source in [None, "none"]) prints with a trailing "?" so
    a guess never quietly reads as a fact once it's on paper.
    """
    s = {**DEFAULT_STYLE, **(style or {})}
    tracks = [t for t in record.get("tracks", []) if not t.get("_skip_on_label")]

    width = _label_width(label_size)
    usable_width = width - 2 * MARGIN_PX
    img = Image.new("1", (width, 1), color=1)  # placeholder height; resized once content is known
    draw = ImageDraw.Draw(img)

    # ---- header block: artist (big) / "– release title" (word-wrapped,
    # widow-avoided) / catalog number (small) -- all at fixed sizes ----
    artist = record.get("artist", "") or ""
    artist_font = _font("bold", s["artist_font_size"])
    artist = _truncate_to_width(draw, artist, artist_font, usable_width)

    release_title_font = _font("regular", s["release_title_font_size"])
    release_title_text = f"– {record.get('release_title', '')}".strip()
    release_title_lines = _wrap_text(draw, release_title_text, release_title_font, usable_width)

    catno = record.get("catalog_number") or ""
    catno_font = _font("regular", s["catno_font_size"])
    catno = _truncate_to_width(draw, catno, catno_font, usable_width)

    header_lines = [(artist, artist_font, s["artist_font_size"])]
    header_lines += [(line, release_title_font, s["release_title_font_size"])
                      for line in release_title_lines]
    if catno:
        header_lines.append((catno, catno_font, s["catno_font_size"]))

    # ---- track rows: fixed detail/BPM sizes for every row; weight (not
    # size) is what marks a highlighted track ----
    def row_kind(t):
        return "bold" if (t.get("highlight") and s["highlight_bold"]) else "regular"

    row_plan = []
    for t in tracks:
        kind = row_kind(t)
        detail_font = _font(kind, s["detail_font_size"])
        bpm_font = _font(kind, s["bpm_font_size"])
        row_h = max(_line_height(s["detail_font_size"]), _line_height(s["bpm_font_size"])) \
            + _gap(s["bpm_font_size"])
        row_plan.append({"track": t, "detail_font": detail_font, "bpm_font": bpm_font, "row_h": row_h})

    # ---- now that every size is known, compute total height and render ----
    header_h = MARGIN_PX
    for _, _, size in header_lines:
        header_h += _line_height(size) + _gap(size)
    content_h = sum(p["row_h"] for p in row_plan)
    fixed_h = _fixed_label_height(label_size)
    height = fixed_h if fixed_h else header_h + content_h + MARGIN_PX

    img = Image.new("1", (width, height), color=1)  # 1-bit, white background
    draw = ImageDraw.Draw(img)

    y = MARGIN_PX
    for text, font, size in header_lines:
        draw.text((MARGIN_PX, y), text, font=font, fill=0)
        y += _line_height(size) + _gap(size)

    for p in row_plan:
        t = p["track"]
        pos = t.get("position") or ""
        title = t.get("title") or ""
        bpm = t.get("bpm")
        if bpm is None:
            bpm_str = "--"
        elif t.get("bpm_source") in (None, "none"):
            bpm_str = f"{bpm}?"
        else:
            bpm_str = f"{bpm}"

        bpm_w = draw.textlength(bpm_str, font=p["bpm_font"])
        detail_max_w = usable_width - bpm_w - _gap(s["detail_font_size"])
        line = _truncate_to_width(draw, f"{pos}  {title}".strip(), p["detail_font"], detail_max_w)

        row_h = p["row_h"] - _gap(s["bpm_font_size"])
        detail_y = y + (row_h - draw.textbbox((0, 0), line, font=p["detail_font"])[3]) // 2
        bpm_y = y + (row_h - draw.textbbox((0, 0), bpm_str, font=p["bpm_font"])[3]) // 2

        draw.text((MARGIN_PX, max(y, detail_y)), line, font=p["detail_font"], fill=0)
        draw.text((width - MARGIN_PX - bpm_w, max(y, bpm_y)), bpm_str, font=p["bpm_font"], fill=0)
        y += p["row_h"]

    return img
