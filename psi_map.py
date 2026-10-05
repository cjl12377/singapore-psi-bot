"""Render the PSI regional breakdown as a map of Singapore (PNG bytes)."""
import io
import json
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from location import AREA_REGION
from psi import LEGEND_ROWS, REGIONS, psi_category

_AREAS = json.loads((Path(__file__).parent / "planning_areas.json").read_text())["areas"]

# band label -> (pale region fill, solid badge colour, badge text colour)
_BAND_COLOURS = {
    "Good": ("#CDEBD0", "#2E9E4F", "#FFFFFF"),
    "Moderate": ("#FBEDB5", "#E0A800", "#2B2B2B"),
    "Unhealthy": ("#FAD7B0", "#E8731A", "#FFFFFF"),
    "Very Unhealthy": ("#F5BDBD", "#D32F2F", "#FFFFFF"),
    "Hazardous": ("#DDC6F0", "#7B2FBE", "#FFFFFF"),
}
_UNKNOWN = ("#E0E0E0", "#757575", "#FFFFFF")

_SEA = "#E6EFF5"
_INK = "#1F2933"
_MUTED = "#6B7785"
_KEY = "#4A5560"  # neutral sample badge in the legend
_DIVIDER = "#D5DEE6"
_SCALE = 2  # draw at 2x, downsample for smooth edges
_WIDTH, _HEIGHT = 900, 560  # layout coordinate space
_OUT_WIDTH = 720  # delivered pixel width; smaller = fewer bytes
_PALETTE_SIZE = 48
_PAD = 28
_LEGEND_H = 84

_lons = [x for a in _AREAS for ring in a["rings"] for x, _ in ring]
_lats = [y for a in _AREAS for ring in a["rings"] for _, y in ring]
_LON = (min(_lons), max(_lons))
_LAT = (min(_lats), max(_lats))

# Label anchor per region: area-weighted centroid of its planning areas, excluding
# water catchments/islands that would drag the label off the main island.
_LABEL_SKIP = {"Southern Islands", "Western Islands", "North-Eastern Islands",
               "Central Water Catchment", "Western Water Catchment"}


def _ring_area_centroid(ring: list) -> tuple[float, float, float]:
    a = cx = cy = 0.0
    for i in range(len(ring)):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % len(ring)]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if a == 0:
        return 0.0, ring[0][0], ring[0][1]
    return abs(a) / 2, cx / (3 * a), cy / (3 * a)


def _region_anchors() -> dict[str, tuple[float, float]]:
    sums: dict[str, list[float]] = {r: [0.0, 0.0, 0.0] for r in REGIONS}
    for area in _AREAS:
        if area["name"] in _LABEL_SKIP:
            continue
        for ring in area["rings"]:
            w, cx, cy = _ring_area_centroid(ring)
            s = sums[AREA_REGION[area["name"]]]
            s[0] += w
            s[1] += w * cx
            s[2] += w * cy
    return {r: (s[1] / s[0], s[2] / s[0]) for r, s in sums.items() if s[0]}


_ANCHORS = _region_anchors()


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("Helvetica", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)  # Pillow >= 10.1: scalable built-in font


def _project(lon: float, lat: float) -> tuple[float, float]:
    s = _SCALE
    map_w = (_WIDTH - 2 * _PAD) * s
    map_h = (_HEIGHT - 2 * _PAD - _LEGEND_H) * s
    # Equirectangular; at 1.3°N a degree of lon ≈ a degree of lat to within 0.03%.
    k = min(map_w / (_LON[1] - _LON[0]), map_h / (_LAT[1] - _LAT[0]))
    ox = _PAD * s + (map_w - k * (_LON[1] - _LON[0])) / 2
    oy = _PAD * s + (map_h - k * (_LAT[1] - _LAT[0])) / 2
    return ox + (lon - _LON[0]) * k, oy + (_LAT[1] - lat) * k


def _centered_text(draw, xy, text, font, fill) -> None:
    draw.text(xy, text, font=font, fill=fill, anchor="mm")


