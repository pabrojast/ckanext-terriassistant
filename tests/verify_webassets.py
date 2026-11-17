"""Exercise CKAN's real asset/template stack without a database or services.

Run in a CKAN environment with this extension installed: python tests/verify_webassets.py.
Flask's test client serves the generated files; this does not boot CKAN middleware.
"""
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, send_from_directory
from jinja2 import Environment, FileSystemLoader

from ckan.common import config
from ckan.lib import helpers, i18n, webassets_tools
from ckan.lib.jinja_extensions import AssetExtension
from ckanext.terriassistant import plugin


def main():
    extension = Path(plugin.__file__).parent
    with tempfile.TemporaryDirectory(prefix="assistant-webassets-") as generated:
        config.update({
            "ckan.base_public_folder": "public",
            "ckan.webassets.path": generated,
            "ckan.site_url": "https://catalogue.test",
            "ckan.root_path": "",
            "debug": False,
            "extra_template_paths": [],
            "extra_public_paths": [],
        })
        i18n.locales = ["en"]
        webassets_tools.webassets_init()
        plugin.TerriassistantPlugin().update_config(config)
        app = Flask(__name__)
        app.add_url_rule("/user/login", "user.login", lambda: "login")
        app.register_blueprint(plugin.terriassistant_api)
        app.add_url_rule(
            "/webassets/<path:filename>", "webassets",
            lambda filename: send_from_directory(generated, filename),
        )
        templates = Environment(
            loader=FileSystemLoader(extension / "templates"),
            extensions=[AssetExtension], autoescape=True,
        )
        evidence = []
        for prefix in ("", "/wins"):
            config["ckan.root_path"] = prefix
            with app.test_request_context("/dataset/example"):
                rendered = templates.get_template("terriassistant.html").render(
                    h=helpers, _=lambda text: text, view_id="view-one",
                    title="Map Assistant", chat_enabled=True,
                    terria_instance_url=prefix + "/terria/", encoded_config="config",
                )
                for action in ("bootstrap", "chat", "save"):
                    expected = prefix + "/api/terriassistant/view/view-one/" + action
                    assert 'data-' + action + '-url="' + expected + '"' in rendered
                tags = str(webassets_tools.render_assets("style")) + str(
                    webassets_tools.render_assets("script")
                )
            urls = re.findall(r'(?:href|src)="([^"]+)"', tags)
            assert len(urls) == 2, tags
            client = app.test_client()
            for url in urls:
                parsed = urlsplit(url)
                assert parsed.path.startswith(prefix + "/webassets/terriassistant/"), url
                assert parsed.query, "Bundle must include a cache-busting version"
                response = client.get(url[len(prefix):])
                assert response.status_code == 200, response.status
                if parsed.path.endswith(".css"):
                    assert response.mimetype == "text/css", response.mimetype
                    source = extension / "public/terriassistant/terriassistant.css"
                else:
                    assert response.mimetype in ("text/javascript", "application/javascript")
                    source = extension / "public/terriassistant/terriassistant.js"
                assert response.data.strip() == source.read_bytes().strip()
                evidence.append({"url": url, "status": response.status_code,
                                 "mime": response.mimetype, "bytes": len(response.data)})
        print(json.dumps({"success": True, "assets": evidence}, indent=2))


if __name__ == "__main__":
    main()
