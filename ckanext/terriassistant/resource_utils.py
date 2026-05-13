from __future__ import annotations

import hashlib
import hmac
import os.path
import time
import urllib.parse
from typing import Any, Optional

import ckan.plugins.toolkit as toolkit

from .config import TerriassistantSettings


def _token_secret() -> bytes:
    for key in ("beaker.session.secret", "SECRET_KEY", "flask.secret_key"):
        value = toolkit.config.get(key) if toolkit.config else None
        if value:
            return str(value).encode("utf-8")
    return b"ckanext-terriassistant-proxy-fallback"


def generate_resource_token(resource_id: str, ttl_seconds: int) -> str:
    if not resource_id:
        raise ValueError("resource_id is required to generate a token")
    expiry = int(time.time()) + max(60, int(ttl_seconds))
    payload = f"{resource_id}|{expiry}".encode("utf-8")
    signature = hmac.new(_token_secret(), payload, hashlib.sha256).hexdigest()
    return f"{expiry}.{signature}"


def verify_resource_token(resource_id: str, token: str) -> bool:
    if not resource_id or not token or "." not in token:
        return False
    expiry_str, _, signature = token.partition(".")
    if not signature:
        return False
    try:
        expiry = int(expiry_str)
    except (TypeError, ValueError):
        return False
    if expiry < int(time.time()):
        return False
    payload = f"{resource_id}|{expiry}".encode("utf-8")
    expected = hmac.new(_token_secret(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def to_absolute_url(url: str, site_url: str) -> str:
    if not url:
        return url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme and parsed.netloc:
        return url
    site_url = (site_url or toolkit.config.get("ckan.site_url", "") or "").rstrip("/")
    if not site_url:
        return url
    if url.startswith("//"):
        scheme = urllib.parse.urlparse(site_url + "/").scheme or "https"
        return f"{scheme}:{url}"
    return urllib.parse.urljoin(site_url + "/", url.lstrip("/"))


def extract_upload_filename(resource: dict, resource_url: str) -> str:
    candidates = []
    raw = resource.get("url")
    if isinstance(raw, str) and raw:
        candidates.append(raw)
    if isinstance(resource_url, str) and resource_url:
        candidates.append(resource_url)
    for candidate in candidates:
        parsed = urllib.parse.urlparse(candidate)
        path = parsed.path or candidate
        if "/download/" in path:
            filename = path.rsplit("/download/", 1)[-1]
        else:
            filename = os.path.basename(path.rstrip("/"))
        filename = urllib.parse.unquote(filename or "")
        if filename:
            return filename
    return ""


def build_proxy_resource_url(
    resource_id: str, settings: TerriassistantSettings, filename: Optional[str] = None
) -> str:
    site_url = (settings.site_url or toolkit.config.get("ckan.site_url", "") or "").rstrip("/")
    token = generate_resource_token(resource_id, settings.proxy_token_ttl)
    path = f"/api/terriassistant/resource/{resource_id}/content"
    if filename:
        safe_filename = urllib.parse.quote(filename, safe=".-_")
        if safe_filename:
            path = f"{path}/{safe_filename}"
    return f"{site_url}{path}?token={token}"


def get_resource_url(
    resource: dict, package: dict, user_context: dict, settings: TerriassistantSettings
) -> str:
    """Return the URL Terria should use to load this resource.

    Public resources: their absolute URL. Private uploaded resources: the CKAN
    proxy URL (signed token) so the cross-origin Terria iframe can load them
    without depending on the storage backend's CORS configuration.
    """
    resource_url = resource.get("url") or ""
    is_private = package.get("private") is True
    is_logged = bool(user_context.get("user"))
    is_upload = resource.get("url_type") == "upload" or resource_url.startswith("/")

    if is_private and is_logged and is_upload and resource.get("id"):
        try:
            filename = extract_upload_filename(resource, resource_url)
            return build_proxy_resource_url(resource["id"], settings, filename or None)
        except Exception:
            pass

    if is_private and is_logged and is_upload:
        try:
            from ckan.lib import uploader

            upload = uploader.get_resource_uploader(resource)
            filename = extract_upload_filename(resource, resource_url)
            resolved = upload.get_url_from_filename(
                resource["id"], filename or resource_url, content_type=resource.get("mimetype")
            )
            if resolved:
                return to_absolute_url(resolved, settings.site_url)
        except Exception:
            pass

    return to_absolute_url(resource_url, settings.site_url)


def resolve_private_resource_source(resource_id: str, settings: TerriassistantSettings):
    """Resolve ``(url, content_type, filename)`` for streaming a private resource.

    Runs with ``ignore_auth`` — the caller must have validated the signed token.
    """
    if not resource_id:
        return None
    try:
        import ckan.model as model

        lookup_context: dict[str, Any] = {
            "model": model,
            "session": model.Session,
            "user": "ckan.system",
            "ignore_auth": True,
        }
    except Exception:
        lookup_context = {"ignore_auth": True}

    try:
        resource = toolkit.get_action("resource_show")(lookup_context, {"id": resource_id})
    except Exception:
        return None

    raw_url = resource.get("url") or ""
    filename = extract_upload_filename(resource, raw_url)
    if not filename and raw_url:
        filename = os.path.basename(urllib.parse.urlparse(raw_url).path or "")
    content_type = resource.get("mimetype")

    if resource.get("url_type") == "upload" or raw_url.startswith("/"):
        if not filename:
            return None
        try:
            from ckan.lib import uploader

            upload = uploader.get_resource_uploader(resource)
            resolved = upload.get_url_from_filename(
                resource["id"], filename, content_type=content_type
            )
            if resolved:
                return resolved, content_type, filename
        except Exception:
            pass

    if raw_url:
        return to_absolute_url(raw_url, settings.site_url), content_type, filename or None
    return None
