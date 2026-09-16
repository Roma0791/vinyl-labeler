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
import math
import re
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
    "release_title_font_size": 45,  # "– Baby Wants To Ride EP" -- bumped +2pt for readability
    "genre_font_size": 39,          # it kept running out of room at the full release_title size
    "catno_font_size": 17,          # 0.22x the artist size
    "detail_font_size": 48,         # "A1  Baby Wants To Ride" ~= 65% width, bold
    "bpm_font_size": 77,            # 1.6x the detail size
    "highlight_bold": True,         # highlighted tracks' title (and BPM) render bold
    "rating_star_diameter": 26,     # printed star rating icon size
    "rating_star_gap": 6,           # spacing between the 5 star icons
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


def _split_song_and_variant(title: str):
    """Splits a Discogs-style "Song Name (Remix Name)" title into
    ("Song Name", "Remix Name"). Returns (None, None) if the title has no
    trailing parenthetical."""
    m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", title or "")
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()


def _display_titles(tracks: list, release_title: str) -> list:
    """A multi-mix 12" (several remixes of the same song) gets titles from
    Discogs like "Release Your Mind (Beep-Bop Mix)" on every track -- the
    song name repeats, and at label-line widths the fixed-width truncation
    eats the actual distinguishing part (the remix name) rather than the
    song name, which is redundant with itself track-to-track (and often
    with the release title already printed in the header above). Confirmed
    live: Syke 'n' Sugarstarr -- Release Your Mind (CITY 1054), 4 tracks,
    every title truncated down to "Release Your Mind (Beep-Bop ..." with
    the remix name never making it onto the label at all.

    When a track's song-name prefix is shared with 2+ other tracks, or
    matches the release title, it's redundant on that line -- this drops
    it and keeps just the remix/mix name. Titles with no "Song (Variant)"
    shape, or whose variant isn't shared/redundant, are returned as-is."""
    def norm(s):
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    parsed = [_split_song_and_variant(t.get("title", "")) for t in tracks]
    counts = {}
    for base, variant in parsed:
        if base and variant:
            counts[norm(base)] = counts.get(norm(base), 0) + 1
    release_norm = norm(release_title)

    out = []
    for t, (base, variant) in zip(tracks, parsed):
        if base and variant and (counts.get(norm(base), 0) >= 2 or norm(base) == release_norm):
            out.append(variant)
        else:
            out.append(t.get("title") or "")
    return out


def highlights_only(tracks: list) -> list:
    """For the "highlighted tracks only" print option -- keeps just the
    tracks flagged highlight=True, e.g. the two cuts worth playing off a
    4-track EP, skipping the rest of the tracklist on the label entirely."""
    return [t for t in tracks if t.get("highlight")]


def _star_points(cx: float, cy: float, outer_r: float, inner_r: float) -> list:
    """10 vertices alternating outer/inner radius, starting at the top and
    going clockwise -- the standard construction for a 5-point star."""
    pts = []
    for i in range(10):
        angle = math.radians(-90 + i * 36)
        r = outer_r if i % 2 == 0 else inner_r
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return pts


def _star_fill_fraction(rating: float, star_index: int) -> float:
    """star_index is 1-5. Mirrors the web UI's starFillPercent exactly, so
    a track's rating prints the same outline/half/full pattern shown on
    the review screen."""
    diff = (rating or 0) - (star_index - 1)
    if diff >= 1:
        return 1.0
    if diff >= 0.5:
        return 0.5
    return 0.0


