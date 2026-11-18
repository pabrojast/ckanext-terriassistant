"""Verify the view switch with real CKAN/Flask, isolated actions and no database.

Run in the application image with --network none. For a source overlay, set
PYTHONPATH and TERRIASSISTANT_EXPECTED_SOURCE_ROOT to its root.
"""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, url_for
from flask_babel import Babel
from werkzeug.exceptions import NotFound
from ckan.plugins import toolkit
from ckan import logic
from ckan.lib import datapreview
from ckan.views import api as api_views
from ckan.views import resource as resource_views
from ckanext.terriassistant import plugin as module
from ckanext.terriassistant.config import TerriassistantSettings


root = Path(os.environ.get("TERRIASSISTANT_EXPECTED_SOURCE_ROOT", "/app/src/ckanext-terriassistant")).resolve()
assert Path(module.__file__).resolve().is_relative_to(root)
plugin = module.TerriassistantPlugin()
assert TerriassistantSettings.from_config({}).view_enabled is True
assert TerriassistantSettings.from_config({"ckanext.terriassistant.view_enabled": "false"}).view_enabled is False

assistant = {"id": "assistant", "resource_id": "resource", "view_type": "terriassistant", "terria_state_json": "saved-style"}
standard = {"id": "standard", "resource_id": "resource", "view_type": "terria_view", "terria_state_json": "saved-terria"}
text = {"id": "text", "resource_id": "resource", "view_type": "text_view"}
records = [assistant, standard, text]
original = copy.deepcopy(records)
context, data = {"user": "public-reader"}, {"id": "resource"}


def next_list(ctx, params):
    assert ctx is context and params is data
    return records


for enabled in (True, False):
    plugin.configure({"ckanext.terriassistant.view_enabled": enabled})
    result = plugin.resource_view_list(next_list, context, data)
    expected = [standard, assistant, text] if enabled else [standard, text]
    assert result == expected and all(got is want for got, want in zip(result, expected))
    assert plugin.resource_view_list(lambda *_: [], context, data) == []
    assert plugin.resource_view_show(lambda *_: standard, context, data) is standard
    assert plugin.info()["always_available"] is enabled
    assert plugin.can_view({"resource": {"format": "CSV"}}) is enabled
    with patch.object(datapreview.p, "PluginImplementations", return_value=[plugin]):
        assert datapreview.get_allowed_view_plugins({"resource": {"format": "CSV"}}) == ([plugin] if enabled else [])

    app = Flask("assistant-view-" + str(enabled))
    app.secret_key = "isolated-view-switch-test"
    Babel(app)
    for blueprint in plugin.get_blueprint():
        app.register_blueprint(blueprint)
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert any(route.startswith("/api/terriassistant/data/") for route in routes)
    assert any(route.startswith("/api/terriassistant/view/") for route in routes) is enabled
    assert any(route.startswith("/api/terriassistant/resource/") for route in routes) is enabled
    if enabled:
        assert plugin.resource_view_show(lambda *_: assistant, context, data) is assistant
    else:
        try:
            plugin.resource_view_show(lambda *_: assistant, context, data)
        except toolkit.ObjectNotFound:
            pass
        else:
            raise AssertionError("Disabled view metadata was exposed")
        for suffix in ("bootstrap", "chat", "save"):
            assert app.test_client().get("/api/terriassistant/view/assistant/" + suffix).status_code == 404
        with app.test_request_context("/"):
            try:
                plugin.setup_template_variables({}, {"resource_view": assistant})
            except NotFound:
                pass
            else:
                raise AssertionError("Disabled JSON-supplied embedded preview rendered")

assert records == original and context == {"user": "public-reader"} and data == {"id": "resource"}

# Exercise real CKAN read/embed routing. Actions supply only isolated fixtures.
resource = {"id": "resource", "package_id": "dataset", "format": "CSV"}
package = {"id": "dataset", "resources": [resource], "license_id": "not-a-license"}


def action(name):
    def run(ctx, params):
        if name == "package_show":
            ctx["package"] = SimpleNamespace(type="dataset")
            return package
        if name == "resource_show":
            return resource
        if name == "resource_view_list":
            return plugin.resource_view_list(lambda *_: records, ctx, params)
        if name == "resource_view_show":
            return plugin.resource_view_show(lambda *_: assistant, ctx, params)
        raise AssertionError(name)
    return run


app = Flask("disabled-view-core-routes")
app.secret_key = "isolated-view-switch-test"
Babel(app)
with patch.object(resource_views, "get_action", side_effect=action), \
        patch.object(resource_views, "current_user", SimpleNamespace(name="", is_authenticated=False)), \
        patch.object(resource_views.model.Package, "get_license_register", return_value={}), \
        patch.object(resource_views, "_get_pkg_template", return_value="unused.html"), \
        patch.object(resource_views.base, "render", side_effect=lambda template, extra_vars: extra_vars["current_resource_view"]["view_type"]):
    with app.test_request_context("/?view_id=assistant"):
        try:
            resource_views.read("dataset", "dataset", "resource")
        except NotFound:
            pass
        else:
            raise AssertionError("Disabled direct view link did not return 404")
    with app.test_request_context("/"):
        assert resource_views.read("dataset", "dataset", "resource") == "terria_view"
        try:
            resource_views.view("dataset", "dataset", "resource", "assistant")
        except NotFound:
            pass
        else:
            raise AssertionError("Disabled embedded view link did not return 404")

# CKAN copies metadata from the chained wrapper, not the underlying GET action.
# Exercise its actual action assembly and HTTP dispatcher so a missing
# side_effect_free annotation cannot silently turn metadata GETs into HTTP 400.
def fixture_functions(action_module):
    if action_module.__name__ != "ckan.logic.action.get":
        return []
    return [
        ("resource_view_show", lambda ctx, params: standard if params["id"] == "standard" else assistant),
        ("resource_view_list", lambda ctx, params: records),
    ]


app = Flask("view-api-get-contract")
Babel(app)
app.add_url_rule("/api/3/action/<logic_function>", endpoint="api.action", view_func=api_views.action)
with patch.object(logic, "_actions", {}), \
        patch.object(logic.authz, "get_local_functions", side_effect=fixture_functions), \
        patch.object(logic.authz, "auth_functions_list", return_value=[]), \
        patch.object(logic.p, "PluginImplementations", return_value=[plugin]), \
        patch.object(api_views, "current_user", SimpleNamespace(name="", is_authenticated=False)), \
        patch.object(api_views, "url_for", url_for):
    for name in ("resource_view_list", "resource_view_show"):
        assert logic.get_action(name).side_effect_free is True
    for enabled in (True, False):
        plugin.configure({"ckanext.terriassistant.view_enabled": enabled})
        client = app.test_client()
        normal = client.get("/api/3/action/resource_view_show?id=standard")
        assert normal.status_code == 200, normal.data
        assert normal.json["result"] == standard
        hidden = client.get("/api/3/action/resource_view_show?id=assistant")
        assert hidden.status_code == (200 if enabled else 404), hidden.data
        listed = client.get("/api/3/action/resource_view_list?id=resource")
        assert listed.status_code == 200, listed.data
        assert listed.json["result"] == ([standard, assistant, text] if enabled else [standard, text])

print(json.dumps({"success": True, "default_enabled_compatible": True,
    "disabled_view_hidden": True, "new_view_option_hidden": True,
    "direct_and_embedded_links": 404, "legacy_api": 404,
    "standard_terria_default": True, "data_assistant_api_retained": True,
    "metadata_http_get": "normal 200; disabled assistant 404", "stored_records_unchanged": True,
    "network": "disabled", "database": "isolated action fixtures"}))
