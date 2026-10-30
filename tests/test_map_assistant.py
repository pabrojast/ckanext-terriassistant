import copy
from unittest.mock import Mock

import pytest
from jsonschema import ValidationError

from test_data_api import service, setup_conversation, BASE
from ckanext.terriassistant.data_assistant import map_tools, knowledge, spatial


def state():
    return {"map_context": {"revision": "snapshot", "basemaps": [{"id": "light", "name": "Light"}],
            "layers": [{"id": "layer:one", "name": "Water", "type": "geojson", "columns": [{"name": "value", "type": "scalar"}],
                        "traits": {"style": {"stroke": "black"}, "styles": [{"id": "original", "pointSize": {"nullSize": 9}}], "activeStyle": "original"},
                        "capabilities": list(map_tools.TRAITS) + ["table_style", "vector_style", "zoom"]}]},
            "sources": {"ckan:r": {"resource_id": "r"}}, "messages": [], "turn": {}}


def test_proposals_are_bound_to_context_and_preserve_unrelated_style():
    current = state()
    before = copy.deepcopy(current["map_context"])
    map_tools.propose(current, {"title": "Blue polygons", "operations": [
        {"kind": "style_layer", "source_id": "layer:one", "style_spec": {"fill_color": "#0000ff"}}
    ]})
    proposal = current["turn"]["proposals"][0]
    assert proposal["base_revision"] == "snapshot"
    assert proposal["operations"][0]["traits"]["style"] == {"stroke": "black", "fill": "#0000ff"}
    assert current["map_context"] == before


def test_table_style_preserves_geometry_and_rejects_invented_columns():
    current = state()
    edit = {"kind": "style_layer", "source_id": "layer:one", "style_spec": {"color_by": "value", "palette": "Blues"}}
    map_tools.propose(current, {"title": "Colours", "operations": [edit]})
    traits = current["turn"]["proposals"][0]["operations"][0]["traits"]
    assert len(traits["styles"]) == 2
    assert traits["styles"][-1]["pointSize"]["nullSize"] == 9
    edit["style_spec"]["color_by"] = "invented"
    with pytest.raises(ValueError, match="Unknown column"):
        map_tools.propose(current, {"title": "Wrong", "operations": [edit]})


def test_raster_uses_native_traits_and_keeps_nodata():
    layer = state()["map_context"]["layers"][0]
    layer.update(type="cog", capabilities=list(map_tools.TRAITS) + ["raster_style"],
                 traits={"renderOptions": {"single": {"noDataColor": "transparent", "colors": ["red", "blue"]}}})
    traits = map_tools.compile_style(layer, {"raster_style": {"palette": "greens", "minimum": 0, "maximum": 5, "reverse": True}})
    single = traits["renderOptions"]["single"]
    assert single["colorScaleMode"] == "named" and single["reverseColorScale"] is True
    assert single["domain"] == [0, 5] and single["noDataColor"] == "transparent"
    with pytest.raises(ValueError):
        map_tools.compile_style(layer, {"raster_style": {"minimum": 8, "maximum": 2}})


def test_proposals_reject_urls_not_supplied_by_user_and_unsupported_operations():
    current = state()
    edit = {"kind": "add_url", "url": "https://example.org/data.csv"}
    with pytest.raises(ValueError):
        map_tools.propose(current, {"title": "Add", "operations": [edit]})
    current["messages"] = [{"role": "user", "content": "Add https://example.org/data.csv"}]
    map_tools.propose(current, {"title": "Add", "operations": [edit]})
    with pytest.raises(ValidationError):
        map_tools.propose(current, {"title": "Code", "operations": [{"kind": "eval", "script": "alert(1)"}]})


def test_reviewed_knowledge_retrieves_format_specific_guidance():
    result = knowledge.search("paleta y rango del raster", "cog")
    assert result["documents"][0]["id"] == "K4"
    assert result["version"] and len(result["documents"]) <= 4


AREA = {"type": "Polygon", "coordinates": [[[0, 0], [5, 0], [0, 5], [0, 0]]]}


