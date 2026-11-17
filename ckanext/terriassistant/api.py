from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import urllib3
from flask import Blueprint, Response, request, stream_with_context
from flask_wtf.csrf import generate_csrf, validate_csrf
from wtforms.validators import ValidationError
from werkzeug.http import dump_options_header

import ckan.plugins.toolkit as toolkit
from .data_assistant.sources import open_public_resource

from .config import TerriassistantSettings
from .data_service import (
    DataParseError,
    DataService,
    DataServiceError,
    DataTooLargeError,
    FetchContext,
)
from .llm_service import (
    LlmConfigurationError,
    LlmResponseError,
    LlmService,
    LlmServiceError,
)
from . import resource_utils
from . import terria_builder as tb


terriassistant_api = Blueprint("terriassistant_api", __name__)
_CONTROLLER: "TerriassistantController | None" = None


def _get_controller() -> "TerriassistantController":
    global _CONTROLLER
    settings = TerriassistantSettings.from_config(toolkit.config)
    if _CONTROLLER is None or _CONTROLLER.settings != settings:
        _CONTROLLER = TerriassistantController(settings)
    return _CONTROLLER


class TerriassistantController:
    def __init__(self, settings: TerriassistantSettings):
        self.settings = settings
        self.data_service = DataService(settings)
        self.llm_service = LlmService(settings)

    # ---- endpoints ----------------------------------------------------

    def bootstrap(self, view_id: str) -> Response:
        try:
            ctx = self._load_view_context(view_id)
            profile = self._profile(ctx)
            terria_type = profile.get("terria_type") or "csv"
            saved_spec, warnings = self._effective_saved_spec(ctx["view"], profile, terria_type)
            default_spec = tb.default_style_spec(profile)
            active_spec = saved_spec if saved_spec else default_spec
            init_source, encoded = self._render(ctx, active_spec, profile, terria_type)
            _, default_encoded = self._render(ctx, default_spec, profile, terria_type)
            current_user = ctx["user_context"].get("auth_user_obj")
            can_chat = self._can_chat(ctx["user_context"])
            return self._json(
                {
                    "view_id": view_id,
                    "resource": {
                        "id": ctx["resource"].get("id"),
                        "name": ctx["resource"].get("name"),
                        "format": ctx["resource"].get("format"),
                        "package_id": ctx["package"].get("id"),
                    },
                    "profile": profile,
                    "terria_type": terria_type,
                    "terria_instance_url": self.settings.terria_instance_url,
                    "saved_spec": saved_spec,
                    "active_spec": active_spec,
                    "default_spec": default_spec,
                    "default_encoded_config": default_encoded,
                    "init_source": init_source,
                    "encoded_config": encoded,
                    "warnings": warnings,
                    "chat_enabled": bool(self.settings.deepseek_api_key),
                    "can_chat": can_chat,
                    "can_save": ctx["can_save"],
                    "csrf_token": generate_csrf(),
                    "storage_scope": str(getattr(current_user, "id", None) or ctx["user_context"].get("user") or "anonymous"),
                    "login_url": toolkit.url_for("user.login", came_from=toolkit.url_for(
                        "resource.read", id=ctx["package"].get("name") or ctx["package"]["id"],
                        resource_id=ctx["resource"]["id"], view_id=view_id)),
                    "suggested_prompts": profile.get("suggested_prompts", []),
                }
            )
        except toolkit.NotAuthorized:
            return self._err("Not authorized to access this resource.", 403)
        except toolkit.ObjectNotFound:
            return self._err("Resource view not found.", 404)
        except (DataTooLargeError, DataParseError, DataServiceError) as exc:
            return self._err(str(exc), 422)
        except Exception:
            return self._err("Unexpected error while loading the map view.", 500)

    def chat(self, view_id: str) -> Response:
        try:
            if not self._can_chat(self._user_context()):
                return self._err("Sign in to IHP to use the map assistant.", 401)
            ctx = self._load_view_context(view_id)
            body = request.get_json(silent=True)
            messages = body.get("messages") or []
            if (not isinstance(messages, list) or not messages or len(messages) > 100
                    or any(not isinstance(item, dict) or item.get("role") not in ("user", "assistant")
                           or not isinstance(item.get("content"), str) or len(item["content"]) > 8000
                           for item in messages)
                    or not any(item["role"] == "user" and item["content"].strip() for item in messages)):
                return self._err("messages must contain user/assistant text of at most 8000 characters each.", 400)
            profile = self._profile(ctx)
            terria_type = profile.get("terria_type") or "csv"

            current_spec, draft_warnings = self._resolve_incoming_spec(
                body.get("draft_spec"), profile, ctx["view"], terria_type
            )

            candidate = self.llm_service.complete_chat(
                messages=messages,
                profile=profile,
                current_spec=current_spec,
                terria_type=terria_type,
            )
            next_spec, render_warnings = self._materialize_candidate(
                candidate, profile, terria_type, messages, current_spec
            )

            init_source, encoded = self._render(ctx, next_spec, profile, terria_type)
            return self._json(
                {
                    "assistant_message": candidate["assistant_message"],
                    "action": candidate["action"],
                    "next_spec": next_spec,
                    "init_source": init_source,
                    "encoded_config": encoded,
                    "summary": tb.summarize_style_spec(next_spec),
                    "warnings": draft_warnings + render_warnings + candidate.get("warnings", []),
                    "suggested_prompts": candidate.get("suggested_prompts", []),
                }
            )
        except toolkit.NotAuthorized:
            return self._err("Not authorized to access this resource.", 403)
        except toolkit.ObjectNotFound:
            return self._err("Resource view not found.", 404)
        except LlmConfigurationError as exc:
            return self._err(str(exc), 503)
        except LlmResponseError as exc:
            return self._err(str(exc), 502)
        except LlmServiceError:
            return self._err("The AI service request failed.", 502)
        except (DataTooLargeError, DataParseError, DataServiceError) as exc:
            return self._err(str(exc), 422)
        except Exception:
            return self._err("Unexpected error while updating the map.", 500)

    def save(self, view_id: str) -> Response:
        try:
            ctx = self._load_view_context(view_id)
            if not ctx["can_save"]:
                return self._err("Not authorized to save this map view.", 403)
            profile = self._profile(ctx)
            terria_type = profile.get("terria_type") or "csv"

            body = request.get_json(silent=True)
            try:
                spec = tb.validate_style_spec(body.get("draft_spec"), profile, terria_type)
            except tb.OverrideValidationError as exc:
                return self._err("The map styling is not valid: {0}".format(exc), 422)

            serialized = json.dumps(spec, ensure_ascii=False)
            if len(serialized.encode("utf-8")) > self.settings.max_config_bytes:
                return self._err("The map configuration is too large to save.", 413)

            username = self._current_username()
            self._persist_view(
                ctx["view"],
                {
                    "terria_state_json": serialized,
                    "terria_state_version": "1",
                    "terria_summary": tb.summarize_style_spec(spec),
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                    "saved_by": username,
                },
                ctx["user_context"],
            )
            init_source, encoded = self._render(ctx, spec, profile, terria_type)
            return self._json(
                {
                    "saved_spec": spec,
                    "init_source": init_source,
                    "encoded_config": encoded,
                    "summary": tb.summarize_style_spec(spec),
                    "saved_by": username,
                }
            )
        except toolkit.NotAuthorized:
            return self._err("Not authorized to save this map view.", 403)
        except toolkit.ObjectNotFound:
            return self._err("Resource view not found.", 404)
        except (DataTooLargeError, DataParseError, DataServiceError) as exc:
            return self._err(str(exc), 422)
        except Exception:
            return self._err("Unexpected error while saving the map.", 500)

    def resource_content(self, resource_id: str, filename: str | None = None) -> Response:
        token = request.args.get("token", "")
        if not resource_utils.verify_resource_token(resource_id, token):
            return self._err("Invalid or expired token.", 403)
        resolved = resource_utils.resolve_private_resource_source(resource_id, self.settings)
        if not resolved:
            return self._err("Resource not found.", 404)
        upstream_url, content_type, name = resolved
        stream = open_public_resource(upstream_url, timeout_seconds=60)
        try:
            upstream, _ = stream.__enter__()
        except (ValueError, OSError, urllib3.exceptions.HTTPError):
            return self._err("Could not fetch the resource.", 502)

        resp_content_type = (
            upstream.headers.get("Content-Type") or content_type or "application/octet-stream"
        )
        headers = {
            "Content-Type": resp_content_type,
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "private, max-age=300",
        }
        download_name = filename or name
        if download_name:
            download_name = str(download_name).replace("\r", "").replace("\n", "")
            headers["Content-Disposition"] = dump_options_header("inline", {"filename": download_name})
        if upstream.headers.get("Content-Length"):
            headers["Content-Length"] = upstream.headers["Content-Length"]
        def chunks():
            try:
                yield from upstream.stream(65536)
            finally:
                stream.__exit__(None, None, None)

        response = Response(
            stream_with_context(chunks()),
            status=200,
            headers=headers,
        )
        response.call_on_close(lambda: stream.__exit__(None, None, None))
        return response

    # ---- internals ----------------------------------------------------

    def _render(self, ctx, spec, profile, terria_type) -> tuple[dict[str, Any], str]:
        catalog_item = self._catalog_item(ctx, terria_type)
        built = tb.build_overrides(spec, profile, terria_type)
        init_source = tb.build_init_source(tb.apply_overrides(catalog_item, built), ctx["package"])
        return init_source, tb.encode_init_source(init_source)

    def _profile(self, ctx: dict[str, Any]) -> dict[str, Any]:
        if not resource_utils.user_may_download(ctx["user_context"], ctx["resource"]["id"]):
            raise toolkit.NotAuthorized("Downloading this resource is not permitted")
        return self.data_service.get_profile(ctx["resource"], self._fetch_context())

    def _catalog_item(self, ctx: dict[str, Any], terria_type: str) -> dict[str, Any]:
        resource_url = resource_utils.get_resource_url(
            ctx["resource"], ctx["package"], ctx["user_context"], self.settings
        )
        return tb.build_catalog_item(ctx["resource"], resource_url, terria_type)

    def _materialize_candidate(
        self, candidate, profile, terria_type, messages, current_spec
    ) -> tuple[dict[str, Any] | None, list[str]]:
        if candidate["action"] == "ask_clarification":
            return current_spec, []
        try:
            return tb.validate_style_spec(candidate["style_spec"], profile, terria_type), []
        except tb.OverrideValidationError as exc:
            retry = self.llm_service.complete_chat(
                messages=messages,
                profile=profile,
                current_spec=current_spec,
                terria_type=terria_type,
                validation_feedback=str(exc),
            )
            if retry["action"] != "apply_style":
                return current_spec, ["Couldn't apply the styling: {0}".format(exc)]
            try:
                return tb.validate_style_spec(retry["style_spec"], profile, terria_type), []
            except tb.OverrideValidationError as retry_exc:
                raise LlmResponseError(str(retry_exc)) from retry_exc

    def _resolve_incoming_spec(self, draft, profile, view, terria_type):
        warnings: list[str] = []
        if isinstance(draft, dict):
            try:
                return tb.validate_style_spec(draft, profile, terria_type), warnings
            except tb.OverrideValidationError as exc:
                warnings.append("The local draft was discarded: {0}".format(exc))
        saved_spec, saved_warnings = self._effective_saved_spec(view, profile, terria_type)
        warnings.extend(saved_warnings)
        if saved_spec:
            return saved_spec, warnings
        return tb.default_style_spec(profile) or None, warnings

    def _effective_saved_spec(self, view, profile, terria_type):
        raw = self._parse_saved_spec(view)
        if raw is None:
            return None, []
        try:
            return tb.validate_style_spec(raw, profile, terria_type), []
        except tb.OverrideValidationError as exc:
            return None, ["The saved map styling is no longer valid: {0}".format(exc)]

    def _parse_saved_spec(self, view: dict[str, Any]) -> dict[str, Any] | None:
        serialized = str(view.get("terria_state_json") or "").strip()
        if not serialized:
            return None
        try:
            parsed = json.loads(serialized)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _persist_view(self, view: dict[str, Any], extra_fields: dict[str, Any], user_context: dict[str, Any]) -> None:
        update_payload = {
            "id": view.get("id"),
            "resource_id": view.get("resource_id"),
            "view_type": view.get("view_type"),
            "title": view.get("title"),
            "description": view.get("description", ""),
        }
        update_payload.update(extra_fields)
        # Recheck authorization in the action, including grants revoked since
        # bootstrap/profile loading, and attribute the write to the editor.
        context: dict[str, Any] = dict(user_context)
        try:
            import ckan.model as model

            context["model"] = model
            context["session"] = model.Session
        except Exception:
            pass
        toolkit.get_action("resource_view_update")(context, update_payload)

    def _load_view_context(self, view_id: str) -> dict[str, Any]:
        user_context = self._user_context()
        view = toolkit.get_action("resource_view_show")(dict(user_context), {"id": view_id})
        if view.get("view_type") != "terriassistant":
            raise toolkit.ObjectNotFound("Map assistant view not found")
        resource = toolkit.get_action("resource_show")(dict(user_context), {"id": view["resource_id"]})
        package = toolkit.get_action("package_show")(dict(user_context), {"id": resource["package_id"]})
        visible = toolkit.get_action("resource_view_list")(dict(user_context), {"id": resource["id"]})
        if not any(item.get("id") == view_id for item in visible):
            raise toolkit.NotAuthorized("The resource view is not available")
        can_save = False
        try:
            toolkit.check_access(
                "resource_view_update",
                dict(user_context),
                {"id": view["id"], "resource_id": resource["id"]},
            )
            can_save = True
        except toolkit.NotAuthorized:
            can_save = False
        return {
            "view": view,
            "resource": resource,
            "package": package,
            "can_save": can_save,
            "user_context": user_context,
        }

    def _fetch_context(self) -> FetchContext:
        return FetchContext(
            site_url=self.settings.site_url,
            authorization=request.headers.get("Authorization", ""),
            cookie_header=request.headers.get("Cookie", ""),
            user_agent=request.headers.get("User-Agent", "ckanext-terriassistant/0.1.0"),
        )

    def _user_context(self) -> dict[str, Any]:
        try:
            current_user = toolkit.current_user
            if not current_user or getattr(current_user, "is_anonymous", True):
                return {"user": "", "auth_user_obj": None}
            user_name = current_user.name if hasattr(current_user, "name") else str(current_user)
            return {"user": user_name, "auth_user_obj": current_user}
        except (AttributeError, RuntimeError):
            return {
                "user": getattr(toolkit.g, "user", ""),
                "auth_user_obj": getattr(toolkit.g, "userobj", None),
            }

    def _can_chat(self, context: dict[str, Any]) -> bool:
        user = context.get("auth_user_obj")
        return bool(context.get("user") and user and not getattr(user, "is_anonymous", True)
                    and getattr(user, "state", "") == "active")

    def _current_username(self) -> str:
        return str(self._user_context().get("user") or "")

    def _json(self, data: dict[str, Any], status_code: int = 200) -> Response:
        return Response(
            json.dumps(data, ensure_ascii=False),
            status=status_code,
            content_type="application/json; charset=utf-8",
        )

    def _err(self, message: str, status_code: int) -> Response:
        return self._json({"success": False, "message": message}, status_code)


