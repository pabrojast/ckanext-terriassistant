from __future__ import annotations

import json

import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit

from .api import _get_controller, terriassistant_api
from .config import TerriassistantSettings
from . import resource_utils
from . import terria_builder as tb


PLUGIN_NAME = "terriassistant"


class TerriassistantPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IConfigurable, inherit=True)
    plugins.implements(plugins.IBlueprint)
    plugins.implements(plugins.IResourceView, inherit=True)
    plugins.implements(plugins.IActions, inherit=True)

    def __init__(self, name=None):
        super().__init__()
        self.settings = TerriassistantSettings()

    # IConfigurer
    def update_config(self, config_):
        toolkit.add_template_directory(config_, "templates")
        toolkit.add_public_directory(config_, "public")

    # IConfigurable
    def configure(self, config):
        self.settings = TerriassistantSettings.from_config(config)

    # IBlueprint
    def get_blueprint(self):
        return terriassistant_api

    # IResourceView
    def info(self):
        return {
            "name": PLUGIN_NAME,
            "title": toolkit._("Map Assistant"),
            "default_title": toolkit._(self.settings.default_title),
            "icon": "globe",
            "always_available": True,
            "filterable": False,
            "iframed": False,
            "schema": self.settings.schema_info(toolkit),
        }

    def can_view(self, data_dict):
        return self.settings.can_view_resource((data_dict or {}).get("resource"))

    def setup_template_variables(self, context, data_dict):
        view = data_dict["resource_view"]
        resource = data_dict["resource"]
        package = data_dict["package"]
        view_id = view.get("id")

        controller = _get_controller()
        encoded_config = ""
        terria_type = "csv"
        try:
            ctx = {
                "view": view,
                "resource": resource,
                "package": package,
                "user_context": controller._user_context(),
            }
            profile = controller.data_service.get_profile(resource, controller._fetch_context())
            terria_type = profile.get("terria_type") or "csv"
            raw_spec = None
            serialized = str(view.get("terria_state_json") or "").strip()
            if serialized:
                try:
                    parsed = json.loads(serialized)
                    if isinstance(parsed, dict):
                        raw_spec = parsed
                except ValueError:
                    raw_spec = None
            try:
                spec = tb.validate_style_spec(raw_spec, profile, terria_type) if raw_spec else None
            except tb.OverrideValidationError:
                spec = None
            active_spec = spec if spec else tb.default_style_spec(profile)
            _, encoded_config = controller._render(ctx, active_spec, profile, terria_type)
        except Exception:
            # Fall back to a bare catalog item so the map still shows the data.
            try:
                terria_type = tb.terria_type_for(
                    controller.data_service.resource_kind(resource), resource.get("format")
                )
                resource_url = resource_utils.get_resource_url(
                    resource, package, controller._user_context(), self.settings
                )
                catalog_item = tb.build_catalog_item(resource, resource_url, terria_type)
                encoded_config = tb.encode_init_source(tb.build_init_source(catalog_item, package))
            except Exception:
                encoded_config = ""

        return {
            "title": view.get("title", self.settings.default_title),
            "view_id": view_id,
            "resource_id": resource.get("id"),
            "package_id": package.get("id"),
            "terria_instance_url": self.settings.terria_instance_url,
            "encoded_config": encoded_config,
            "terria_type": terria_type,
            "terria_summary": view.get("terria_summary", ""),
            "chat_enabled": bool(self.settings.deepseek_api_key),
        }

    def view_template(self, context, data_dict):
        return "terriassistant.html"

    def form_template(self, context, data_dict):
        return "terriassistant_form.html"

    # IActions
    @toolkit.chained_action
    def resource_view_list(self, next_action, context, data_dict):
        existing_views = next_action(context, data_dict)
        try:
            resource_id = data_dict.get("id")
            if "model" not in context or "session" not in context:
                import ckan.model as model

                context.setdefault("model", model)
                context.setdefault("session", model.Session)
            resource = toolkit.get_action("resource_show")(context, {"id": resource_id})
        except Exception:
            return existing_views

        has_view = any(
            isinstance(view, dict) and view.get("view_type") == PLUGIN_NAME
            for view in existing_views
        )
        if has_view or resource is None:
            return existing_views
        if not self.settings.can_view_resource(resource):
            return existing_views

        create_payload = {
            "resource_id": resource["id"],
            "title": self.settings.default_title,
            "description": "",
            "view_type": PLUGIN_NAME,
            "terria_state_json": "",
            "terria_state_version": "1",
            "terria_summary": "",
            "saved_at": "",
            "saved_by": "",
        }
        create_context = {
            "model": context.get("model"),
            "session": context.get("session"),
            "user": "ckan.system",
            "ignore_auth": True,
        }
        try:
            toolkit.get_action("resource_view_create")(create_context, create_payload)
            return next_action(context, data_dict)
        except Exception:
            return existing_views

    def get_actions(self):
        return {"resource_view_list": self.resource_view_list}
