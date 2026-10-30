"""Small reviewed retrieval corpus, shipped and versioned with the extension.

Runtime CKAN examples are retrieved separately under the user's permissions;
they never enter this shared corpus. References point to this fork's contracts.
"""
import re
import unicodedata

VERSION = "2026-10-30.1"
DOCUMENTS = [
    dict(id="K1", title="Map composition / Composición del mapa", types=[],
         tags="layers capas orden ordenar visibility visibilidad base mapa zoom encuadre opacity opacidad rename nombre",
         source="terriajs/lib/Models/DataAssistant/MapEdits.ts",
         text="Use propose_map_changes with existing source IDs. update_layer accepts name, show, opacity (0..1) and show_legend. move_layer index is zero-based workbench order. remove_layer only removes from this map. set_basemap must use an advertised basemap_id; zoom uses a loaded layer. Changes require Apply and can be undone. For multiple changes, propose one batch. Never claim it is applied until the next map context reflects it."),
    dict(id="K2", title="Table and vector colour / Colores de tablas y vectores", types=["csv", "geojson", "shp"],
         tags="color colour colores palette paleta categories categorias intervalos bins puntos point size tamaño leyenda legend",
         source="ckanext-terriassistant/ckanext/terriassistant/terria_builder.py",
         text="Inspect column names/types first. style_layer.style_spec uses color_by, color_method (continuous/bins/categories/constant), palette, bins, bin_maximums, category_colors, min_value, max_value, null_color, null_label, size_by, size_min and size_max. The builder emits styles[]/activeStyle. Never invent category values or data minima. Use query_data for actual ranges. Example: style_spec={color_by:'value',color_method:'continuous',palette:'Blues'}, legend_title='Water level'. Applicable only when table_style is advertised."),
    dict(id="K3", title="Vector geometry / Geometría vectorial", types=["geojson", "shp", "wfs"],
         tags="polygon poligono line linea outline contorno marker simbolo fill relleno stroke",
         source="terriajs/lib/Traits/TraitsClasses/GeoJsonTraits.ts",
         text="Constant vector style uses fill_color, fill_opacity, stroke_color, stroke_width, stroke_opacity or marker_color. The compiler emits simplestyle style keys with hyphens and preserves other properties. Example: style_spec={fill_color:'#268b70',fill_opacity:0.5,stroke_color:'#173c39',stroke_width:2}. Do not replace GeoJSON features, URLs or IDs to change presentation. Existing perPropertyStyles may override the base style; inspect and explain such limits."),
    dict(id="K4", title="COG raster and temporal series / Raster y series", types=["cog", "cog-time-series"],
         tags="raster cog banda band range rango colores palette paleta reverse invertir transparency transparencia fecha date tiempo",
         source="terriajs/lib/Traits/TraitsClasses/CogCatalogItemTraits.ts",
         text="style_layer.raster_style uses palette, minimum AND maximum, band, reverse, hide_outside. Named palettes include greens, greys, ylgnbu, ylorrd, bluered and rdbu. Example: raster_style={palette:'ylgnbu',minimum:0,maximum:50}. The compiler sets renderOptions.single and explicit colorScaleMode='named' so saved custom colours do not win. Domain is a colour scale; transparency uses displayRange/applyDisplayRange. Never change units, NoData, value scale or tile URLs. set_date must select an available date; advertised bands only. Analysis requires a point/polygon and computed evidence."),
    dict(id="K5", title="WMS and service imagery / Servicios de mapas", types=["wms", "wmts"],
         tags="wms wmts imagen imagery style estilo service servicio",
         source="terriajs/lib/Models/Catalog/Ows/WebMapServiceCatalogItem.ts",
         text="WMS colour rendering is controlled by the service. style_layer.wms_style must be one of wms_styles advertised in the current context. Otherwise change opacity/visibility only. Do not generate SLD, arbitrary server parameters, SQL or JavaScript. Image-only WMS/WMTS layers cannot support numeric calculations without an actual data source."),
    dict(id="K6", title="Saved views, share links and files / Vistas y enlaces", types=[],
         tags="share shortlink enlace link json sld vista terria_view archivo file url guardar save",
         source="terriajs/lib/ReactViews/Map/Panels/SharePanel/BuildShareLink.ts",
         text="The context describes the resolved map even when opened from #share. Inspect CKAN views with inspect_map_source; add_ckan uses registered source_id and optional view_id. Saved resource styles take precedence over SLD defaults; only the matching model is imported. add_url may use only a data URL supplied by the user. Local files are selected in the UI. Sharing creates a new link, excludes local/private/non-shareable layers and does not save a CKAN view. Never say a local file is included in a share."),
    dict(id="K7", title="Spatial discovery / Búsqueda por área", types=[],
         tags="search buscar area poligono polygon solr dataset catalog catalogo cobertura spatial",
         source="ckanext-terriassistant/ckanext/terriassistant/data_assistant/spatial.py",
         text="search_catalog defaults to the selected polygon. CKAN package_search/ext_bbox finds candidates; exact intersections use the dataset's declared spatial coverage, not actual observations. Results may be partial: continue with next_cursor. Empty matches with more candidates are not a final absence of data. Missing/invalid coverage cannot confirm intersection. Resource views can be inspected before proposing addition. Search query uses short subject terms; formats, organization and tags are separate filters."),
]


def tokens(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9_]{2,}", text))


def search(query, layer_type=""):
    terms = tokens(query)
    ranked = []
    for doc in DOCUMENTS:
        score = len(terms & tokens(doc["tags"] + " " + doc["title"])) * 3
        score += len(terms & tokens(doc["text"]))
        score += 6 if layer_type and layer_type in doc["types"] else 0
        if score:
            ranked.append((score, doc))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return {"version": VERSION, "documents": [
        {key: doc[key] for key in ("id", "title", "source", "text")} for _, doc in ranked[:4]
    ]}
