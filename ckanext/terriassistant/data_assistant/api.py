"""Same-origin session API. Only server-issued tool calls may be continued."""
import hashlib
import json
import math
import re
import time
import uuid

from flask import Blueprint, jsonify, request, session
from flask_wtf.csrf import generate_csrf, validate_csrf
from werkzeug.exceptions import HTTPException
from wtforms.validators import ValidationError

from ckan.plugins import toolkit as tk

from . import runtime, sources
from .settings import Settings
from .tools import SCHEMAS, validate_call

blueprint = Blueprint("terria_data", __name__, url_prefix="/api/terriassistant/data")
ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")
UUID = re.compile(r"^[a-fA-F0-9-]{36}$")


def fail(message, code=400):
    from werkzeug.exceptions import abort
    abort(code, description=message)


def user():
    current = tk.current_user
    if not current or getattr(current, "is_anonymous", True) or getattr(current, "state", "") != "active":
        fail("Sign in to IHP to ask about data", 401)
    return current


def owner():
    current = user()
    nonce = session.get("terria_data_session")
    if not nonce:
        nonce = uuid.uuid4().hex
        session["terria_data_session"] = nonce
    return str(current.id), hashlib.sha256((str(current.id) + ":" + nonce).encode()).hexdigest()


def clear_session():
    """Called by CKAN's logout hook before its authentication cookie is cleared."""
    fingerprint = session.pop("terria_data_owner", None)
    session.pop("terria_data_session", None)
    if not fingerprint:
        return
    index = runtime.PREFIX + "session:" + fingerprint
    try:
        for raw in runtime.redis().smembers(index):
            cid = raw.decode()
            state = runtime.get(cid)
            if state and state.get("owner") == fingerprint:
                if state.get("turn"):
                    runtime.redis().setex(runtime.PREFIX + "cancel:" + state["turn"]["id"], settings().ttl, "1")
                    runtime.release(state)
                runtime.redis().delete(runtime.key(cid))
        runtime.redis().delete(index)
    except Exception:
        # Logout itself must succeed even if Redis is unavailable; temporary records still expire.
        runtime.log.warning("Could not immediately clear Terria data session")


def settings():
    return Settings.from_config(tk.config)


@blueprint.before_request
def protect():
    user()
    if not settings().enabled:
        fail("The data assistant is unavailable", 503)
    if request.content_length and request.content_length > settings().request_bytes:
        fail("Request too large; narrow the query", 413)
    if request.method not in ("GET", "HEAD"):
        try:
            validate_csrf(request.headers.get("X-CSRFToken"))
        except ValidationError:
            fail("Refresh the assistant to renew your session", 403)


@blueprint.after_request
def private(response):
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["CDN-Cache-Control"] = "no-store"
    response.headers["Surrogate-Control"] = "no-store"
    response.headers["Vary"] = "Cookie"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        from ckanext.terria_view.api_endpoints import _disable_ckan_response_cache
        _disable_ckan_response_cache()
    except ImportError:
        pass
    return response


@blueprint.errorhandler(Exception)
def error(exc):
    if isinstance(exc, HTTPException):
        return jsonify(error=exc.description), exc.code
    if isinstance(exc, tk.NotAuthorized):
        return jsonify(error="Access to these data is no longer permitted"), 403
    if isinstance(exc, tk.ObjectNotFound):
        return jsonify(error="Data not found"), 404
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return jsonify(error="Invalid request or unavailable result"), 400
    runtime.log.warning("Terria data API failed (%s)", type(exc).__name__)
    return jsonify(error="The data assistant is temporarily unavailable"), 503


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        fail("Expected a JSON object")
    return data


def owned(cid):
    if not ID.fullmatch(cid):
        fail("Conversation not found", 404)
    uid, fingerprint = owner()
    state = runtime.get(cid)
    if not state or state["owner"] != fingerprint or state["user"] != uid:
        fail("Conversation expired; start a new question", 404)
    runtime.check_permissions(state)
    runtime.redis().expire(runtime.key(cid), settings().ttl)
    runtime.redis().expire(runtime.PREFIX + "session:" + fingerprint, settings().ttl)
    return state


def for_turn(tid):
    if not ID.fullmatch(tid):
        fail("Turn not found", 404)
    cid = runtime.redis().get(runtime.PREFIX + "turn:" + tid)
    if not cid:
        fail("Turn expired", 404)
    state = owned(cid.decode())
    if not state.get("turn") or state["turn"]["id"] != tid:
        fail("Turn expired", 404)
    return state


