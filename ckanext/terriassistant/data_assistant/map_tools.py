"""Compile a closed map-edit vocabulary into Terria traits, never executable JSON."""
import copy
import json
import math
import re
import uuid
from urllib.parse import urlsplit

from jsonschema import Draft7Validator

from ..terria_builder import validate_style_spec, build_overrides, _SPEC_KEYS


def obj(properties, required=()):
    return dict(type="object", properties=properties, required=list(required), additionalProperties=False)


TEXT = dict(type="string", maxLength=200)
ID = dict(type="string", minLength=1, maxLength=300)
TRAITS = ("name", "show", "opacity", "style", "styles", "activeStyle", "defaultStyle",
          "renderOptions", "legends", "clampToGround", "forceCesiumPrimitives", "currentTime",
          "perPropertyStyles", "disableTableStyle", "selectedBandId", "hideLegendInWorkbench")
COG_PALETTES = "rainbow jet hsv hot cool spring summer autumn winter bone copper greys ylgnbu greens ylorrd bluered rdbu picnic portland blackbody earth electric".split()
CONTEXT = obj({
    "revision": ID,
    "camera_revision": ID,
    "camera": obj({key: dict(type="number") for key in ("west", "south", "east", "north")}, ["west", "south", "east", "north"]),
    "layers": dict(type="array", maxItems=100, items=obj({
        "id": ID, "name": TEXT, "type": TEXT, "resource_id": TEXT,
        "traits": obj({key: {} for key in TRAITS}),
        "capabilities": dict(type="array", maxItems=30, items=TEXT),
        "columns": dict(type="array", maxItems=200, items=obj({"name": TEXT, "type": TEXT}, ["name", "type"])),
        "dates": obj({"start": TEXT, "end": TEXT}),
        "wms_styles": dict(type="array", maxItems=100, items=TEXT),
        "bands": dict(type="array", maxItems=100, items=dict(type="integer", minimum=1)),
    }, ["id", "name", "type", "traits", "capabilities"])),
    "basemaps": dict(type="array", maxItems=50, items=obj({"id": ID, "name": TEXT}, ["id", "name"])),
    "basemap_id": TEXT,
}, ["revision", "layers", "basemaps"])

EDIT = obj({
    "kind": dict(enum=["update_layer", "style_layer", "remove_layer", "move_layer", "zoom", "set_view", "set_date", "set_basemap", "add_ckan", "add_url"]),
    "rectangle": obj({key: dict(type="number", minimum=-180 if key in ("west", "east") else -90,
                                maximum=180 if key in ("west", "east") else 90) for key in ("west", "south", "east", "north")}, ["west", "south", "east", "north"]),
    "source_id": ID, "name": TEXT, "show": dict(type="boolean"),
    "opacity": dict(type="number", minimum=0, maximum=1),
    "show_legend": dict(type="boolean"),
    "style_spec": obj({key: {} for key in _SPEC_KEYS}),
    "raster_style": obj({
        "palette": dict(enum=COG_PALETTES),
        "minimum": dict(type="number"), "maximum": dict(type="number"),
        "band": dict(type="integer", minimum=1, maximum=100),
        "reverse": dict(type="boolean"), "hide_outside": dict(type="boolean"),
    }),
    "wms_style": TEXT, "legend_title": TEXT,
    "index": dict(type="integer", minimum=0, maximum=99),
    "date": dict(type="string", minLength=10, maxLength=40),
    "basemap_id": ID, "view_id": ID,
    "url": dict(type="string", maxLength=2000), "format": TEXT,
}, ["kind"])
PROPOSAL = obj({"title": TEXT, "operations": dict(type="array", minItems=1, maxItems=20, items=EDIT)}, ["title", "operations"])


