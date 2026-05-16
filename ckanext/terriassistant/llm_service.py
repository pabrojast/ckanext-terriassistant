from __future__ import annotations

import json
from typing import Any

import requests

from .config import TerriassistantSettings


class LlmServiceError(Exception):
    pass


class LlmConfigurationError(LlmServiceError):
    pass


class LlmResponseError(LlmServiceError):
    pass


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are a senior GIS cartographer working inside CKAN. A single dataset is loaded
into a TerriaJS map as one layer (catalog item) of type "{terria_type}". The user
chats with you to restyle that layer. You DO NOT write TerriaJS configuration —
you produce a small, high-level "style_spec" object and the host application
translates it into a valid TerriaJS configuration.

Return valid JSON only, with exactly this shape:
{{
  "assistant_message": "<short reply in the user's language>",
  "action": "apply_style" | "ask_clarification",
  "style_spec": {{ ... }},
  "warnings": [],
  "suggested_prompts": ["...", "..."]
}}

Rules:
- Use ONLY the columns / properties listed in the data profile. Never invent
  column names, palette names or option keys.
- "style_spec" must be a flat object. Each turn return the FULL spec reflecting
  the user's cumulative intent (the previous spec is provided in
  "current_style_spec" for reference) — do not return only the delta.
- Only include keys that are relevant; omit everything else (an empty {{}} means
  "use TerriaJS defaults").
- If the request is ambiguous, impossible with the available data, or not a
  styling request, set action to "ask_clarification", ask a short question, and
  you may repeat or omit current_style_spec.
- Otherwise set action to "apply_style".
- Keep assistant_message to one or two short sentences. Use the user's language.

style_spec keys (all optional):

  Data-driven color (works for any column on CSV layers, and for a feature
  property on GeoJSON layers):
    "color_by": "<column or property name>"
    "color_method": "continuous" | "bins" | "categories" | "constant"
        (default: "continuous" for numeric columns, "categories" otherwise)
    "palette": "<palette name, see list below>"
    "bins": <integer 2..15>            (for color_method "bins")
    "bin_maximums": [<number>, ...]    (alternative explicit bin edges for "bins")
    "category_colors": [{{"value":"A","color":"#1f77b4"}}, ...]  (for "categories")
    "min_value": <number>, "max_value": <number>   (clip the color scale)
    "null_color": "<css color>", "null_label": "<text>", "outlier_color": "<css color>"
    "legend_ticks": <integer 0..20>

  Point sizing (CSV / GeoJSON point layers):
    "size_by": "<numeric column>"
    "size_min": <pixels>, "size_max": <pixels>

  Constant point/marker symbology (CSV points, also accepted for GeoJSON points):
    "marker": "circle" | "square" | "triangle" | "star" | "<Maki icon name>" | "<image URL>"
    "marker_size": <pixels, 1..256>
    "marker_rotation": <degrees>
    "marker_color": "<css color>"      (a single colour for all markers)
    "outline_color": "<css color>", "outline_width": <pixels>

  GeoJSON polygon / line constant styling (simplestyle-spec):
    "fill_color": "<css color>", "fill_opacity": <0..1>
    "stroke_color": "<css color>", "stroke_width": <pixels>, "stroke_opacity": <0..1>
    "clamp_to_ground": <boolean>

  General:
    "opacity": <0..1>                  (whole layer; valid for every layer type)

  CSV column overrides (only if TerriaJS picks the wrong columns):
    "latitude_column": "<column>", "longitude_column": "<column>", "region_column": "<column>"

Colours: use hex like "#1f77b4" (or simple CSS names like "red", "steelblue").
Palettes (case sensitive): {palettes}.
For sequential numeric data prefer "Viridis", "Reds", "Blues", "YlOrRd"; for
diverging data "RdBu", "RdYlBu", "BrBG", "Spectral"; for categories "Set1",
"Set2", "Category10", "Tableau10".

Layer-type notes:
{layer_notes}

Examples:
- "color the points by population, red gradient" ->
  {{"action":"apply_style","style_spec":{{"color_by":"population","color_method":"continuous","palette":"Reds"}},"assistant_message":"Done — points are now coloured by population on a red gradient."}}
- "shade regions by GDP in 5 classes" ->
  {{"action":"apply_style","style_spec":{{"color_by":"GDP","color_method":"bins","bins":5,"palette":"YlGnBu"}},"assistant_message":"Regions are now shaded by GDP in 5 classes."}}
- "make all markers blue and a bit bigger with a thin black outline" ->
  {{"action":"apply_style","style_spec":{{"marker_color":"#1f77b4","marker_size":14,"outline_color":"#000000","outline_width":1}},"assistant_message":"Markers are now blue, larger and outlined."}}
- "fill polygons green at 50% with a 2px dark outline" ->
  {{"action":"apply_style","style_spec":{{"fill_color":"#2ca25f","fill_opacity":0.5,"stroke_color":"#222222","stroke_width":2}},"assistant_message":"Polygons are filled green at 50% with a dark outline."}}
- "set the layer opacity to 0.6" ->
  {{"action":"apply_style","style_spec":{{"opacity":0.6}},"assistant_message":"Layer opacity set to 0.6."}}
