from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Resource formats Terria can render (mirrors ckanext-terria_view).
SUPPORTED_FORMATS = (
    "csv",
    "csv-geo-*",
    "geojson",
    "shp",
    "kml",
    "kmz",
    "czml",
    "wms",
    "wfs",
    "wmts",
    "esri rest",
    "tif",
    "tiff",
    "geotiff",
    "cog",
)


def _format_supported(format_value: str) -> bool:
    format_value = (format_value or "").strip().lower()
    if not format_value:
        return False
    for pattern in SUPPORTED_FORMATS:
        if "*" in pattern:
            if fnmatch.fnmatch(format_value, pattern):
                return True
        elif format_value == pattern:
            return True
    return False


@dataclass(frozen=True)
class TerriassistantSettings:
    plugin_name: str = "terriassistant"
    default_title: str = "Map Assistant"
    site_url: str = ""
    terria_instance_url: str = "https://ihp-wins.unesco.org/terria/"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_thinking: bool = False
    max_resource_bytes: int = 25 * 1024 * 1024
    max_rows: int = 150000
    profile_rows: int = 5000
    sample_rows: int = 20
    sample_features: int = 20
    prompt_history_messages: int = 10
    max_config_bytes: int = 4 * 1024 * 1024
    proxy_token_ttl: int = 3600
    json_repair_attempts: int = 2

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "TerriassistantSettings":
        p = "ckanext.terriassistant."
        terria_url = str(
            config.get(p + "terria_instance_url", cls.terria_instance_url)
        ).strip()
        if terria_url and not terria_url.endswith("/"):
            terria_url += "/"
        return cls(
            default_title=str(config.get(p + "default_title", cls.default_title)),
            site_url=str(config.get("ckan.site_url", "")).rstrip("/"),
            terria_instance_url=terria_url,
            deepseek_api_key=str(config.get(p + "deepseek_api_key", "")).strip(),
            deepseek_base_url=str(
                config.get(p + "deepseek_base_url", cls.deepseek_base_url)
            ).rstrip("/"),
            deepseek_model=str(config.get(p + "deepseek_model", cls.deepseek_model)).strip(),
            deepseek_thinking=_as_bool(config.get(p + "deepseek_thinking"), cls.deepseek_thinking),
            max_resource_bytes=_as_int(config.get(p + "max_resource_bytes"), cls.max_resource_bytes),
            max_rows=_as_int(config.get(p + "max_rows"), cls.max_rows),
            profile_rows=_as_int(config.get(p + "profile_rows"), cls.profile_rows),
            sample_rows=_as_int(config.get(p + "sample_rows"), cls.sample_rows),
            sample_features=_as_int(config.get(p + "sample_features"), cls.sample_features),
            prompt_history_messages=_as_int(
                config.get(p + "prompt_history_messages"), cls.prompt_history_messages
            ),
            max_config_bytes=_as_int(config.get(p + "max_config_bytes"), cls.max_config_bytes),
            proxy_token_ttl=_as_int(config.get(p + "proxy_token_ttl"), cls.proxy_token_ttl),
            json_repair_attempts=_as_int(
                config.get(p + "json_repair_attempts"), cls.json_repair_attempts
            ),
        )

    def can_view_resource(self, resource: Mapping[str, Any] | None) -> bool:
        resource = resource or {}
        if _format_supported(str(resource.get("format") or "")):
            return True
        url = str(resource.get("url") or "").strip().lower()
        path = urlparse(url).path if url else ""
        return any(
            path.endswith(ext)
            for ext in (
                ".csv",
                ".geojson",
                ".json",
                ".kml",
                ".kmz",
                ".czml",
                ".zip",
                ".tif",
                ".tiff",
            )
        )

    def schema_info(self, toolkit: Any) -> dict[str, list[Any]]:
        ignore_missing = toolkit.get_validator("ignore_missing")
        return {
            "terria_state_json": [ignore_missing],
            "terria_state_version": [ignore_missing],
            "terria_summary": [ignore_missing],
            "saved_at": [ignore_missing],
            "saved_by": [ignore_missing],
            "__extras": [ignore_missing],
        }
