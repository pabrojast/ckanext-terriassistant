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

Ask the data (Terria)
====================

The authenticated ``/api/terriassistant/data`` API complements the resource styling assistant. It uses ``deepseek-flash`` for tool selection and explanations; all numeric results come from deterministic tools. Enable ``ckanext.terriassistant.data_enabled = true`` and run dedicated workers::

    ckan -c /app/production.ini jobs worker terria-data

It reuses ``ckanext.terriassistant.deepseek_api_key`` and ``deepseek_base_url`` server-side. ``data_model`` defaults to ``deepseek-flash``; the styling assistant's existing model is unchanged. With ckanext-envvars, use ``CKANEXT__TERRIASSISTANT__DATA_ENABLED`` and ``CKANEXT__TERRIASSISTANT__DATA_MODEL``. The Terria frontend needs ``parameters.dataAssistant.enabled`` and same-origin ``ckanSession``.

Contract and tools
------------------

GET ``bootstrap`` provides a CSRF token and capabilities. POST ``conversations``, then POST ``conversations/{id}/turns`` with a unique request_id, question, selected source descriptors and optional dates/polygon/point. Poll GET ``turns/{id}``; when ``awaiting_tools``, the client executes only pending registered layer tools and POSTs ``turns/{id}/tool-results`` with exactly their IDs. Cancel via POST ``turns/{id}/cancel``; delete temporary state with DELETE ``conversations/{id}``.

Closed tools: search_catalog, describe_source, query_data, compare_results, present_result, suggest_map_action. Calculations cover filters/ranking, summary statistics, grouped aggregates, temporal series, annual linear trends (3 observations spanning 6 months), Pearson correlation, Tukey IQR outliers, mean/percentage comparisons and spatial selection. Missing/nonfinite values are excluded and counted; zero remains zero. Source columns and units are never invented. Different sources with unknown units cannot be subtracted. Correlations do not establish causation.

Evidence includes source identity, parameters, counts, coverage and truncation. Tables/charts/CSV refer to computed evidence; map actions require a user click. Browser tools calculate loaded CSV/GeoJSON and COG values; CKAN tools read DataStore or authorized CSV/GeoJSON resources. Samples from describe_source are marked and cannot support full-population claims. No arbitrary code, SQL or client-supplied download URL is executed. Downloads pin public DNS addresses and recheck each redirect; credentials are never forwarded.

Access and lifetime
-------------------

Every endpoint requires an active CKAN login; writes require CSRF. Conversations are bound to both account and browser session. Resource access is rechecked before provider calls and polling; raw analysis also requires ``ResourceUtils.user_may_download`` (including datashare permissions). Private source rows may be sent to DeepSeek only under those permissions. Logout invalidates session conversations and cancels jobs. API responses are private/no-store; prompts, provider keys and reasoning are not returned. Redis contains ephemeral state and expires it, with no conversation database/history or data in shares.

Defaults (``ckanext.terriassistant.data_`` prefix): ttl=1800 seconds, timeout=300 seconds, max_rounds=8, max_calls=16, max_bytes=26214400, max_rows=150000, request_bytes=4194304. One active turn per user and 10 questions/conversations per minute. Result tables are capped at 200 rows, time series at 2000 points; the model gets at most the first 100 result rows explicitly marked as a preview. Five turns of history are retained for context. Oversized input fails instead of sampling silently.

Verification
------------

Install requirements plus pytest, fakeredis[lua], flask and flask-wtf; run ``python -m pytest -q tests``. CI exercises jsonschema 3.0 (compatible with the existing CKAN DOI extension) and 4.23 using Draft 7 schemas. API tests use a CKAN stub, Flask sessions and Redis Lua semantics; integrated CKAN/DeepSeek/browser validation is additionally required before environment promotion.

Catalog searches resolve an exact dataset ID/name through authorized ``package_show`` before full-text search, retain both dataset slug and title, and return at most 20 resource descriptors. Truncation is explicit so the model can narrow broad searches; this also bounds repeated permission checks during polling.

AI Assistant: map design and spatial discovery
==============================================

The authenticated data conversation also supports map design. The frontend label is
AI Assistant / Asistente IA; the existing ``dataAssistant`` configuration and API
prefix remain compatible. Bootstrap advertises ``capabilities.map_editing=1`` and
``spatial_search=1``. Turn requests optionally include a validated ``map_context``
with layer capabilities, presentation, columns, dates, basemaps and camera. It is
refreshed each turn and excludes credentials, source URLs and raw files.

New tools are ``search_map_knowledge``, ``inspect_map_source`` and
``propose_map_changes``. Proposals return an ID, base revision and typed operations;
they do not mutate CKAN or the map. The client validates, applies on user click,
rolls back failures and keeps a conflict-aware session-only undo history.
Table/vector styles reuse the existing style-spec builder; COG styles use native
renderOptions and WMS styles must be advertised by the service. Existing numeric
analysis, evidence and legacy actions remain supported.

``knowledge.py`` ships reviewed English/Spanish retrieval tags, guidance and
examples, versioned with the extension. Retrieval ranks format/task/text matches
and returns at most four referenced excerpts. No embeddings or vector service are
required. Authorized CKAN views are inspected on demand, sanitized before entering
model context and never added to the shared corpus.

POST ``catalog/search`` accepts query, polygon, formats, organization, tags and a
continuation cursor. It is authenticated, CSRF-protected and rate-limited. It uses
CKAN ``package_search`` with ``extras.ext_bbox`` and the calling user's permissions,
then intersects declared dataset coverage with the drawn polygon. If search hooks
omit a private resource list, it is recovered through the calling user's authorized
``package_show`` action; denied datasets remain excluded. It does not
claim actual observations inside the area. Up to 100 candidate datasets and 20
resource descriptors are considered per response; ``next_cursor`` continues both
datasets and resources. Empty partial pages are not final no-results answers.
Stored coverage follows GeoJSON antimeridian semantics; drawn polygons are
unwrapped and split. Missing/invalid coverage cannot establish an intersection.

GET ``sources/<resource_id>/map?view_id=...`` checks read/download permissions and
returns view choices plus the generated Terria model, without shared caching.
Restricted models are non-shareable and proxy tokens are stripped for same-origin
session authorization. It requires the matching ``terria_view`` release with
``saved_config`` support. The assistant can add user-supplied HTTP(S) data URLs and
local files using the browser's existing loaders. Local files remain local; share
links omit local/private/non-shareable layers. This release does not create
datasets or save resource views.
