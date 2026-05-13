=======================
ckanext-terriassistant
=======================

A CKAN resource view that is *half TerriaJS map, half chat assistant*: it loads the
uploaded spatial file into an embedded `TerriaJS <https://terria.io>`_ instance and lets
you restyle the map by chatting with an AI assistant. Tell it *"color the points by
population using a red palette with 5 bins"* and the map reloads with that styling; click
**Save** to persist it on the resource view.

It is the spatial counterpart of ``ckanext-chartscha`` (chat-driven Chart.js charts) and
reuses the iframe ``#start=`` embedding approach of ``ckanext-terria_view``.

How it works
============

* Registers a ``terriassistant`` resource view (auto-created for supported resources).
* ``setup_template_variables`` builds a TerriaJS ``initSources`` catalog config pointing
  at the resource and renders it in an iframe via ``<terria_url>#start=<encoded JSON>``.
* The chat sends your message to a configurable OpenAI-compatible / DeepSeek endpoint.
  The model does **not** write TerriaJS configuration directly — it returns a small,
  high-level ``style_spec`` (e.g. ``{"color_by": "population", "color_method": "bins",
  "bins": 5, "palette": "YlGnBu"}``). The server **validates** that spec against the
  dataset profile (column names must exist, palettes/colours/ranges are checked,
  retrying the model once with feedback if needed) and then **translates** it into a
  correct TerriaJS v8 catalog-item config (``styles[]`` / ``activeStyle`` with
  ``color`` / ``point`` / ``outline`` / ``pointSize`` traits for tabular data,
  simplestyle-spec ``style`` for constant GeoJSON styling, ``opacity`` for everything).
  The ``#start=`` config is rebuilt and the iframe reloads.
* **Save** stores the styling in the resource view (``terria_state_json``); the view then
  renders with that styling on subsequent loads.
* Private uploaded resources are served through a signed-token CKAN proxy
  (``/api/terriassistant/resource/<id>/content``) so the cross-origin Terria iframe can
  load them without depending on the storage backend's CORS configuration.

Supported resource formats
==========================

``csv``, ``csv-geo-*``, ``geojson``, ``shp``, ``kml``/``kmz``, ``czml``, ``wms``,
``wfs``, ``wmts``, ``esri rest``, ``tif``/``tiff``/``geotiff``/``cog``. Data-driven
styling (color/size by column) is available for ``csv`` and ``geojson``; other formats
support layer opacity only.

Installation
============

#. Activate your CKAN virtualenv.
#. Install the extension and its requirements::

      pip install -e /path/to/ckanext-terriassistant
      pip install -r /path/to/ckanext-terriassistant/requirements.txt

#. Add ``terriassistant`` to ``ckan.plugins`` in your CKAN ``.ini``.
#. Set at least the AI key (see config below).
#. Restart CKAN.

Configuration
=============

::

   ckanext.terriassistant.default_title = Map Assistant
   ckanext.terriassistant.terria_instance_url = https://ihp-wins.unesco.org/terria/
   ckanext.terriassistant.deepseek_api_key = your-api-key
   ckanext.terriassistant.deepseek_base_url = https://api.deepseek.com
   ckanext.terriassistant.deepseek_model = deepseek-v4-pro
   ckanext.terriassistant.deepseek_thinking = false
   ckanext.terriassistant.max_resource_bytes = 26214400
   ckanext.terriassistant.max_rows = 150000
   ckanext.terriassistant.profile_rows = 5000
   ckanext.terriassistant.sample_rows = 20
   ckanext.terriassistant.sample_features = 20
   ckanext.terriassistant.prompt_history_messages = 10
   ckanext.terriassistant.max_config_bytes = 4194304
   ckanext.terriassistant.proxy_token_ttl = 3600

``deepseek_base_url`` accepts any OpenAI-compatible ``/chat/completions`` endpoint.
Without ``deepseek_api_key`` the map still renders; the chat panel is shown disabled.

Notes
=====

* If the TerriaJS instance is on a different origin than CKAN (the common case), make
  sure that instance allows being embedded in an iframe. Private resources are served by
  this plugin's proxy with ``Access-Control-Allow-Origin: *`` so Terria can fetch them.
* Each styling change reloads the Terria iframe, which resets the camera/zoom. This keeps
  the plugin compatible with any hosted TerriaJS build.

License
=======

AGPL v3 or later.
