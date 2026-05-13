"""Build TerriaJS v8 catalog/init JSON from a small, well-defined "style spec".

The chat assistant never emits raw TerriaJS trait JSON. Instead it returns a
compact ``style_spec`` (a flat dict of high-level options). This module
validates that spec against the dataset profile and translates it into a
correct TerriaJS v8 catalog-item config (``styles[]`` / ``activeStyle`` /
``style`` simplestyle / ``opacity`` / ...). That keeps the model's job easy and
guarantees the JSON we feed Terria is always well-formed.

Reference: terriajs/lib/Traits/TraitsClasses/Table/* and
terriajs/wwwroot/test/init/*.json.
"""
from __future__ import annotations

import copy
import json
import re
import urllib.parse
from typing import Any

# ---------------------------------------------------------------------------
# Resource format -> TerriaJS catalog item type
# ---------------------------------------------------------------------------
FORMAT_TO_TERRIA_TYPE = {
    "csv": "csv",
    "geojson": "geojson",
    "json": "geojson",
    "shp": "shp",
    "kml": "kml",
    "kmz": "kml",
    "czml": "czml",
    "wms": "wms",
    "wfs": "wfs-features",
    "wmts": "wmts",
    "esri": "esri-mapServer",
    "esri rest": "esri-mapServer",
    "tif": "cog",
    "tiff": "cog",
    "geotiff": "cog",
    "cog": "cog",
}

# Layer types that support data-driven table styling (styles[] / activeStyle).
TABLE_STYLEABLE_TYPES = {"csv", "geojson"}

# Palette names accepted from the model. TerriaJS understands ColorBrewer
# palettes ("<n>-class <Name>" or just the d3 scheme name) and d3-scale-chromatic
# scheme names (e.g. "Viridis" -> interpolateViridis). Case sensitive.
ALLOWED_PALETTES = {
    # sequential
    "Reds", "Blues", "Greens", "Greys", "Oranges", "Purples",
    "YlOrRd", "YlOrBr", "YlGnBu", "YlGn", "BuGn", "BuPu", "GnBu", "OrRd",
    "PuBuGn", "PuBu", "PuRd", "RdPu",
    # sequential (single-hue d3)
    "Viridis", "Inferno", "Magma", "Plasma", "Cividis", "Turbo", "Warm", "Cool",
    # diverging
    "RdBu", "RdGy", "RdYlBu", "RdYlGn", "Spectral", "BrBG", "PiYG", "PRGn",
    "PuOr",
    # categorical
    "Category10", "Set1", "Set2", "Set3", "Paired", "Dark2", "Accent",
    "Pastel1", "Pastel2", "Tableau10", "HighContrast",
}

# Maki / built-in marker symbols people commonly ask for. Terria also accepts
# any Maki icon name or an image URL; we allow URLs explicitly and otherwise
# accept anything that looks like a simple identifier.
COMMON_MARKERS = {
    "point", "circle", "square", "triangle", "star", "cross", "marker",
    "hospital", "school", "airport", "park", "water", "fuel", "restaurant",
    "cafe", "town", "city", "monument", "dam", "harbor",
}

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_RE = re.compile(r"^rgba?\([\d\s.,%/]+\)$", re.IGNORECASE)
_HSL_RE = re.compile(r"^hsla?\([\d\s.,%/deg]+\)$", re.IGNORECASE)
_CSS_COLOR_NAMES = {
    "red", "green", "blue", "black", "white", "grey", "gray", "yellow", "orange",
    "purple", "pink", "brown", "cyan", "magenta", "lime", "navy", "teal", "olive",
    "maroon", "silver", "gold", "transparent", "darkgreen", "darkblue", "darkred",
    "lightblue", "lightgreen", "lightgrey", "lightgray", "steelblue", "tomato",
    "salmon", "khaki", "indigo", "violet", "turquoise", "coral", "crimson",
    "forestgreen", "royalblue", "slategray", "dodgerblue", "firebrick",
}

_GEOJSON_MARKER_SIZES = ("small", "medium", "large")

STYLE_ID = "terriassistant"


class OverrideValidationError(Exception):
    """Raised when a style spec is not valid for the current dataset."""


# ---------------------------------------------------------------------------
# Catalog item / init source construction
# ---------------------------------------------------------------------------

def terria_type_for(resource_kind: str, resource_format: str | None) -> str:
    fmt = str(resource_format or "").strip().lower()
    if fmt in FORMAT_TO_TERRIA_TYPE:
        return FORMAT_TO_TERRIA_TYPE[fmt]
    if fmt.startswith("csv-geo"):
        return "csv"
    return FORMAT_TO_TERRIA_TYPE.get(resource_kind, "csv")