def public_turn(state):
    turn = dict(state["turn"])
    if runtime.redis().exists(runtime.PREFIX + "cancel:" + turn["id"]):
        return {"id": turn["id"], "status": "cancelled"}
    if turn["status"] not in runtime.TERMINAL and time.time() - turn["started"] > settings().timeout:
        runtime.redis().setex(runtime.PREFIX + "cancel:" + turn["id"], settings().ttl, "1")
        runtime.release(state)
        return {"id": turn["id"], "status": "error", "error": "Analysis timed out; retry with a smaller range"}
    turn["evidence"] = [state["evidence"][eid] for eid in turn["evidence_ids"]]
    # Only expose outputs; never return prompts, provider credentials or reasoning.
    return {k: turn[k] for k in ("id", "status", "progress", "pending", "answer", "error", "evidence", "artifacts", "actions") if k in turn}


@blueprint.get("/bootstrap")
def bootstrap():
    from ..config import TerriassistantSettings
    provider = TerriassistantSettings.from_config(tk.config)
    return jsonify(enabled=bool(provider.deepseek_api_key), model=settings().model,
                   csrf_token=generate_csrf(), tools=SCHEMAS,
                   limits={"max_rows": settings().max_rows, "max_bytes": settings().max_bytes,
                           "timeout": settings().timeout, "ttl": settings().ttl})


