from typing import Optional

# NEA's own reference points for each PSI region (regionMetadata.labelLocation
# in the data.gov.sg PSI response).
REGION_POINTS = {
    "north": (1.41803, 103.82),
    "south": (1.29587, 103.82),
    "east": (1.35735, 103.94),
    "west": (1.35735, 103.70),
    "central": (1.35735, 103.82),
}

SG_LAT = (1.13, 1.48)
SG_LON = (103.55, 104.15)


def nearest_region(lat: float, lon: float) -> Optional[str]:
    """Returns the closest PSI region, or None if the point is outside Singapore."""
    if not (SG_LAT[0] <= lat <= SG_LAT[1] and SG_LON[0] <= lon <= SG_LON[1]):
        return None
    return min(
        REGION_POINTS,
        key=lambda r: (REGION_POINTS[r][0] - lat) ** 2 + (REGION_POINTS[r][1] - lon) ** 2,
    )
