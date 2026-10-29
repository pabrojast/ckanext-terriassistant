"""Authorized CKAN reads and bounded downloads; no browser credentials forwarded."""
import csv
import io
import ipaddress
import json
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
            "dataset_name": str(package.get("title") or package["id"])[:200],
            "url": "/dataset/" + package["id"] + "/resource/" + resource["id"],
            "private": bool(package.get("private")), "modified": resource.get("last_modified"),
            "units": None}


def search(user_id, query, limit=10):
    from ckan.plugins import toolkit as tk
    context = context_for(user_id)
    result = tk.get_action("package_search")(context, {"q": query, "rows": limit,
                                                        "include_private": True})
    sources = []
    for package in result["results"]:
        for resource in package.get("resources", [])[:20]:
            try:
                verified, parent = resource_for(user_id, resource["id"])
                sources.append(descriptor(verified, parent))
            except (tk.NotAuthorized, tk.ObjectNotFound):
                continue
    return {"datasets_found": result["count"], "sources": sources}


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