def render_psi_map(psi: dict, highlight_area: Optional[str] = None,
                   pm25: Optional[dict] = None) -> bytes:
    """psi: {region: 24-hr PSI}. pm25: {region: 1-hr PM2.5}; when given, the badge shows
    PM2.5 with the PSI beneath it (colours stay on the PSI band). highlight_area: planning
    area to outline (location lookups)."""
    s = _SCALE
    img = Image.new("RGB", (_WIDTH * s, _HEIGHT * s), _SEA)
    draw = ImageDraw.Draw(img)

    for area in _AREAS:
        region = AREA_REGION[area["name"]]
        label = psi_category(psi[region])[0] if region in psi else None
        fill = _BAND_COLOURS.get(label, _UNKNOWN)[0]
        for ring in area["rings"]:
            pts = [_project(x, y) for x, y in ring]
            draw.polygon(pts, fill=fill, outline="#FFFFFF", width=s)

    if highlight_area:
        for area in _AREAS:
            if area["name"] == highlight_area:
                for ring in area["rings"]:
                    pts = [_project(x, y) for x, y in ring]
                    draw.line(pts + [pts[0]], fill=_INK, width=3 * s, joint="curve")

    name_font, value_font, sub_font = _font(15 * s), _font(30 * s), _font(14 * s)
    for region, (lon, lat) in _ANCHORS.items():
        if region not in psi:
            continue
        label = psi_category(psi[region])[0]
        _, badge, text_col = _BAND_COLOURS.get(label, _UNKNOWN)
        x, y = _project(lon, lat)
        two_line = bool(pm25 and region in pm25)
        val = str(pm25[region]) if two_line else str(psi[region])
        sub = f"PSI {psi[region]}"
        vw = max(draw.textlength(val, font=value_font),
                 draw.textlength(sub, font=sub_font) if two_line else 0)
        bw, bh = max(vw + 28 * s, 64 * s), (58 if two_line else 44) * s
        draw.rounded_rectangle(
            (x - bw / 2, y - bh / 2, x + bw / 2, y + bh / 2),
            radius=min(bh / 2, 22 * s), fill=badge, outline="#FFFFFF", width=2 * s,
        )
        if two_line:
            _centered_text(draw, (x, y - 7 * s), val, value_font, text_col)
            _centered_text(draw, (x, y + 17 * s), sub, sub_font, text_col)
        else:
            _centered_text(draw, (x, y), val, value_font, text_col)
        _centered_text(draw, (x, y - bh / 2 - 12 * s), region.upper(), name_font, _INK)

    _draw_legend(draw, bool(pm25))

    out = img.resize((_OUT_WIDTH, round(_HEIGHT * _OUT_WIDTH / _WIDTH)), Image.LANCZOS)
    buf = io.BytesIO()
    _to_palette(out).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


_BANDS = [(_BAND_COLOURS[{"V. Unhealthy": "Very Unhealthy"}.get(label, label)][1], label, rng)
          for _, label, rng in LEGEND_ROWS]


def _key_badge(draw, cx: float, cy: float, s: int) -> float:
    """Neutral sample badge (PM2.5 over PSI) with labels to its right. Returns right edge x."""
    bw, bh = 70 * s, 52 * s
    draw.rounded_rectangle((cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2),
                           radius=20 * s, fill=_KEY)
    top_y, bot_y = cy - 8 * s, cy + 14 * s
    _centered_text(draw, (cx, top_y), "PM2.5", _font(18 * s), "#FFFFFF")
    _centered_text(draw, (cx, bot_y), "PSI", _font(12 * s), "#FFFFFF")
    tx = cx + bw / 2 + 12 * s
    f1, f2 = _font(14 * s), _font(12 * s)
    draw.text((tx, top_y), "1-hr PM2.5 (µg/m³)", font=f1, fill=_INK, anchor="lm")
    draw.text((tx, bot_y), "24-hr PSI (rolling)", font=f2, fill=_MUTED, anchor="lm")
    return tx + max(draw.textlength("1-hr PM2.5 (µg/m³)", font=f1),
                    draw.textlength("24-hr PSI (rolling)", font=f2))


def _draw_legend(draw, has_pm25: bool) -> None:
    """Bottom strip: sample badge (what the two badge numbers mean), then a PSI colour bar."""
    s = _SCALE
    mid = (_HEIGHT - _LEGEND_H / 2 - 10) * s
    name_f, rng_f, cap_f = _font(13 * s), _font(12 * s), _font(11 * s)

    left = 36 * s
    if has_pm25:
        right = _key_badge(draw, left + 35 * s, mid, s)
        bx0 = right + 26 * s
        draw.line((bx0 - 13 * s, mid - 22 * s, bx0 - 13 * s, mid + 22 * s), fill=_DIVIDER, width=s)
    else:
        bx0 = left
    bx1 = (_WIDTH - 36) * s
    draw.text((bx0, mid - 26 * s), "PSI LEVEL", font=cap_f, fill=_MUTED, anchor="lm")

    # Segmented colour bar, name under each segment, range inside
    bar_h, gap = 16 * s, 4 * s
    seg_w = (bx1 - bx0 - gap * 4) / 5
    by = mid - 6 * s
    for i, (colour, label, rng) in enumerate(_BANDS):
        sx = bx0 + i * (seg_w + gap)
        draw.rounded_rectangle((sx, by - bar_h / 2, sx + seg_w, by + bar_h / 2),
                               radius=bar_h / 2, fill=colour)
        _centered_text(draw, (sx + seg_w / 2, by), rng, rng_f,
                       "#2B2B2B" if label == "Moderate" else "#FFFFFF")
        _centered_text(draw, (sx + seg_w / 2, by + 22 * s), label, name_f, _INK)


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


_FLAT = sorted({_rgb(c) for pair in [*_BAND_COLOURS.values(), _UNKNOWN] for c in pair}
               | {_rgb(c) for c in (_SEA, _INK, _MUTED, _KEY, _DIVIDER, "#FFFFFF")})


def _to_palette(img: Image.Image) -> Image.Image:
    """8-bit palette PNG. Every flat colour is kept exactly (plain median-cut merges the
    similar severity badge colours); the rest of the palette is adaptive, for smooth edges."""
    adaptive = img.quantize(colors=_PALETTE_SIZE - len(_FLAT), method=Image.Quantize.MEDIANCUT,
                            dither=Image.Dither.NONE).getpalette()
    flat = [c for rgb in _FLAT for c in rgb]
    pal = Image.new("P", (1, 1))
    pal.putpalette((flat + adaptive[:3 * (_PALETTE_SIZE - len(_FLAT))])[:768])
    return img.quantize(palette=pal, dither=Image.Dither.NONE)