@terriassistant_api.before_request
def protect_view_requests():
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        controller = _get_controller()
        if request.content_length and request.content_length > controller.settings.max_config_bytes + 16384:
            return controller._err("The map request is too large.", 413)
        try:
            validate_csrf(request.headers.get("X-CSRFToken"))
        except ValidationError:
            return controller._err("Refresh the map assistant to renew your session.", 403)
        if not isinstance(request.get_json(silent=True), dict):
            return controller._err("Expected a JSON object.", 400)


@terriassistant_api.after_request
def private_view_response(response):
    # Bootstrap contains user capabilities, sampled data and expiring proxy URLs.
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["CDN-Cache-Control"] = "no-store"
    response.headers["Surrogate-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.vary.update(("Cookie", "Authorization"))
    try:
        from ckanext.terria_view.api_endpoints import _disable_ckan_response_cache
        _disable_ckan_response_cache()
    except ImportError:
        pass
    return response


@terriassistant_api.get("/api/terriassistant/view/<view_id>/bootstrap")
def terriassistant_bootstrap(view_id: str) -> Response:
    return _get_controller().bootstrap(view_id)


@terriassistant_api.post("/api/terriassistant/view/<view_id>/chat")
def terriassistant_chat(view_id: str) -> Response:
    return _get_controller().chat(view_id)


@terriassistant_api.post("/api/terriassistant/view/<view_id>/save")
def terriassistant_save(view_id: str) -> Response:
    return _get_controller().save(view_id)


@terriassistant_api.get("/api/terriassistant/resource/<resource_id>/content")
def terriassistant_resource_content(resource_id: str) -> Response:
    return _get_controller().resource_content(resource_id)


@terriassistant_api.get("/api/terriassistant/resource/<resource_id>/content/<path:filename>")
def terriassistant_resource_content_named(resource_id: str, filename: str) -> Response:
    return _get_controller().resource_content(resource_id, filename)