def catalog_item_id(resource_id: str) -> str:
    return "ckan-terriassistant-{0}".format(resource_id)


def build_catalog_item(resource: dict[str, Any], resource_url: str, terria_type: str) -> dict[str, Any]:
    name = resource.get("name") or resource.get("format") or "Map layer"
    item: dict[str, Any] = {
        "id": catalog_item_id(str(resource.get("id") or "")),
        "name": str(name),
        "type": terria_type,
        "url": resource_url,
    }
    if resource.get("description"):
        item["description"] = str(resource["description"])
    return item


def _world_bounds() -> dict[str, float]:
    return {"west": -180.0, "south": -85.0, "east": 180.0, "north": 85.0}


def _bounds_from_package(package: dict[str, Any]) -> dict[str, float]:
    spatial = package.get("spatial")
    if isinstance(spatial, str) and spatial.strip():
        try:
            geom = json.loads(spatial)
            coords = None
            if geom.get("type") == "Polygon":
                coords = geom["coordinates"][0]
            elif geom.get("type") == "MultiPolygon":
                coords = geom["coordinates"][0][0]
            elif geom.get("type") == "Point":
                lon, lat = geom["coordinates"][:2]
                pad = 1.0
                return {"west": lon - pad, "south": lat - pad, "east": lon + pad, "north": lat + pad}
            if coords:
                lons = [c[0] for c in coords]
                lats = [c[1] for c in coords]
                return {"west": min(lons), "south": min(lats), "east": max(lons), "north": max(lats)}
        except (ValueError, KeyError, TypeError, IndexError):
            pass
    return _world_bounds()


def build_init_source(catalog_item: dict[str, Any], package: dict[str, Any] | None = None) -> dict[str, Any]:
    bounds = _bounds_from_package(package or {})
    camera = {
        "west": bounds["west"],
        "south": bounds["south"],
        "east": bounds["east"],
        "north": bounds["north"],
    }
    return {
        "version": "8.0.0",
        "initSources": [
            {
                "homeCamera": camera,
                "initialCamera": camera,
                "catalog": [catalog_item],
                "workbench": [catalog_item["id"]],
            }
        ],
    }


def encode_init_source(init_source: dict[str, Any]) -> str:
    return urllib.parse.quote(json.dumps(init_source, ensure_ascii=False))