@blueprint.post("/conversations")
def create():
    uid, fingerprint = owner()
    rate_key = runtime.PREFIX + "creates:" + uid + ":" + str(int(time.time()) // 60)
    if runtime.redis().incr(rate_key) > 10:
        fail("Too many conversations; retry in a minute", 429)
    runtime.redis().expire(rate_key, 120)
    cid = uuid.uuid4().hex
    runtime.put({"id": cid, "owner": fingerprint, "user": uid, "messages": [],
                 "sources": {}, "used_resources": [], "evidence": {}, "next_evidence": 1})
    session["terria_data_owner"] = fingerprint
    runtime.redis().sadd(runtime.PREFIX + "session:" + fingerprint, cid)
    runtime.redis().expire(runtime.PREFIX + "session:" + fingerprint, settings().ttl)
    return jsonify(id=cid), 201


@blueprint.delete("/conversations/<cid>")
def delete(cid):
    state = owned(cid)
    if state.get("turn"):
        runtime.redis().setex(runtime.PREFIX + "cancel:" + state["turn"]["id"], settings().ttl, "1")
        runtime.release(state)
    runtime.redis().delete(runtime.key(cid))
    return jsonify(deleted=True)


def source_context(data, uid):
    if not isinstance(data, list) or len(data) > 30:
        fail("Select at most 30 layers")
    result = {}
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or len(item["id"]) > 300:
            fail("Invalid layer")
        src = {k: str(item[k])[:200] for k in ("name", "format", "units", "time") if item.get(k) is not None}
        src["id"] = item["id"]
        src["client"] = True
        rid = item.get("resource_id")
        if rid:
            if not isinstance(rid, str) or not UUID.fullmatch(rid):
                fail("Invalid resource identifier")
            resource, package = sources.resource_for(uid, rid)
            src.update(sources.descriptor(resource, package))
            src.update(id=item["id"], client=True)
        result[src["id"]] = src
    return result


@blueprint.post("/conversations/<cid>/turns")
def start_turn(cid):
    data = body()
    state = owned(cid)
    question = data.get("question")
    request_id = data.get("request_id")
    if not isinstance(question, str) or not question.strip() or len(question) > 8000:
        fail("Enter a question of at most 8000 characters")
    if not isinstance(request_id, str) or not ID.fullmatch(request_id):
        fail("A request identifier is required")
    if state.get("turn", {}).get("request_id") == request_id:
        return jsonify(public_turn(state))
    current = source_context(data.get("sources", []), state["user"])
    selection = data.get("selection") or {}
    if not isinstance(selection, dict) or set(selection) - {"start", "end", "polygon", "point", "band"}:
        fail("Invalid map selection")
    # Reuse the same strict geometry/date validation as tools.
    validate_call("query_data", {"source_id": "selection", "operation": "rows", **selection})
    rate_key = runtime.PREFIX + "rate:" + state["user"] + ":" + str(int(time.time()) // 60)
    runtime.redis().expire(rate_key, 120)
    if runtime.redis().incr(rate_key) > 10:
        fail("Too many questions; retry in a minute", 429)
    runtime.redis().expire(rate_key, 120)
    tid = uuid.uuid4().hex
    if not runtime.redis().set(runtime.PREFIX + "active:" + state["user"], tid, nx=True, ex=settings().timeout + 10):
        fail("Another question is running; cancel it or wait for its answer", 409)
    state["sources"] = {k: v for k, v in state["sources"].items() if not v.get("client")}
    state["sources"].update(current)
    if len(state["sources"]) > 250:
        runtime.redis().delete(runtime.PREFIX + "active:" + state["user"])
        fail("Conversation has too many sources; start a new conversation")
    state["selection"] = selection
    # Bound history at completed-turn boundaries, never split assistant/tool pairs.
    starts = [i for i, msg in enumerate(state["messages"]) if msg["role"] == "user"]
    if len(starts) >= 5:
        state["messages"] = state["messages"][starts[-4]:]
    # Remove any interrupted tool sequence from the previous failed/cancelled turn.
    if state.get("turn", {}).get("status") != "completed" and starts:
        last = max((i for i, msg in enumerate(state["messages"]) if msg["role"] == "user"), default=0)
        state["messages"] = state["messages"][:last]
    state["messages"].append({"role": "user", "content": question.strip()})
    if len(state["evidence"]) > 64:
        state["evidence"] = dict(list(state["evidence"].items())[-64:])
    state["turn"] = {"id": tid, "request_id": request_id, "started": time.time(), "status": "queued",
                     "rounds": 0, "calls": 0, "evidence_ids": [], "artifacts": [], "actions": []}
    try:
        if not runtime.put(state, require_existing=True):
            runtime.release(state)
            fail("Conversation was closed", 404)
        runtime.redis().setex(runtime.PREFIX + "turn:" + tid, settings().ttl, cid)
        runtime.enqueue(cid)
    except Exception:
        runtime.release(state)
        raise
    return jsonify(public_turn(state)), 202


@blueprint.get("/turns/<tid>")
def poll(tid):
    return jsonify(public_turn(for_turn(tid)))


def bounded_result(result):
    if not isinstance(result, dict):
        fail("Invalid tool result")
    allowed = {"error", "columns", "sample", "sample_only", "rows_total", "rows_used", "rows", "statistics",
               "complete", "truncated", "operation", "units", "coverage", "warning", "method", "resolution",
               "band", "period", "start", "end", "filters", "invalid_dates", "offset"}
    if set(result) - allowed:
        fail("Unexpected tool result fields")
    def visit(value, depth=0):
        if depth > 12:
            fail("Tool result too deeply nested")
        if isinstance(value, float) and not math.isfinite(value):
            fail("Non-finite result")
        if isinstance(value, list):
            if len(value) > 2000:
                fail("Too many result rows")
            for v in value:
                visit(v, depth + 1)
        elif isinstance(value, dict):
            if len(value) > 100:
                fail("Too many result fields")
            for v in value.values():
                visit(v, depth + 1)
        elif isinstance(value, str) and len(value) > 10000:
            fail("Result value too long")
    visit(result)
    return result


@blueprint.post("/turns/<tid>/tool-results")
def tool_results(tid):
    state = for_turn(tid)
    if public_turn(state)["status"] != "awaiting_tools":
        fail("This turn is not waiting for tool results", 409)
    incoming = body().get("results")
    pending = state["turn"]["pending"]
    if not isinstance(incoming, list) or len(incoming) != len(pending):
        fail("Return exactly the pending tool results")
    if {v.get("id") for v in incoming if isinstance(v, dict)} != {v["id"] for v in pending}:
        fail("Tool result identifiers do not match")
    with runtime.redis().lock(runtime.PREFIX + "continue:" + tid, timeout=15, blocking_timeout=2):
        state = for_turn(tid)
        if state["turn"]["status"] != "awaiting_tools":
            fail("Tool results already accepted", 409)
        for call in pending:
            runtime.source_for(state, call["arguments"]["source_id"])
            result = bounded_result(next(v["result"] for v in incoming if v["id"] == call["id"]))
            if call["name"] == "query_data" and not result.get("error"):
                result = runtime.evidence(state, result, call["arguments"]["source_id"], call["arguments"])
            runtime.append_tool(state, call["id"], result)
        state["turn"].update(status="queued", pending=[])
        runtime.put(state, tid)
        runtime.enqueue(state["id"])
    return jsonify(public_turn(state)), 202


@blueprint.post("/turns/<tid>/cancel")
def cancel(tid):
    state = for_turn(tid)
    runtime.redis().setex(runtime.PREFIX + "cancel:" + tid, settings().ttl, "1")
    runtime.release(state)
    return jsonify(id=tid, status="cancelled")