"""

_CSV_NOTES = (
    "This is a tabular (CSV) layer. It may be point data (lat/lon columns) or "
    "region-mapped data (a column matching known regions). Data-driven 'color_by'/"
    "'size_by' work on any column. 'marker'/'marker_size'/'marker_color'/'outline_*' "
    "only affect point data; 'fill_*'/'stroke_*' are ignored for CSV."
)
_GEOJSON_NOTES = (
    "This is a GeoJSON layer. For CONSTANT styling of polygons/lines use 'fill_*' "
    "and 'stroke_*' (and 'marker_color'/'marker' for points). For DATA-DRIVEN "
    "colouring/sizing by a feature property use 'color_by'/'size_by' (do not mix "
    "constant 'fill_*'/'stroke_*' with data-driven keys in the same request — pick one)."
)
_OPAQUE_NOTES = (
    "This layer type does not support data-driven styling. The ONLY supported key "
    "is 'opacity'. For anything else, return action 'ask_clarification' explaining "
    "the limitation."
)


def _layer_notes(terria_type: str) -> str:
    if terria_type == "csv":
        return _CSV_NOTES
    if terria_type == "geojson":
        return _GEOJSON_NOTES
    return _OPAQUE_NOTES


class LlmService:
    def __init__(self, settings: TerriassistantSettings):
        self.settings = settings

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        profile: dict[str, Any],
        current_spec: dict[str, Any] | None,
        terria_type: str,
        validation_feedback: str = "",
    ) -> dict[str, Any]:
        if not self.settings.deepseek_api_key:
            raise LlmConfigurationError(
                "The AI service is not configured. Set ckanext.terriassistant.deepseek_api_key."
            )

        base_messages = self._build_messages(
            messages, profile, current_spec, terria_type, validation_feedback
        )
        repair_turns: list[dict[str, str]] = []
        max_attempts = max(1, 1 + int(self.settings.json_repair_attempts))
        last_error: LlmResponseError | None = None

        for _ in range(max_attempts):
            raw_content = self._call_chat_completion(base_messages + repair_turns)
            try:
                candidate = json.loads(raw_content)
            except ValueError as exc:
                last_error = LlmResponseError(
                    "The AI service returned malformed JSON."
                )
                repair_turns = self._build_repair_turns(
                    raw_content, "JSON parse error: {0}".format(exc)
                )
                continue
            try:
                return self._normalize_candidate(candidate, profile)
            except LlmResponseError as exc:
                last_error = exc
                repair_turns = self._build_repair_turns(raw_content, str(exc))
                continue

        raise last_error or LlmResponseError(
            "The AI service returned an invalid JSON response."
        )

    # ------------------------------------------------------------------

    def _call_chat_completion(self, messages: list[dict[str, str]]) -> str:
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 1800,
            "thinking": (
                {"type": "enabled", "reasoning_effort": "high"}
                if self.settings.deepseek_thinking
                else {"type": "disabled"}
            ),
        }
        url = "{0}/chat/completions".format(self.settings.deepseek_base_url.rstrip("/"))
        headers = {
            "Authorization": "Bearer {0}".format(self.settings.deepseek_api_key),
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=(5, 60))
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LlmServiceError("The AI service request failed.") from exc
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmResponseError(
                "The AI service returned an unexpected envelope."
            ) from exc

    def _build_repair_turns(self, raw_content: str, error_detail: str) -> list[dict[str, str]]:
        # Truncate the broken content so we don't blow the context window.
        truncated = (raw_content or "")[:2000]
        feedback = (
            "Your previous response could not be used. "
            "Issue: {0}\n\n"
            "Reply again with ONLY a single valid JSON object matching the schema "
            "described in the system message. Do not include any prose, code fences, "
            "or commentary outside the JSON. Preserve the user's cumulative intent."
        ).format(error_detail)
        return [
            {"role": "assistant", "content": truncated},
            {"role": "user", "content": feedback},
        ]

    # ------------------------------------------------------------------

    def _build_messages(
        self,
        messages: list[dict[str, str]],
        profile: dict[str, Any],
        current_spec: dict[str, Any] | None,
        terria_type: str,
        validation_feedback: str,
    ) -> list[dict[str, str]]:
        from .terria_builder import ALLOWED_PALETTES

        history = []
        for item in messages[-self.settings.prompt_history_messages :]:
            role = item.get("role")
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            history.append({"role": role, "content": content})

        system_prompt = _SYSTEM_PROMPT.format(
            terria_type=terria_type,
            palettes=", ".join(sorted(ALLOWED_PALETTES)),
            layer_notes=_layer_notes(terria_type),
        ).strip()

        context_message = {
            "role": "user",
            "content": json.dumps(
                {
                    "data_profile": profile,
                    "current_style_spec": current_spec,
                    "validation_feedback": validation_feedback or None,
                    "instruction": "Use the data profile above as the only source of truth for column names.",
                },
                ensure_ascii=False,
            ),
        }
        return [{"role": "system", "content": system_prompt}, context_message] + history

    def _normalize_candidate(self, candidate: Any, profile: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            raise LlmResponseError("The AI service returned a non-object candidate.")
        action = str(candidate.get("action") or "").strip().lower()
        if action not in {"apply_style", "ask_clarification"}:
            raise LlmResponseError("The AI service returned an invalid action.")
        assistant_message = str(candidate.get("assistant_message") or "").strip()
        if not assistant_message:
            raise LlmResponseError("The AI service returned an empty assistant message.")

        warnings = candidate.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        suggested_prompts = candidate.get("suggested_prompts") or profile.get("suggested_prompts", [])
        if not isinstance(suggested_prompts, list):
            suggested_prompts = profile.get("suggested_prompts", [])

        # Accept a couple of common aliases the model might use.
        spec = candidate.get("style_spec")
        if spec is None:
            spec = candidate.get("terria_overrides") or candidate.get("spec")
        if action == "apply_style" and not isinstance(spec, dict):
            raise LlmResponseError("The AI service did not return a style_spec for apply_style.")
        if action == "ask_clarification" and not isinstance(spec, dict):
            spec = None

        return {
            "assistant_message": assistant_message,
            "action": action,
            "style_spec": spec,
            "warnings": [str(w) for w in warnings][:5],
            "suggested_prompts": [str(s) for s in suggested_prompts][:4],
        }
