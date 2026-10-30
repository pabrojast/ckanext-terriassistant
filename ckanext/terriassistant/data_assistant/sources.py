"""Authorized CKAN reads and bounded downloads; no browser credentials forwarded."""
import csv
import io
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlsplit

import pandas as pd
import urllib3

from .calculations import clean, profile


def context_for(user_id):
    from ckan import model
    from ckan.plugins import toolkit as tk
    user = model.User.get(user_id)
    if user is None or user.state != "active":
        raise tk.NotAuthorized("An active IHP account is required")
    return {"user": user.name, "auth_user_obj": user, "model": model, "session": model.Session}


def resource_for(user_id, resource_id, download=False):
    from ckan.plugins import toolkit as tk
    from ckanext.terria_view.resource_utils import ResourceUtils
    context = context_for(user_id)
    resource = tk.get_action("resource_show")(context, {"id": resource_id})
    package = tk.get_action("package_show")(context, {"id": resource["package_id"]})
    if download and not ResourceUtils.user_may_download(context, resource_id):
        raise tk.NotAuthorized("Download permission is required to analyze these data")
    return resource, package


def descriptor(resource, package):
    return {"id": "ckan:" + resource["id"], "resource_id": resource["id"],
            "dataset_id": package["id"], "name": str(resource.get("name") or resource["id"])[:200],
            "format": str(resource.get("format") or "")[:50],
            "dataset_name": str(package.get("name") or package["id"])[:200],
            "dataset_title": str(package.get("title") or package.get("name") or package["id"])[:200],
            "url": "/dataset/" + package["id"] + "/resource/" + resource["id"],
            "private": bool(package.get("private")), "modified": resource.get("last_modified"),
            "units": None}


def search(user_id, query, limit=10, polygon=None, formats=None, organization=None, tags=None, cursor=None):
    from ckan.plugins import toolkit as tk
    from .spatial import geometry, coverage, bbox_string
    context = context_for(user_id)
    area = geometry(polygon) if polygon else None
    start, resource_offset = 0, 0
    if cursor:
        if not re.fullmatch(r"\d{1,7}:\d{1,6}", cursor):
            raise ValueError("Invalid search continuation")
        start, resource_offset = map(int, cursor.split(":"))
    result = None
    # Solr's partial-match analyzer splits hyphenated slugs. Resolve exact IDs/names
    # through the authorized action first so a precise request is not diluted.
    if not cursor and re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,99}", query.strip()):
        try:
            package = tk.get_action("package_show")(context, {"id": query.strip()})
            # Do not let exact lookups bypass explicit catalog filters.
            if not organization and not tags and not formats:
                result = {"count": 1, "results": [package]}
        except (tk.ObjectNotFound, tk.NotAuthorized):
            pass
    filters = []
    if organization:
        filters.append("organization:" + json.dumps(organization))
    for tag in tags or []:
        filters.append("tags:" + json.dumps(tag))
    if formats:
        filters.append("res_format:(" + " OR ".join(json.dumps(f) for f in formats) + ")")
    params = {"q": query or "*:*", "rows": min(limit, 10), "start": start,
              "include_private": True, "sort": "score desc, id asc"}
    if filters:
        params["fq"] = " ".join(filters)
    if area is not None:
        params["extras"] = {"ext_bbox": bbox_string(area)}
    sources, examined, matched, unknown = [], 0, 0, 0
    next_cursor = None
    count = 0
    exact = result is not None
    while examined < 100:
        if result is None:
            params["start"] = start
            result = tk.get_action("package_search")(context, params)
        count = result["count"]
        packages = result["results"]
        if not packages:
            break
        for index, package in enumerate(packages):
            position = start + index
            examined += 1
            # CKAN search hooks can omit private resource lists from the indexed
            # representation. Recover them only through the caller's authorized
            # package action, never by trusting Solr visibility as download access.
            if not package.get("resources"):
                try:
                    package = tk.get_action("package_show")(context, {"id": package["id"]})
                except (tk.NotAuthorized, tk.ObjectNotFound):
                    resource_offset = 0
                    continue
            if area is not None:
                extent = coverage(package)
                if extent is None:
                    unknown += 1
                    resource_offset = 0
                    continue
                if not area.intersects(extent):
                    resource_offset = 0
                    continue
            matched += 1
            resources = package.get("resources", [])
            for ri in range(resource_offset, len(resources)):
                resource = resources[ri]
                if formats and str(resource.get("format", "")).lower() not in {f.lower() for f in formats}:
                    continue
                if len(sources) >= 20:
                    next_cursor = f"{position}:{ri}"
                    break
                try:
                    verified, parent = resource_for(user_id, resource["id"])
                    sources.append(descriptor(verified, parent))
                except (tk.NotAuthorized, tk.ObjectNotFound):
                    continue
            resource_offset = 0
            if next_cursor:
                break
            if (len(sources) >= 20 or examined >= 100) and position + 1 < count:
                next_cursor = f"{position + 1}:0"
                break
        if next_cursor or exact:
            break
        start += len(packages)
        if start >= count:
            break
        if sources or area is None:
            next_cursor = f"{start}:0"
            break
        result = None
    return {"datasets_found": count if area is None else matched, "candidates_found": count,
            "sources": sources, "truncated": bool(next_cursor), "next_cursor": next_cursor,
            "examined": examined, "unknown_coverage": unknown,
            "spatial_basis": "declared_dataset_coverage" if area is not None else None}