def apply_overrides(catalog_item: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Merge catalog-item-level overrides onto a copy of the catalog item."""
    item = copy.deepcopy(catalog_item)
    if not overrides:
        return item
    for key, value in overrides.items():
        item[key] = copy.deepcopy(value)
    return item


# ---------------------------------------------------------------------------
# Style spec validation
# ---------------------------------------------------------------------------

_SPEC_KEYS = {
    # data-driven color
    "color_by", "color_method", "palette", "bins", "bin_maximums",
    "category_colors", "min_value", "max_value", "null_color", "null_label",
    "outlier_color", "legend_ticks",
    # point size
    "size_by", "size_min", "size_max",
    # constant point/marker symbology
    "marker", "marker_size", "marker_rotation", "marker_color",
    "outline_color", "outline_width",
    # geojson simplestyle (polygons / lines)
    "fill_color", "fill_opacity", "stroke_color", "stroke_width", "stroke_opacity",
    # general
    "opacity", "clamp_to_ground",
    # csv column overrides
    "latitude_column", "longitude_column", "region_column",
}

_COLOR_METHODS = {"continuous", "bins", "categories", "constant"}


def _is_color(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    return bool(
        _HEX_RE.match(v) or _RGB_RE.match(v) or _HSL_RE.match(v) or v.lower() in _CSS_COLOR_NAMES
    )


def _num(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OverrideValidationError("'{0}' must be a number.".format(name))
    return float(value)


def _opacity(value: Any, name: str) -> float:
    n = _num(value, name)
    if not (0.0 <= n <= 1.0):
        raise OverrideValidationError("'{0}' must be between 0 and 1.".format(name))
    return n


def _column_names(profile: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for col in profile.get("columns") or []:
        if isinstance(col, dict) and col.get("name"):
            names.add(str(col["name"]))
    for col in profile.get("property_columns") or []:
        if isinstance(col, dict) and col.get("name"):
            names.add(str(col["name"]))
    return names


def _require_column(name: Any, columns: set[str], where: str) -> str:
    if not isinstance(name, str) or not name:
        raise OverrideValidationError("{0} must be a column name (string).".format(where))
    if columns and name not in columns:
        available = ", ".join(sorted(columns)) if columns else "(none reported)"
        raise OverrideValidationError(
            "{0} refers to unknown column '{1}'. Available columns: {2}.".format(where, name, available)
        )
    return name


def _color_or_raise(value: Any, where: str) -> str:
    if not _is_color(value):
        raise OverrideValidationError("{0} is not a valid CSS color (use a hex like '#1f77b4').".format(where))
    return value


def validate_style_spec(spec: Any, profile: dict[str, Any], terria_type: str) -> dict[str, Any]:
    """Validate and lightly normalise a style spec. Empty/None -> {} (no change).

    Raises ``OverrideValidationError`` describing the first problem found; the
    caller feeds that message back to the model for one retry.
    """
    if spec in (None, {}):
        return {}
    if not isinstance(spec, dict):
        raise OverrideValidationError("style_spec must be a JSON object.")

    unknown = set(spec) - _SPEC_KEYS
    if unknown:
        raise OverrideValidationError(
            "Unknown style_spec keys: {0}. Allowed keys: {1}.".format(
                ", ".join(sorted(unknown)), ", ".join(sorted(_SPEC_KEYS))
            )
        )

    columns = _column_names(profile)
    table_styleable = terria_type in TABLE_STYLEABLE_TYPES
    out: dict[str, Any] = {}

    # ---- opacity (all layer types) ----
    if "opacity" in spec:
        out["opacity"] = _opacity(spec["opacity"], "opacity")

    if not table_styleable:
        # Only opacity is meaningful for raster / OGC service layers.
        styling_keys = set(spec) - {"opacity"}
        if styling_keys:
            raise OverrideValidationError(
                "This layer type ({0}) only supports the 'opacity' option (got: {1}).".format(
                    terria_type, ", ".join(sorted(styling_keys))
                )
            )
        if not out:
            raise OverrideValidationError("style_spec did not contain any applicable option.")
        return out

    # ---- color ----
    if "color_by" in spec:
        out["color_by"] = _require_column(spec["color_by"], columns, "color_by")
    if "color_method" in spec:
        if spec["color_method"] not in _COLOR_METHODS:
            raise OverrideValidationError(
                "color_method must be one of {0}.".format(sorted(_COLOR_METHODS))
            )
        out["color_method"] = spec["color_method"]
    if "palette" in spec:
        if spec["palette"] not in ALLOWED_PALETTES:
            raise OverrideValidationError(
                "palette '{0}' is not supported. Use one of: {1}.".format(
                    spec["palette"], ", ".join(sorted(ALLOWED_PALETTES))
                )
            )
        out["palette"] = spec["palette"]
    if "bins" in spec:
        b = spec["bins"]
        if isinstance(b, bool) or not isinstance(b, int) or not (2 <= b <= 15):
            raise OverrideValidationError("bins must be an integer between 2 and 15.")
        out["bins"] = b
    if "bin_maximums" in spec:
        bm = spec["bin_maximums"]
        if not isinstance(bm, list) or not bm or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in bm
        ):
            raise OverrideValidationError("bin_maximums must be a non-empty list of numbers.")
        out["bin_maximums"] = [float(v) for v in bm]
    if "category_colors" in spec:
        cc = spec["category_colors"]
        if not isinstance(cc, list) or not cc:
            raise OverrideValidationError("category_colors must be a non-empty list.")
        clean = []
        for item in cc:
            if not isinstance(item, dict) or "value" not in item or "color" not in item:
                raise OverrideValidationError(
                    "category_colors items need 'value' and 'color', e.g. {\"value\":\"A\",\"color\":\"#ff0000\"}."
                )
            clean.append({"value": str(item["value"]), "color": _color_or_raise(item["color"], "category_colors color")})
        out["category_colors"] = clean
    for key in ("min_value", "max_value"):
        if key in spec:
            out[key] = _num(spec[key], key)
    if "null_color" in spec:
        out["null_color"] = _color_or_raise(spec["null_color"], "null_color")
    if "outlier_color" in spec:
        out["outlier_color"] = _color_or_raise(spec["outlier_color"], "outlier_color")
    if "null_label" in spec:
        out["null_label"] = str(spec["null_label"])
    if "legend_ticks" in spec:
        lt = spec["legend_ticks"]
        if isinstance(lt, bool) or not isinstance(lt, int) or not (0 <= lt <= 20):
            raise OverrideValidationError("legend_ticks must be an integer between 0 and 20.")
        out["legend_ticks"] = lt

    # ---- point size ----
    if "size_by" in spec:
        out["size_by"] = _require_column(spec["size_by"], columns, "size_by")
    for key in ("size_min", "size_max"):
        if key in spec:
            v = _num(spec[key], key)
            if v < 0:
                raise OverrideValidationError("{0} must be >= 0.".format(key))
            out[key] = v
    if "size_min" in out and "size_max" in out and out["size_min"] > out["size_max"]:
        raise OverrideValidationError("size_min must be <= size_max.")

    # ---- constant marker ----
    if "marker" in spec:
        marker = spec["marker"]
        if not isinstance(marker, str) or not marker:
            raise OverrideValidationError("marker must be a string (a Maki icon name or an image URL).")
        if not (marker in COMMON_MARKERS or marker.startswith(("http://", "https://", "data:")) or re.match(r"^[\w-]+$", marker)):
            raise OverrideValidationError("marker must be a simple icon name or an image URL.")
        out["marker"] = marker
    if "marker_size" in spec:
        ms = _num(spec["marker_size"], "marker_size")
        if not (1 <= ms <= 256):
            raise OverrideValidationError("marker_size must be between 1 and 256 (pixels).")
        out["marker_size"] = ms
    if "marker_rotation" in spec:
        out["marker_rotation"] = _num(spec["marker_rotation"], "marker_rotation")
    if "marker_color" in spec:
        out["marker_color"] = _color_or_raise(spec["marker_color"], "marker_color")
    if "outline_color" in spec:
        out["outline_color"] = _color_or_raise(spec["outline_color"], "outline_color")
    if "outline_width" in spec:
        ow = _num(spec["outline_width"], "outline_width")
        if ow < 0:
            raise OverrideValidationError("outline_width must be >= 0.")
        out["outline_width"] = ow

    # ---- geojson simplestyle (also tolerated for csv but ignored when building) ----
    if "fill_color" in spec:
        out["fill_color"] = _color_or_raise(spec["fill_color"], "fill_color")
    if "fill_opacity" in spec:
        out["fill_opacity"] = _opacity(spec["fill_opacity"], "fill_opacity")
    if "stroke_color" in spec:
        out["stroke_color"] = _color_or_raise(spec["stroke_color"], "stroke_color")
    if "stroke_width" in spec:
        sw = _num(spec["stroke_width"], "stroke_width")
        if sw < 0:
            raise OverrideValidationError("stroke_width must be >= 0.")
        out["stroke_width"] = sw
    if "stroke_opacity" in spec:
        out["stroke_opacity"] = _opacity(spec["stroke_opacity"], "stroke_opacity")

    # ---- general / column overrides ----
    if "clamp_to_ground" in spec:
        if not isinstance(spec["clamp_to_ground"], bool):
            raise OverrideValidationError("clamp_to_ground must be a boolean.")
        out["clamp_to_ground"] = spec["clamp_to_ground"]
    for key in ("latitude_column", "longitude_column", "region_column"):
        if key in spec:
            out[key] = _require_column(spec[key], columns, key)

    if not out:
        raise OverrideValidationError("style_spec did not contain any applicable option.")
    return out


# ---------------------------------------------------------------------------
# Style spec -> TerriaJS v8 catalog-item overrides
# ---------------------------------------------------------------------------

def _has_any(spec: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(k in spec for k in keys)


def _build_color_traits(spec: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
    color_by = spec.get("color_by")
    method = spec.get("color_method")
    # Infer method if a column was given but no method.
    if color_by and not method:
        kinds = {
            str(c.get("name")): c.get("kind")
            for c in (profile.get("columns") or profile.get("property_columns") or [])
            if isinstance(c, dict)
        }
        method = "continuous" if kinds.get(color_by) in ("numeric", "number") else "categories"
    color: dict[str, Any] = {}
    if color_by:
        color["colorColumn"] = color_by
    if method == "continuous":
        color["mapType"] = "continuous"
        color["numberOfBins"] = 0
    elif method == "bins":
        color["mapType"] = "bin"
        if "bins" in spec:
            color["numberOfBins"] = spec["bins"]
        if "bin_maximums" in spec:
            color["binMaximums"] = spec["bin_maximums"]
        if "numberOfBins" not in color and "binMaximums" not in color:
            color["numberOfBins"] = 5
    elif method == "categories":
        color["mapType"] = "enum"
        if "category_colors" in spec:
            color["enumColors"] = [
                {"value": c["value"], "color": c["color"]} for c in spec["category_colors"]
            ]
    elif method == "constant":
        color["mapType"] = "constant"
        if "marker_color" in spec:
            color["nullColor"] = spec["marker_color"]
    if "palette" in spec:
        color["colorPalette"] = spec["palette"]
    if "min_value" in spec:
        color["minimumValue"] = spec["min_value"]
    if "max_value" in spec:
        color["maximumValue"] = spec["max_value"]
    if "null_color" in spec:
        color["nullColor"] = spec["null_color"]
    if "outlier_color" in spec:
        color["outlierColor"] = spec["outlier_color"]
    if "null_label" in spec:
        color["nullLabel"] = spec["null_label"]
    if "legend_ticks" in spec:
        color["legendTicks"] = spec["legend_ticks"]
    # A constant point color with no color_by -> still produce a constant colormap.
    if not color and "marker_color" in spec:
        color = {"mapType": "constant", "nullColor": spec["marker_color"]}
    return color or None


def _build_point_traits(spec: dict[str, Any]) -> dict[str, Any] | None:
    null_pt: dict[str, Any] = {}
    if "marker" in spec:
        null_pt["marker"] = spec["marker"]
    if "marker_size" in spec:
        null_pt["width"] = spec["marker_size"]
        null_pt["height"] = spec["marker_size"]
    if "marker_rotation" in spec:
        null_pt["rotation"] = spec["marker_rotation"]
    return {"null": null_pt} if null_pt else None


def _build_outline_traits(spec: dict[str, Any]) -> dict[str, Any] | None:
    null_ol: dict[str, Any] = {}
    if "outline_color" in spec:
        null_ol["color"] = spec["outline_color"]
    if "outline_width" in spec:
        null_ol["width"] = spec["outline_width"]
    return {"null": null_ol} if null_ol else None


def _build_point_size_traits(spec: dict[str, Any]) -> dict[str, Any] | None:
    if "size_by" not in spec:
        return None
    ps: dict[str, Any] = {"pointSizeColumn": spec["size_by"]}
    if "size_min" in spec:
        ps["sizeOffset"] = spec["size_min"]
    if "size_min" in spec and "size_max" in spec:
        ps["sizeFactor"] = max(0.0, spec["size_max"] - spec["size_min"])
    elif "size_max" in spec:
        ps["sizeFactor"] = spec["size_max"]
    return ps


def _build_table_style(spec: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
    style: dict[str, Any] = {"id": STYLE_ID, "title": "Custom style"}
    for src, dst in (
        ("latitude_column", "latitudeColumn"),
        ("longitude_column", "longitudeColumn"),
        ("region_column", "regionColumn"),
    ):
        if src in spec:
            style[dst] = spec[src]
    color = _build_color_traits(spec, profile)
    if color:
        style["color"] = color
    point = _build_point_traits(spec)
    if point:
        style["point"] = point
    outline = _build_outline_traits(spec)
    if outline:
        style["outline"] = outline
    point_size = _build_point_size_traits(spec)
    if point_size:
        style["pointSize"] = point_size
    # Only return a style if it carries something beyond id/title.
    return style if len(style) > 2 else None


def _build_simplestyle(spec: dict[str, Any]) -> dict[str, Any] | None:
    style: dict[str, Any] = {}
    if "fill_color" in spec:
        style["fill"] = spec["fill_color"]
    if "fill_opacity" in spec:
        style["fill-opacity"] = spec["fill_opacity"]
    if "stroke_color" in spec:
        style["stroke"] = spec["stroke_color"]
    if "stroke_width" in spec:
        style["stroke-width"] = spec["stroke_width"]
    if "stroke_opacity" in spec:
        style["stroke-opacity"] = spec["stroke_opacity"]
    if "marker_color" in spec:
        style["marker-color"] = spec["marker_color"]
    if "marker" in spec and not str(spec["marker"]).startswith(("http://", "https://", "data:")):
        style["marker-symbol"] = spec["marker"]
    if "marker_size" in spec:
        ms = spec["marker_size"]
        style["marker-size"] = "small" if ms < 14 else ("medium" if ms < 26 else "large")
    return style or None


_DATA_DRIVEN_KEYS = ("color_by", "color_method", "palette", "bins", "bin_maximums",
                     "category_colors", "min_value", "max_value", "outlier_color",
                     "legend_ticks", "size_by", "size_min", "size_max")
_POINT_SYMBOL_KEYS = ("marker", "marker_size", "marker_rotation", "outline_color", "outline_width")


def build_overrides(spec: dict[str, Any] | None, profile: dict[str, Any], terria_type: str) -> dict[str, Any]:
    """Translate a (validated) style spec into TerriaJS v8 catalog-item overrides."""
    if not spec:
        return {}
    overrides: dict[str, Any] = {}
    if "opacity" in spec:
        overrides["opacity"] = spec["opacity"]
    if terria_type not in TABLE_STYLEABLE_TYPES:
        return overrides
    if "clamp_to_ground" in spec and terria_type == "geojson":
        overrides["clampToGround"] = spec["clamp_to_ground"]

    is_geojson = terria_type == "geojson"
    wants_data_driven = _has_any(spec, _DATA_DRIVEN_KEYS) or "null_color" in spec or any(
        k in spec for k in ("latitude_column", "longitude_column", "region_column")
    ) or "null_label" in spec
    wants_point_symbol = _has_any(spec, _POINT_SYMBOL_KEYS)
    wants_simplestyle = _has_any(spec, ("fill_color", "fill_opacity", "stroke_color", "stroke_width", "stroke_opacity", "marker_color"))

    if is_geojson and wants_simplestyle and not (wants_data_driven or wants_point_symbol):
        # Constant GeoJSON styling via Cesium primitives (simplestyle-spec).
        ss = _build_simplestyle(spec)
        if ss:
            overrides["style"] = ss
        return overrides

    # Table styling path (CSV always; GeoJSON when data-driven coloring/sizing).
    if wants_data_driven or wants_point_symbol or (not is_geojson and "marker_color" in spec):
        # For CSV, fold marker_color into a constant colormap so "make all points red" works.
        working_spec = dict(spec)
        style = _build_table_style(working_spec, profile)
        if style:
            overrides["styles"] = [style]
            overrides["activeStyle"] = STYLE_ID
    elif is_geojson and wants_simplestyle:
        ss = _build_simplestyle(spec)
        if ss:
            overrides["style"] = ss
    return overrides


# ---------------------------------------------------------------------------
# Defaults / human-readable summary
# ---------------------------------------------------------------------------

def default_style_spec(profile: dict[str, Any]) -> dict[str, Any]:
    """A sensible first styling so the layer doesn't look bare on first load."""
    if profile.get("kind") == "csv":
        numeric = profile.get("numeric_columns") or []
        if numeric:
            return {"color_by": numeric[0], "color_method": "continuous", "palette": "Viridis"}
    if profile.get("kind") == "geojson":
        gt = (profile.get("geometry_type") or "").lower()
        if "polygon" in gt:
            return {}  # let TerriaJS pick a default fill
        numeric_props = [
            c.get("name") for c in (profile.get("property_columns") or [])
            if isinstance(c, dict) and c.get("kind") in ("number", "numeric")
        ]
        if numeric_props:
            return {"color_by": numeric_props[0], "color_method": "continuous", "palette": "Viridis"}
    return {}


def summarize_style_spec(spec: dict[str, Any] | None) -> str:
    if not spec:
        return "Default map styling"
    parts: list[str] = []
    if spec.get("color_by"):
        method = spec.get("color_method") or "continuous"
        bit = "colored by {0}".format(spec["color_by"])
        if method == "bins":
            bit += " ({0} bins)".format(spec.get("bins", "binned"))
        elif method == "categories":
            bit += " (by category)"
        if spec.get("palette"):
            bit += " · {0}".format(spec["palette"])
        parts.append(bit)
    if spec.get("size_by"):
        parts.append("sized by {0}".format(spec["size_by"]))
    if spec.get("marker"):
        parts.append("marker {0}".format(spec["marker"]))
    if spec.get("marker_color"):
        parts.append("markers {0}".format(spec["marker_color"]))
    if spec.get("fill_color"):
        parts.append("fill {0}".format(spec["fill_color"]))
    if spec.get("stroke_color") or spec.get("outline_color"):
        parts.append("outline {0}".format(spec.get("stroke_color") or spec.get("outline_color")))
    if "opacity" in spec:
        parts.append("opacity {0}".format(spec["opacity"]))
    return ", ".join(parts) if parts else "Custom map styling"


# Backwards-compatible aliases (older call sites used these names).
validate_overrides = validate_style_spec
default_overrides = default_style_spec
summarize_overrides = summarize_style_spec