def validate_context(context):
    Draft7Validator(CONTEXT).validate(context)
    if len(json.dumps(context, allow_nan=False)) > 200000:
        raise ValueError("Map context is too large; select fewer layers")
    ids = [layer["id"] for layer in context["layers"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate map layer")
    return context


def merge(base, patch):
    result = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        result[key] = merge(result.get(key), value) if isinstance(value, dict) else copy.deepcopy(value)
    return result


def compile_style(layer, edit):
    traits = {}
    capabilities = layer["capabilities"]
    if "style_spec" in edit:
        if "table_style" not in capabilities:
            raise ValueError("This layer does not support table/vector styling")
        profile = {"columns": layer.get("columns", [])}
        spec = edit["style_spec"]
        # A column name must exist even when the layer is still loading.
        for field in ("color_by", "size_by", "latitude_column", "longitude_column", "region_column"):
            if field in spec and spec[field] not in {c["name"] for c in profile["columns"]}:
                raise ValueError("Unknown column: " + str(spec[field]))
        kind = "geojson" if "vector_style" in capabilities else "csv"
        traits = build_overrides(validate_style_spec(spec, profile, kind), profile, kind)
        if "style" in traits:
            traits["style"] = merge(layer["traits"].get("style"), traits["style"])
            traits["forceCesiumPrimitives"] = True
        if "styles" in traits:
            traits["forceCesiumPrimitives"] = False if kind == "geojson" else layer["traits"].get("forceCesiumPrimitives")
            if traits["forceCesiumPrimitives"] is None:
                del traits["forceCesiumPrimitives"]
            existing = copy.deepcopy(layer["traits"].get("styles") or [])
            style = traits["styles"][0]
            active = next((s for s in existing if s.get("id") == layer["traits"].get("activeStyle")), {})
            previous = next((s for s in existing if s.get("id") == style["id"]), active)
            style = merge(previous, style)
            traits["styles"] = [s for s in existing if s.get("id") != style["id"]] + [style]
        # Explicit legends from an imported SLD must not mislabel a new colour map.
        if ("styles" in traits or "style" in traits) and "legends" in capabilities:
            traits["legends"] = []
    if "raster_style" in edit:
        if "raster_style" not in capabilities:
            raise ValueError("This layer does not support COG styling")
        style = edit["raster_style"]
        single = copy.deepcopy(layer["traits"].get("renderOptions", {}).get("single") or {})
        if "palette" in style:
            single.update(colorScale=style["palette"], colorScaleMode="named")
        if ("minimum" in style) != ("maximum" in style):
            raise ValueError("Specify both minimum and maximum")
        if "minimum" in style:
            if not all(math.isfinite(style[k]) for k in ("minimum", "maximum")) or style["minimum"] >= style["maximum"]:
                raise ValueError("Invalid raster range")
            single["domain"] = [style["minimum"], style["maximum"]]
        for src, dst in (("band", "band"), ("reverse", "reverseColorScale"), ("hide_outside", "applyDisplayRange")):
            if src in style:
                single[dst] = style[src]
        if "band" in style and layer.get("bands") and style["band"] not in layer["bands"]:
            raise ValueError("Unknown raster band")
        traits["renderOptions"] = merge(layer["traits"].get("renderOptions"), {"single": single})
        if "legends" in capabilities:
            traits["legends"] = []
    if "wms_style" in edit:
        if "wms_style" not in capabilities or edit["wms_style"] not in layer.get("wms_styles", []):
            raise ValueError("Choose an advertised WMS style")
        traits["styles"] = edit["wms_style"]
        if "legends" in capabilities:
            traits["legends"] = []
    if "legend_title" in edit:
        styles = traits.get("styles")
        if not isinstance(styles, list) or not styles:
            raise ValueError("Legend titles require a table colour style")
        styles[-1].setdefault("color", {}).setdefault("legend", {})["title"] = edit["legend_title"]
    if not traits:
        raise ValueError("No supported style change was supplied")
    return traits


def propose(state, args):
    Draft7Validator(PROPOSAL).validate(args)
    context = state.get("map_context")
    if not context:
        raise ValueError("This client does not support map editing")
    layers = {layer["id"]: copy.deepcopy(layer) for layer in context["layers"]}
    operations = []
    for edit in args["operations"]:
        kind = edit["kind"]
        source_id = edit.get("source_id")
        layer = layers.get(source_id)
        op = {"kind": kind}
        if kind == "add_ckan":
            source = state["sources"].get(source_id, {})
            if not source.get("resource_id"):
                raise ValueError("Search CKAN and select a registered resource first")
            op.update(resource_id=source["resource_id"])
            if edit.get("view_id"):
                op["view_id"] = edit["view_id"]
        elif kind == "add_url":
            url = edit.get("url", "")
            parsed = urlsplit(url)
            supplied = any(m.get("role") == "user" and url in m.get("content", "") for m in state["messages"])
            if not url or not supplied or parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("Only a data URL explicitly supplied by the user can be added")
            op.update(url=url, format=edit.get("format", "auto"))
            if edit.get("name"):
                op["name"] = edit["name"]
        elif kind == "set_basemap":
            if edit.get("basemap_id") not in {b["id"] for b in context["basemaps"]}:
                raise ValueError("Unknown base map")
            op["basemap_id"] = edit["basemap_id"]
        elif kind == "set_view":
            rectangle = edit.get("rectangle", {})
            if not rectangle or rectangle["north"] <= rectangle["south"] or rectangle["east"] == rectangle["west"]:
                raise ValueError("Invalid map extent")
            op["rectangle"] = rectangle
        else:
            if layer is None:
                raise ValueError("Select an existing map layer")
            op["source_id"] = source_id
            if kind in ("style_layer", "update_layer", "set_date"):
                if kind == "style_layer":
                    traits = compile_style(layer, edit)
                elif kind == "set_date":
                    traits = {"currentTime": edit["date"]}
                else:
                    traits = {k: edit[k] for k in ("name", "show", "opacity") if k in edit}
                    if "show_legend" in edit:
                        traits["hideLegendInWorkbench"] = not edit["show_legend"]
                if not traits or set(traits) - set(layer["capabilities"]):
                    raise ValueError("The requested properties are not editable for this layer")
                op.update(kind="update_layer", traits=traits)
                if kind == "style_layer" and traits.get("legends") == []:
                    op["refresh_legend"] = True
                layer["traits"].update(traits)
            elif kind == "move_layer":
                if "index" not in edit or edit["index"] >= len(layers):
                    raise ValueError("Invalid layer position")
                op["index"] = edit["index"]
            elif kind == "zoom" and "zoom" not in layer["capabilities"]:
                raise ValueError("Layer has no map extent")
        operations.append(op)
    proposal = {"id": uuid.uuid4().hex, "title": args["title"],
                "base_revision": context["revision"], "operations": operations}
    if any(op["kind"] in ("zoom", "set_view") for op in operations) and context.get("camera_revision"):
        proposal["view_revision"] = context["camera_revision"]
    state["turn"].setdefault("proposals", []).append(proposal)
    return {"proposed": proposal["id"], "requires_click": True}
