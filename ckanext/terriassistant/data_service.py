from __future__ import annotations

import csv
import io
import json
import re
import warnings
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

from .config import TerriassistantSettings, _format_supported


class DataServiceError(Exception):
    pass


class DataFetchError(DataServiceError):
    pass


class DataTooLargeError(DataServiceError):
    pass


class DataParseError(DataServiceError):
    pass


_LAT_NAMES = {"lat", "latitude", "y", "ycoord", "lat_dd", "latitud"}
_LON_NAMES = {"lon", "lng", "long", "longitude", "x", "xcoord", "lon_dd", "longitud"}
_REGION_HINTS = (
    "region",
    "postcode",
    "post_code",
    "zip",
    "lga",
    "sa1",
    "sa2",
    "sa3",
    "sa4",
    "state",
    "country",
    "iso",
    "fips",
    "admin",
    "municip",
    "comuna",
    "provinc",
    "depart",
)


@dataclass(frozen=True)
class FetchContext:
    site_url: str = ""
    authorization: str = ""
    cookie_header: str = ""
    user_agent: str = "ckanext-terriassistant/0.1.0"
    timeout_seconds: int = 30


class DataService:
    """Downloads a CKAN resource and builds a lightweight profile for the LLM."""

    def __init__(self, settings: TerriassistantSettings):
        self.settings = settings
        self._cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._cache.clear()

    # ---- public API ------------------------------------------------------

    def get_profile(self, resource: dict[str, Any], fetch_context: FetchContext) -> dict[str, Any]:
        kind = self._resource_kind(resource)
        resource_id = str(resource.get("id") or "")
        version = str(resource.get("last_modified") or resource.get("metadata_modified") or "")
        url = str(resource.get("url") or "")
        cache_key = (resource_id, version, url)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        if kind == "csv":
            profile = self._profile_csv(resource, fetch_context)
        elif kind == "geojson":
            profile = self._profile_geojson(resource, fetch_context)
        else:
            profile = self._profile_opaque(resource, kind)

        self._cache[cache_key] = profile
        return profile

    def resource_kind(self, resource: dict[str, Any]) -> str:
        return self._resource_kind(resource)

    # ---- kind detection --------------------------------------------------

    def _resource_kind(self, resource: dict[str, Any]) -> str:
        fmt = str(resource.get("format") or "").strip().lower()
        url = str(resource.get("url") or "").strip().lower()
        path = urlparse(url).path
        if fmt == "csv" or fmt.startswith("csv-geo") or path.endswith(".csv"):
            return "csv"
        if fmt == "geojson" or path.endswith(".geojson"):
            return "geojson"
        if fmt == "json" or path.endswith(".json"):
            # treat .json as geojson if we can; profiling will fall back to opaque
            return "geojson"
        if fmt in ("shp",) or path.endswith(".zip"):
            return "shp"
        if fmt in ("kml", "kmz") or path.endswith(".kml") or path.endswith(".kmz"):
            return "kml"
        if fmt == "czml" or path.endswith(".czml"):
            return "czml"
        if fmt in ("tif", "tiff", "geotiff", "cog") or path.endswith((".tif", ".tiff")):
            return "cog"
        if fmt in ("wms",):
            return "wms"
        if fmt in ("wfs",):
            return "wfs"
        if fmt in ("wmts",):
            return "wmts"
        if fmt == "esri rest":
            return "esri"
        if _format_supported(fmt):
            return fmt
        return "opaque"

    # ---- downloading -----------------------------------------------------

    def _download_bytes(
        self, resource: dict[str, Any], fetch_context: FetchContext
    ) -> tuple[bytes, str, requests.Response]:
        resource_url = str(resource.get("url") or "").strip()
        if not resource_url:
            raise DataFetchError("The CKAN resource does not have a URL.")
        if resource_url.startswith("/"):
            if not fetch_context.site_url:
                raise DataFetchError("Cannot resolve relative resource URLs without ckan.site_url.")
            resource_url = urljoin(fetch_context.site_url + "/", resource_url.lstrip("/"))

        headers = {"User-Agent": fetch_context.user_agent, "Accept": "*/*"}
        if fetch_context.authorization:
            headers["Authorization"] = fetch_context.authorization
        if fetch_context.cookie_header:
            headers["Cookie"] = fetch_context.cookie_header

        try:
            response = requests.get(
                resource_url,
                headers=headers,
                stream=True,
                timeout=(5, fetch_context.timeout_seconds),
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DataFetchError("Could not fetch the resource.") from exc

        collected = bytearray()
        limit = self.settings.max_resource_bytes
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            collected.extend(chunk)
            if len(collected) > limit:
                raise DataTooLargeError(
                    "The resource exceeds the configured byte limit of {0} bytes.".format(limit)
                )
        return bytes(collected), response.url, response

    # ---- CSV -------------------------------------------------------------

    def _profile_csv(
        self, resource: dict[str, Any], fetch_context: FetchContext
    ) -> dict[str, Any]:
        raw, _, response = self._download_bytes(resource, fetch_context)
        encoding = response.encoding or "utf-8-sig"
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            text = raw.decode("utf-8-sig", errors="replace")

        delimiter = self._detect_delimiter(text, resource)
        try:
            frame = pd.read_csv(io.StringIO(text), sep=delimiter, low_memory=False)
        except Exception as exc:
            raise DataParseError("The CSV could not be parsed.") from exc
        if frame.empty and list(frame.columns) == []:
            raise DataParseError("The CSV does not contain tabular data.")
        if len(frame.index) > self.settings.max_rows:
            raise DataTooLargeError(
                "The CSV exceeds the configured row limit of {0}.".format(self.settings.max_rows)
            )
        frame = frame.rename(columns=lambda v: str(v).strip())

        profile_frame = frame.head(self.settings.profile_rows).copy()
        columns: list[dict[str, Any]] = []
        numeric_columns: list[str] = []
        categorical_columns: list[str] = []
        datetime_columns: list[str] = []
        coerced_numeric: dict[str, pd.Series] = {}

        for column in frame.columns:
            series = profile_frame[column]
            kind, coerced = self._infer_series_kind(series)
            entry: dict[str, Any] = {
                "name": str(column),
                "kind": kind,
                "null_count": int(series.isna().sum()),
                "unique_count": int(series.nunique(dropna=True)),
                "sample_values": [self._json_value(v) for v in series.dropna().head(5).tolist()],
            }
            if kind == "numeric":
                numeric_columns.append(str(column))
                coerced_numeric[str(column)] = coerced
                entry["min"] = self._json_value(coerced.min(skipna=True))
                entry["max"] = self._json_value(coerced.max(skipna=True))
                entry["mean"] = self._json_value(coerced.mean(skipna=True))
            elif kind == "datetime":
                datetime_columns.append(str(column))
                entry["min"] = self._json_value(coerced.min(skipna=True))
                entry["max"] = self._json_value(coerced.max(skipna=True))
            else:
                categorical_columns.append(str(column))
                value_counts = series.fillna("(blank)").astype(str).value_counts().head(8)
                entry["top_values"] = [
                    {"value": self._json_value(idx), "count": int(cnt)}
                    for idx, cnt in value_counts.items()
                ]
            columns.append(entry)

        lat_col, lon_col = self._detect_lat_lon(frame.columns, numeric_columns, coerced_numeric)
        region_cols = self._detect_region_columns(frame.columns)
        if lat_col and lon_col:
            geometry = "points"
        elif region_cols:
            geometry = "region"
        else:
            geometry = "unknown"

        return {
            "kind": "csv",
            "terria_type": "csv",
            "resource_id": resource.get("id"),
            "resource_name": resource.get("name") or resource.get("id") or "CSV resource",
            "format": resource.get("format"),
            "row_count": int(len(frame.index)),
            "column_count": int(len(frame.columns)),
            "delimiter": delimiter,
            "columns": columns,
            "numeric_columns": numeric_columns,
            "categorical_columns": categorical_columns,
            "datetime_columns": datetime_columns,
            "latitude_column": lat_col,
            "longitude_column": lon_col,
            "region_columns": region_cols,
            "geometry": geometry,
            "sample_rows": self._frame_to_rows(frame.head(self.settings.sample_rows)),
            "suggested_prompts": self._suggest_prompts_csv(
                numeric_columns, categorical_columns, geometry
            ),
        }

    def _detect_delimiter(self, text: str, resource: dict[str, Any]) -> str:
        fmt = str(resource.get("format") or "").lower()
        if fmt == "tsv":
            return "\t"
        sample = text[:4096]
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            return ","

    def _infer_series_kind(self, series: pd.Series) -> tuple[str, pd.Series]:
        cleaned = series.dropna()
        if cleaned.empty:
            return "categorical", cleaned
        numeric_candidate = pd.to_numeric(
            cleaned.astype(str).str.replace(",", "", regex=False), errors="coerce"
        )
        if len(numeric_candidate.index) > 0:
            ratio = float(numeric_candidate.notna().sum()) / float(len(numeric_candidate.index))
            if ratio >= 0.8:
                return "numeric", numeric_candidate
        text_sample = " ".join(cleaned.astype(str).head(10).tolist())
        looks_like_date = any(m in text_sample for m in ("-", "/", ":", "T"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            datetime_candidate = pd.to_datetime(cleaned, errors="coerce", utc=False)
        if looks_like_date and len(datetime_candidate.index) > 0:
            ratio = float(datetime_candidate.notna().sum()) / float(len(datetime_candidate.index))
            if ratio >= 0.8:
                return "datetime", datetime_candidate
        return "categorical", cleaned.astype(str)

    def _detect_lat_lon(self, columns, numeric_columns, coerced_numeric):
        lat_col = lon_col = None
        for column in columns:
            key = re.sub(r"[^a-z]", "", str(column).lower())
            if lat_col is None and key in _LAT_NAMES and str(column) in numeric_columns:
                lat_col = str(column)
            if lon_col is None and key in _LON_NAMES and str(column) in numeric_columns:
                lon_col = str(column)
        # range-based fallback
        if lat_col is None or lon_col is None:
            for column, coerced in coerced_numeric.items():
                cmin = coerced.min(skipna=True)
                cmax = coerced.max(skipna=True)
                if pd.isna(cmin) or pd.isna(cmax):
                    continue
                if lat_col is None and -90 <= cmin and cmax <= 90 and "lon" not in column.lower():
                    if "lat" in column.lower():
                        lat_col = column
                if lon_col is None and -180 <= cmin and cmax <= 180 and "lat" not in column.lower():
                    if "lon" in column.lower() or "lng" in column.lower():
                        lon_col = column
        return lat_col, lon_col

    def _detect_region_columns(self, columns):
        result = []
        for column in columns:
            low = str(column).lower()
            if any(hint in low for hint in _REGION_HINTS):
                result.append(str(column))
        return result

    def _suggest_prompts_csv(self, numeric_columns, categorical_columns, geometry):
        out = []
        if geometry == "points" and numeric_columns:
            out.append("Color the points by {0} using a continuous red palette.".format(numeric_columns[0]))
            out.append("Make the markers bigger and add a thin black outline.")
        elif geometry == "region" and numeric_columns:
            out.append("Shade the regions by {0} with 5 bins.".format(numeric_columns[0]))
        if categorical_columns and not numeric_columns:
            out.append("Color the features by {0} with distinct colors per category.".format(categorical_columns[0]))
        out.append("Set the layer opacity to 0.7.")
        return out[:4]

    # ---- GeoJSON ---------------------------------------------------------

    def _profile_geojson(
        self, resource: dict[str, Any], fetch_context: FetchContext
    ) -> dict[str, Any]:
        raw, _, response = self._download_bytes(resource, fetch_context)
        encoding = response.encoding or "utf-8"
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except ValueError:
            # Not actually GeoJSON / JSON we can parse — treat as opaque.
            return self._profile_opaque(resource, "geojson")

        features = []
        if isinstance(data, dict):
            if data.get("type") == "FeatureCollection":
                features = data.get("features") or []
            elif data.get("type") == "Feature":
                features = [data]
        if not isinstance(features, list):
            features = []

        property_names: list[str] = []
        property_kinds: dict[str, set] = {}
        geometry_types: dict[str, int] = {}
        sample_features = []
        for feature in features[: self.settings.sample_features]:
            if not isinstance(feature, dict):
                continue
            geom = feature.get("geometry") or {}
            gtype = geom.get("type") if isinstance(geom, dict) else None
            if gtype:
                geometry_types[gtype] = geometry_types.get(gtype, 0) + 1
            props = feature.get("properties") or {}
            if isinstance(props, dict):
                for k, v in props.items():
                    if k not in property_names:
                        property_names.append(k)
                    property_kinds.setdefault(k, set()).add(
                        "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else "string"
                    )
                sample_features.append(props)
        dominant_geometry = max(geometry_types, key=geometry_types.get) if geometry_types else None

        return {
            "kind": "geojson",
            "terria_type": "geojson",
            "resource_id": resource.get("id"),
            "resource_name": resource.get("name") or resource.get("id") or "GeoJSON resource",
            "format": resource.get("format"),
            "feature_count": len(features),
            "geometry_type": dominant_geometry,
            "property_columns": [
                {"name": name, "kind": "number" if property_kinds.get(name) == {"number"} else "string"}
                for name in property_names
            ],
            "sample_features": sample_features,
            "suggested_prompts": self._suggest_prompts_geojson(dominant_geometry),
        }

    def _suggest_prompts_geojson(self, geometry_type):
        out = []
        gt = (geometry_type or "").lower()
        if "polygon" in gt:
            out.append("Fill the polygons green with 50% opacity and a black 2px outline.")
            out.append("Make the polygon fill blue and the outline thicker.")
        elif "line" in gt:
            out.append("Make the lines red and 3px wide.")
        elif "point" in gt:
            out.append("Make the markers large and red.")
        else:
            out.append("Style the features with a blue fill and dark outline.")
        out.append("Set the layer opacity to 0.7.")
        return out[:4]

    # ---- opaque ----------------------------------------------------------

    def _profile_opaque(self, resource: dict[str, Any], kind: str) -> dict[str, Any]:
        return {
            "kind": "opaque",
            "terria_type": kind,
            "resource_id": resource.get("id"),
            "resource_name": resource.get("name") or resource.get("id") or "resource",
            "format": resource.get("format"),
            "note": (
                "This format ({0}) is rendered directly by TerriaJS; column-level "
                "styling is not available. You can still adjust the layer opacity."
            ).format(resource.get("format") or kind),
            "suggested_prompts": ["Set the layer opacity to 0.6.", "Set the layer opacity to 1."],
        }

    # ---- helpers ---------------------------------------------------------

    def _frame_to_rows(self, frame: pd.DataFrame) -> list[dict[str, Any]]:
        return [
            {str(col): self._json_value(val) for col, val in row.items()}
            for _, row in frame.iterrows()
        ]

    def _json_value(self, value: Any) -> Any:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                return str(value)
        return value