def test_polygon_holes_dateline_and_invalid_geometry():
    area = spatial.geometry({"type": "Polygon", "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]]
    ]})
    assert not area.intersects(spatial.geometry({"type": "Point", "coordinates": [3, 3]}))
    dateline = spatial.geometry({"type": "Polygon", "coordinates": [[[179, 0], [-179, 0], [-179, 2], [179, 2], [179, 0]]]})
    assert dateline.area == 4
    assert not dateline.intersects(spatial.geometry({"type": "Point", "coordinates": [0, 1]}))
    with pytest.raises(ValueError):
        spatial.geometry({"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [0, 2], [2, 0], [0, 0]]]})


def test_declared_world_and_wide_coverage_are_not_reversed():
    assert spatial.coverage({"xmin": -180, "ymin": -90, "xmax": 180, "ymax": 90}).area == 64800
    assert spatial.coverage({"xmin": -170, "ymin": 0, "xmax": 170, "ymax": 10}).area == 3400
    assert spatial.coverage({"xmin": 179, "ymin": 0, "xmax": -179, "ymax": 10}).area == 20
    assert spatial.coverage({"xmin": "invalid", "ymin": 0, "xmax": 10, "ymax": 10}) is None


def test_spatial_search_continues_past_false_positives_and_honours_exact_names(service, monkeypatch):
    s = service
    outside = {"id": "outside", "resources": [{"id": "r0"}], "spatial": {"type": "Point", "coordinates": [4, 4]}}
    inside = {"id": "inside", "resources": [{"id": "r1"}], "spatial": {"type": "Point", "coordinates": [1, 1]}}
    def search(context, params):
        assert params["extras"]["ext_bbox"] == "0.0,0.0,5.0,5.0"
        return {"count": 2, "results": [outside] if params["start"] == 0 else [inside]}
    s.tk.get_action = lambda name: search
    monkeypatch.setattr(s.sources, 'resource_for', lambda *args: ({"id": args[1]}, inside))
    result = s.sources.search("alice", "water quality", polygon=AREA, limit=1)
    assert [src["resource_id"] for src in result["sources"]] == ["r1"]
    assert result["examined"] == 2 and result["truncated"] is False
    s.tk.get_action = lambda name: lambda *args: outside
    assert not s.sources.search("alice", "outside", polygon=AREA)["sources"]


def test_catalog_resource_pagination_does_not_drop_resources(service):
    s = service
    package = {"id": "p", "resources": [{"id": str(i)} for i in range(25)]}
    s.tk.get_action = lambda name: lambda *args: {"count": 1, "results": [package]}
    first = s.sources.search("alice", "water quality")
    second = s.sources.search("alice", "water quality", cursor=first["next_cursor"])
    assert len(first["sources"]) == 20 and len(second["sources"]) == 5
    assert second["next_cursor"] is None


def test_search_hydrates_omitted_private_resources_with_user_permissions(service, monkeypatch):
    s = service
    package = {"id": "private-map", "resources": [{"id": "private-resource"}],
               "spatial": {"type": "Point", "coordinates": [1, 1]}}
    calls = []
    def show(context, params):
        calls.append((context, params))
        return package
    s.tk.get_action = lambda name: show if name == "package_show" else lambda *args: {
        "count": 1, "results": [{**package, "resources": []}]}
    monkeypatch.setattr(s.sources, 'resource_for', lambda *args: ({"id": args[1]}, package))
    result = s.sources.search("alice", "", organization="water", polygon=AREA)
    assert [src["resource_id"] for src in result["sources"]] == ["private-resource"]
    assert calls[0][0]["user"] == "alice"
    assert calls[0][1] == {"id": "private-map"}
    def denied(*args):
        raise s.tk.NotAuthorized("Revoked")
    s.tk.get_action = lambda name: denied if name == "package_show" else lambda *args: {
        "count": 1, "results": [{**package, "resources": []}]}
    assert s.sources.search("alice", "", organization="water", polygon=AREA)["sources"] == []


def test_api_negotiates_edits_and_rechecks_unselected_map_resources(service, monkeypatch):
    s = service
    assert s.client.get(BASE + '/bootstrap').json["capabilities"]["map_editing"] == 1
    cid, headers = setup_conversation(s)
    context = state()["map_context"]
    context["layers"][0]["resource_id"] = "private"
    response = s.client.post(BASE + f"/conversations/{cid}/turns", headers=headers, json={
        "request_id": "map-1", "question": "make it blue", "sources": [], "map_context": context})
    assert response.status_code == 202
    def denied(*args, **kwargs):
        raise s.tk.NotAuthorized()
    monkeypatch.setattr(s.sources, "resource_for", denied)
    assert s.client.get(BASE + "/turns/" + response.json["id"]).status_code == 403


def test_catalog_search_requires_csrf(service):
    assert service.client.post(BASE + "/catalog/search", json={"query": "water"}).status_code == 403
