import json
from pathlib import Path
from typing import Optional

# NEA reports PSI for 5 regions but publishes no boundaries. Each URA planning
# area is assigned a region: URA's West/North/East regions map directly; URA
# Central and North-East areas go to the NEA region whose map label point is
# nearest the area's centroid. Edit a single line here to correct a mapping.
AREA_REGION = {
    "Ang Mo Kio": "central",
    "Bedok": "east",
    "Bishan": "central",
    "Boon Lay": "west",
    "Bukit Batok": "west",
    "Bukit Merah": "south",
    "Bukit Panjang": "west",
    "Bukit Timah": "central",
    "Central Water Catchment": "north",
    "Changi": "east",
    "Changi Bay": "east",
    "Choa Chu Kang": "west",
    "Clementi": "west",
    "Downtown Core": "south",
    "Geylang": "east",
    "Hougang": "east",
    "Jurong East": "west",
    "Jurong West": "west",
    "Kallang": "south",
    "Lim Chu Kang": "north",
    "Mandai": "north",
    "Marina East": "south",
    "Marina South": "south",
    "Marine Parade": "east",
    "Museum": "south",
    "Newton": "south",
    "North-Eastern Islands": "east",
    "Novena": "central",
    "Orchard": "south",
    "Outram": "south",
    "Pasir Ris": "east",
    "Paya Lebar": "east",
    "Pioneer": "west",
    "Punggol": "east",
    "Queenstown": "south",
    "River Valley": "south",
    "Rochor": "south",
    "Seletar": "north",
    "Sembawang": "north",
    "Sengkang": "north",
    "Serangoon": "central",
    "Simpang": "north",
    "Singapore River": "south",
    "Southern Islands": "south",
    "Straits View": "south",
    "Sungei Kadut": "north",
    "Tampines": "east",
    "Tanglin": "south",
    "Tengah": "west",
    "Toa Payoh": "central",
    "Tuas": "west",
    "Western Islands": "west",
    "Western Water Catchment": "west",
    "Woodlands": "north",
    "Yishun": "north",
}

SG_LAT = (1.13, 1.48)
SG_LON = (103.55, 104.15)
SNAP_DEG = 0.008  # ~900 m

# Simplified URA Master Plan planning-area outlines: rings are [lon, lat] pairs.
_AREAS = json.loads((Path(__file__).parent / "planning_areas.json").read_text())["areas"]


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def locate(lat: float, lon: float) -> Optional[tuple[str, str]]:
    """Returns (planning_area, psi_region), or None if the point is outside Singapore."""
    if not (SG_LAT[0] <= lat <= SG_LAT[1] and SG_LON[0] <= lon <= SG_LON[1]):
        return None
    for area in _AREAS:
        if any(_in_ring(lon, lat, ring) for ring in area["rings"]):
            return area["name"], AREA_REGION[area["name"]]
    # Beaches, shoreline and newer reclaimed land can fall just outside every
    # outline; snap to the closest outline if it's near enough, otherwise the
    # point is across the water (Johor, Batam) rather than on Singapore's edge.
    best_name, best_d2 = None, SNAP_DEG ** 2
    for area in _AREAS:
        for ring in area["rings"]:
            for x, y in ring:
                d2 = (x - lon) ** 2 + (y - lat) ** 2
                if d2 < best_d2:
                    best_name, best_d2 = area["name"], d2
    if best_name is None:
        return None
    return best_name, AREA_REGION[best_name]
