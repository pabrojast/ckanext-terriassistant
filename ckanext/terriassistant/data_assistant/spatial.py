"""WGS84 validation and antimeridian-aware intersections for catalog discovery."""
import json
import math

from shapely import affinity
from shapely.geometry import Polygon, MultiPolygon, box, shape
from shapely.ops import unary_union


def _ring(coords, unwrap=True):
    if len(coords) < 4 or coords[0] != coords[-1]:
        raise ValueError("Close the polygon ring")
    result = []
    for index, coord in enumerate(coords):
        if len(coord) != 2:
            raise ValueError("Use longitude/latitude coordinates")
        x, y = coord
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y)) or not -180 <= x <= 180 or not -90 <= y <= 90:
            raise ValueError("Coordinates must be finite WGS84 longitude/latitude")
        if result and unwrap and abs(coord[0] - coords[index - 1][0]) != 360:
            while x - result[-1][0] > 180:
                x -= 360
            while x - result[-1][0] < -180:
                x += 360
        result.append((x, y))
    return result


def geometry(data, unwrap=True):
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict) or data.get("type") not in ("Polygon", "MultiPolygon", "Point", "MultiPoint"):
        raise ValueError("Unsupported spatial coverage")
    if data["type"] in ("Point", "MultiPoint"):
        geom = shape(data)
        points = [geom] if data["type"] == "Point" else list(geom.geoms)
        if any(not (-180 <= p.x <= 180 and -90 <= p.y <= 90) for p in points):
            raise ValueError("Invalid point coverage")
        return geom
    polygons = data["coordinates"] if data["type"] == "MultiPolygon" else [data["coordinates"]]
    parts = []
    for rings in polygons:
        if not rings:
            raise ValueError("Empty polygon")
        exterior = _ring(rings[0], unwrap)
        holes = []
        for ring in rings[1:]:
            unwrapped = _ring(ring, unwrap)
            shift = round((exterior[0][0] - unwrapped[0][0]) / 360) * 360
            holes.append([(x + shift, y) for x, y in unwrapped])
        polygon = Polygon(exterior, holes)
        if polygon.is_empty or not polygon.is_valid or polygon.area == 0:
            raise ValueError("Draw a valid polygon without crossing edges")
        minx, _, maxx, _ = polygon.bounds
        if maxx - minx > 360:
            raise ValueError("Polygon wraps around the world more than once")
        for offset in range(math.floor((minx + 180) / 360), math.floor((maxx + 180) / 360) + 1):
            piece = polygon.intersection(box(-180 + 360 * offset, -90, 180 + 360 * offset, 90))
            if not piece.is_empty and piece.area > 0:
                parts.append(affinity.translate(piece, xoff=-360 * offset))
    result = unary_union(parts)
    if result.is_empty:
        raise ValueError("Empty area")
    return result


def coverage(package):
    extras = {e["key"]: e.get("value") for e in package.get("extras", []) if isinstance(e, dict) and "key" in e}
    data = package.get("spatial") or extras.get("spatial")
    try:
        if not data:
            values = [package.get(k, extras.get(k)) for k in ("xmin", "ymin", "xmax", "ymax")]
            if any(value is None or value == "" for value in values):
                return None
            xmin, ymin, xmax, ymax = map(float, values)
            data = {"type": "Polygon", "coordinates": [[[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax], [xmin, ymin]]]}
            return geometry(data, unwrap=xmin > xmax)
        # Stored GeoJSON follows RFC 7946: dateline coverage is a split
        # MultiPolygon. A wide declared rectangle must not become its complement.
        return geometry(data, unwrap=False)
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def bbox_string(geom):
    # A split dateline polygon has a broad envelope. Solr still returns candidates
    # correctly; the exact intersection and bounded continuation remove false positives.
    return ",".join(str(value) for value in geom.bounds)