def map_source(user_id, resource_id, view_id=None, include_config=False):
    """Resolve a permitted resource through terria_view, preserving its saved style."""
    from ckan.plugins import toolkit as tk
    from ckanext.terria_view.terria_json_generator import TerriaJSONGenerator
    from ckanext.terria_view.private_catalog import is_non_public_dataset
    from ckanext.terria_view.terria_config_builder import strip_proxy_tokens
    from ..config import _format_supported
    resource, package = resource_for(user_id, resource_id, download=include_config)
    if not _format_supported(resource.get("format", "")):
        raise ValueError("This resource format is not supported by the map")
    context = context_for(user_id)
    views = [v for v in tk.get_action("resource_view_list")(context, {"id": resource_id})
             if v.get("view_type") == "terria_view"]
    selected = 0
    if view_id:
        selected = next((i for i, view in enumerate(views) if view["id"] == view_id), -1)
        if selected < 0:
            raise ValueError("The selected view does not belong to this resource")
    result = {"source": descriptor(resource, package), "views": [
        {"id": v["id"], "title": str(v.get("title", ""))[:200]} for v in views
    ]}
    item, _ = TerriaJSONGenerator().format_dataset_item(
        resource, package["id"], package.get("notes", ""),
        {"display_name": (package.get("organization") or {}).get("title", ""),
         "description": "", "image_display_url": ""},
        selected, package=package, user_context=context, terria_views=views, relative_urls=True)
    if not item:
        raise ValueError("This resource has no supported map representation")
    if include_config:
        # Always use CKAN's permission-checked content proxy for restricted data.
        strip_proxy_tokens(item)
        if is_non_public_dataset(package):
            item["shareable"] = False
        result["model"] = item
    else:
        from .map_tools import TRAITS
        def safe(value):
            if isinstance(value, dict):
                return {k: safe(v) for k, v in value.items() if not re.search(r"url|token|key|html|template", k, re.I)}
            if isinstance(value, list):
                return [safe(v) for v in value[:100]]
            if isinstance(value, str):
                return "[external reference]" if "://" in value else value[:500]
            return value
        result["example"] = safe({k: item[k] for k in TRAITS if k in item})
        result["type"] = item.get("type")
    return result