def _draw_star(img: Image.Image, draw: ImageDraw.ImageDraw, cx: float, cy: float,
               diameter: float, fill_fraction: float) -> None:
    """One star centered at (cx, cy): outline always drawn (so an empty
    star is still visible as a shape, not blank space), then filled black
    from the left up to fill_fraction of its width (0, 0.5 or 1.0). The
    fill is pasted through a mask rather than drawn directly, so filling
    the left half can never overwrite the outline already drawn on the
    right half of a half-filled star."""
    outer_r = diameter / 2
    inner_r = outer_r * 0.45
    draw.polygon(_star_points(cx, cy, outer_r, inner_r), outline=0)
    if fill_fraction <= 0:
        return

    box = int(diameter) + 2
    mask = Image.new("L", (box, box), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.polygon(_star_points(box / 2, box / 2, outer_r, inner_r), fill=255)
    if fill_fraction < 1.0:
        mdraw.rectangle([box * fill_fraction, 0, box, box], fill=0)

    black = Image.new("1", (box, box), color=0)
    img.paste(black, (int(cx - box / 2), int(cy - box / 2)), mask=mask)


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

    # ---- header block: genre (small, right-aligned, on top) / artist
    # (big, up to 2 lines) / "– release title" (word-wrapped, widow-
    # avoided) / catalog number (small) -- all at fixed sizes. " / " is
    # tightened to "/" throughout to save width -- Discogs' multi-artist/
    # multi-part-title convention ("Tony Thomas / Mastik Soul") is wordy
    # for a label this size. ----
    artist = (record.get("artist", "") or "").replace(" / ", "/")
    artist_font = _font("bold", s["artist_font_size"])
    artist_lines = _wrap_text(draw, artist, artist_font, usable_width)
    if len(artist_lines) > 2:
        artist_lines = [artist_lines[0], " ".join(artist_lines[1:])]
    if len(artist_lines) == 2:
        artist_lines[1] = _truncate_to_width(draw, artist_lines[1], artist_font, usable_width)

    # Genre, right-aligned above everything else: one entry per visible
    # track (positionally mapped from Discogs' styles list -- see
    # discogs.distribute_styles_to_tracks), joined in track order with "/"
    # and no spaces. `tracks` here is already whatever's actually being
    # printed (_skip_on_label filtered, and highlights_only filtered by the
    # caller before this function ever sees the record), so a hidden
    # track's genre is naturally excluded too.
    genre_font = _font("regular", s["genre_font_size"])
    genre_values = [t["genre"].strip() for t in tracks if t.get("genre") and t["genre"].strip()]
    # Most EPs/singles run one style across every track (Discogs' styles
    # list is release-level to begin with -- see
    # discogs.distribute_styles_to_tracks) -- printing "HOUSE/HOUSE/HOUSE"
    # wastes the width for no information; print it once instead when
    # every visible track agrees.
    if genre_values and len({g.lower() for g in genre_values}) == 1:
        genre_text = genre_values[0].upper()
    else:
        genre_text = "/".join(genre_values).upper()
    genre_text = _truncate_to_width(draw, genre_text, genre_font, usable_width)

    release_title_font = _font("regular", s["release_title_font_size"])
    release_title_text = f"– {record.get('release_title', '')}".replace(" / ", "/").strip()
    release_title_lines = _wrap_text(draw, release_title_text, release_title_font, usable_width)

    catno = record.get("catalog_number") or ""
    catno_font = _font("regular", s["catno_font_size"])
    catno = _truncate_to_width(draw, catno, catno_font, usable_width)

    header_lines = []
    if genre_text:
        header_lines.append((genre_text, genre_font, s["genre_font_size"], "right"))
    header_lines += [(line, artist_font, s["artist_font_size"], "left") for line in artist_lines]
    header_lines += [(line, release_title_font, s["release_title_font_size"], "left")
                      for line in release_title_lines]
    if catno:
        header_lines.append((catno, catno_font, s["catno_font_size"], "left"))

    # ---- track rows: fixed detail/BPM sizes for every row; weight (not
    # size) is what marks a highlighted track ----
    def row_kind(t):
        return "bold" if (t.get("highlight") and s["highlight_bold"]) else "regular"

    display_titles = _display_titles(tracks, record.get("release_title", ""))

    row_plan = []
    for t, display_title in zip(tracks, display_titles):
        kind = row_kind(t)
        detail_font = _font(kind, s["detail_font_size"])
        bpm_font = _font(kind, s["bpm_font_size"])
        row_h = max(_line_height(s["detail_font_size"]), _line_height(s["bpm_font_size"])) \
            + _gap(s["bpm_font_size"])
        # Only unrated tracks skip the star row entirely -- most tracks on
        # a given EP won't have one set, and 5 empty outline stars under
        # every single track would just be ink/space spent on "no signal"
        # rather than the occasional favorite a rating is actually for.
        rating = t.get("rating") or 0
        star_row_h = int(s["rating_star_diameter"] * 1.4) if rating else 0
        row_plan.append({"track": t, "display_title": display_title, "detail_font": detail_font,
                          "bpm_font": bpm_font, "row_h": row_h, "rating": rating,
                          "star_row_h": star_row_h})

    # ---- now that every size is known, compute total height and render ----
    header_h = MARGIN_PX
    for _, _, size, _ in header_lines:
        header_h += _line_height(size) + _gap(size)
    content_h = sum(p["row_h"] + p["star_row_h"] for p in row_plan)
    fixed_h = _fixed_label_height(label_size)
    height = fixed_h if fixed_h else header_h + content_h + MARGIN_PX

    img = Image.new("1", (width, height), color=1)  # 1-bit, white background
    draw = ImageDraw.Draw(img)

    y = MARGIN_PX
    for text, font, size, align in header_lines:
        x = width - MARGIN_PX - draw.textlength(text, font=font) if align == "right" else MARGIN_PX
        draw.text((x, y), text, font=font, fill=0)
        y += _line_height(size) + _gap(size)

    for p in row_plan:
        t = p["track"]
        pos = t.get("position") or ""
        title = p["display_title"]
        bpm = t.get("bpm")
        if bpm is None:
            bpm_str = "--"
        else:
            # Always whole numbers on the label, regardless of what's
            # actually stored (older catalogue entries, manual entry).
            bpm = round(bpm)
            bpm_str = f"{bpm}?" if t.get("bpm_source") in (None, "none") else f"{bpm}"

        bpm_w = draw.textlength(bpm_str, font=p["bpm_font"])
        detail_max_w = usable_width - bpm_w - _gap(s["detail_font_size"])
        line = _truncate_to_width(draw, f"{pos}  {title}".strip(), p["detail_font"], detail_max_w)

        row_h = p["row_h"] - _gap(s["bpm_font_size"])
        detail_y = y + (row_h - draw.textbbox((0, 0), line, font=p["detail_font"])[3]) // 2
        bpm_y = y + (row_h - draw.textbbox((0, 0), bpm_str, font=p["bpm_font"])[3]) // 2

        draw.text((MARGIN_PX, max(y, detail_y)), line, font=p["detail_font"], fill=0)
        draw.text((width - MARGIN_PX - bpm_w, max(y, bpm_y)), bpm_str, font=p["bpm_font"], fill=0)
        y += p["row_h"]

        if p["rating"]:
            d = s["rating_star_diameter"]
            cx = MARGIN_PX + d / 2
            cy = y + p["star_row_h"] / 2
            for k in range(1, 6):
                _draw_star(img, draw, cx, cy, d, _star_fill_fraction(p["rating"], k))
                cx += d + s["rating_star_gap"]
            y += p["star_row_h"]

    return img