def public_addresses(host, port):
    addresses = sorted({record[4][0] for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
        raise ValueError("The resource does not have a public download address")
    return addresses


def download(url, maximum):
    """Pin DNS for each redirect; reject loopback, private networks and URL credentials."""
    for _ in range(5):
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Unsupported resource address")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        address = public_addresses(parsed.hostname, port)[0]
        kwargs = dict(port=port, timeout=urllib3.Timeout(connect=5, read=30), maxsize=1)
        if parsed.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(address, assert_hostname=parsed.hostname,
                                              server_hostname=parsed.hostname, **kwargs)
        else:
            pool = urllib3.HTTPConnectionPool(address, **kwargs)
        response = None
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            response = pool.request("GET", target, headers={"Host": parsed.netloc,
                                    "Accept-Encoding": "identity"}, preload_content=False,
                                    redirect=False, retries=False)
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status != 200:
                raise ValueError("The resource download failed")
            if int(response.headers.get("Content-Length", "0")) > maximum:
                raise ValueError("Resource exceeds the download limit")
            raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError("Resource exceeds the download limit")
            return raw
        finally:
            if response is not None:
                response.close()
            pool.close()
    raise ValueError("Too many resource redirects")


def load_frame(user_id, resource_id, settings):
    from ckan.plugins import toolkit as tk
    from ckan.lib import uploader
    resource, package = resource_for(user_id, resource_id, download=True)
    if resource.get("datastore_active"):
        records, offset, size, byte_count = [], 0, 5000, 0
        while True:
            resource_for(user_id, resource_id, download=True)
            page = tk.get_action("datastore_search")(context_for(user_id),
                {"resource_id": resource_id, "limit": size, "offset": offset})
            if page.get("total", 0) > settings.max_rows:
                raise ValueError("Dataset exceeds the analysis row limit; use a smaller resource")
            batch = page.get("records", [])
            byte_count += len(json.dumps(batch, default=str).encode("utf-8"))
            if byte_count > settings.max_bytes:
                raise ValueError("Dataset exceeds the analysis byte limit; use a smaller resource")
            records.extend(batch)
            if len(records) > settings.max_rows:
                raise ValueError("Dataset exceeds the analysis row limit")
            if len(batch) < size:
                break
            offset += size
        return pd.DataFrame(records)
    fmt = str(resource.get("format") or "").lower()
    if fmt not in ("csv", "geojson", "json") and not fmt.startswith("csv-geo"):
        raise ValueError("This format requires a loaded Terria layer; add it to the map first")
    url = str(resource.get("url") or "")
    raw = None
    if resource.get("url_type") == "upload":
        upload = uploader.get_resource_uploader(resource)
        if hasattr(upload, "get_url_from_filename"):
            from ckanext.terria_view.config_manager import ConfigManager
            from ckanext.terria_view.resource_utils import ResourceUtils
            resolved = ResourceUtils(ConfigManager(site_url=tk.config.get("ckan.site_url", ""))).resolve_private_resource_source(resource_id)
            if not resolved:
                raise ValueError("Could not resolve the uploaded resource")
            url = resolved[0]
        elif hasattr(upload, "get_path"):
            # Only the CKAN uploader resolves local paths. Never accept a path from the client or resource URL.
            with open(upload.get_path(resource_id), "rb") as handle:
                raw = handle.read(settings.max_bytes + 1)
    if raw is None:
        raw = download(urljoin(tk.config.get("ckan.site_url", "") + "/", url), settings.max_bytes)
    if len(raw) > settings.max_bytes:
        raise ValueError("Resource exceeds the download limit")
    if fmt in ("geojson", "json"):
        data = json.loads(raw)
        if data.get("type") != "FeatureCollection":
            raise ValueError("Expected a GeoJSON FeatureCollection")
        if len(data["features"]) > settings.max_rows:
            raise ValueError("Dataset exceeds the analysis row limit")
        frame = pd.DataFrame([{**(f.get("properties") or {}), "_geometry": f.get("geometry")} for f in data["features"]])
    else:
        text = raw.decode("utf-8-sig")
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
        frame = pd.read_csv(io.StringIO(text), sep=delimiter, nrows=settings.max_rows + 1, low_memory=False)
    if len(frame) > settings.max_rows:
        raise ValueError("Dataset exceeds the analysis row limit")
    if frame.columns.duplicated().any():
        raise ValueError("Duplicate columns must be disambiguated")
    return frame


def describe(user_id, resource_id, settings):
    resource, package = resource_for(user_id, resource_id)
    result = {"source": descriptor(resource, package)}
    fmt = str(resource.get("format") or "").lower()
    if fmt in ("csv", "geojson", "json") or fmt.startswith("csv-geo") or resource.get("datastore_active"):
        result.update(profile(load_frame(user_id, resource_id, settings)))
    else:
        result["requires_loaded_layer"] = True
    return clean(result)
