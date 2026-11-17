(function () {
  "use strict";

  function readJson(value) {
    if (!value) return null;
    try { return JSON.parse(value); } catch (e) { return null; }
  }
  function isObject(value) {
    return value && typeof value === "object" && !Array.isArray(value);
  }
  function stringify(value) { return JSON.stringify(value || null); }
  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function isEmptyObj(o) { return !o || !isObject(o) || Object.keys(o).length === 0; }

  function TerriassistantApp(root) {
    this.root = root;
    this.viewId = root.dataset.viewId;
    this.urls = {
      bootstrap: root.dataset.bootstrapUrl,
      chat: root.dataset.chatUrl,
      save: root.dataset.saveUrl
    };
    this.terriaInstanceUrl = root.dataset.terriaInstanceUrl;
    this.state = {
      bootstrap: null,
      ready: false,
      busy: false,
      csrfToken: "",
      storageScope: "anonymous",
      storageAvailable: true,
      loginUrl: "",
      chatConfigured: false,
      messages: [],
      savedSpec: {},
      draftSpec: {},
      pendingLocalDraft: null,
      chatEnabled: false,
      canSave: false,
      warnings: [],
      suggestedPrompts: []
    };
    this.el = {
      warnings: document.getElementById("ta-warnings"),
      login: document.getElementById("ta-login"),
      messages: document.getElementById("ta-messages"),
      suggestions: document.getElementById("ta-suggestion-list"),
      input: document.getElementById("ta-input"),
      status: document.getElementById("ta-status"),
      summary: document.getElementById("ta-summary"),
      banner: document.getElementById("ta-draft-banner"),
      send: document.getElementById("ta-send"),
      save: document.getElementById("ta-save"),
      resetSaved: document.getElementById("ta-reset-saved"),
      resetDefault: document.getElementById("ta-reset-default"),
      useDraft: document.getElementById("ta-use-draft"),
      discardDraft: document.getElementById("ta-discard-draft"),
      clearChat: document.getElementById("ta-clear-chat"),
      openWindow: document.getElementById("ta-open-window"),
      fullwidth: document.getElementById("ta-fullwidth"),
      iframe: document.getElementById("ta-iframe"),
      mapLoading: document.getElementById("ta-map-loading"),
      mapSurface: document.querySelector(".ta-map-surface")
    };
  }

  TerriassistantApp.prototype.init = async function () {
    this.bindEvents();
    if (this.el.iframe) {
      var self = this;
      this.el.iframe.addEventListener("load", function () {
        if (self.el.iframe.src !== "about:blank") self.hideMapLoading();
      });
    }
    await this.loadBootstrap();
  };

  TerriassistantApp.prototype.bindEvents = function () {
    var self = this;
    document.getElementById("ta-chat-form").addEventListener("submit", function (e) {
      e.preventDefault();
      self.sendMessage();
    });
    this.el.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        self.sendMessage();
      }
    });
    this.el.save.addEventListener("click", function () { self.saveDraft(); });
    this.el.resetSaved.addEventListener("click", function () { self.resetToSaved(); });
    this.el.resetDefault.addEventListener("click", function () { self.resetToDefault(); });
    this.el.useDraft.addEventListener("click", function () { self.usePendingDraft(); });
    this.el.discardDraft.addEventListener("click", function () { self.discardPendingDraft(); });
    this.el.clearChat.addEventListener("click", function () { self.clearChat(); });
    this.el.fullwidth.addEventListener("click", function () { self.toggleFullWidth(); });
  };

  // ---- bootstrap -------------------------------------------------------

  TerriassistantApp.prototype.loadBootstrap = async function () {
    if (this.state.busy) return false;
    this.setBusy(true);
    this.setStatus("Loading map…", true);
    try {
      var data = await this.requestJson(this.urls.bootstrap);
      this.state.bootstrap = data;
      this.state.csrfToken = data.csrf_token || "";
      this.state.storageScope = String(data.storage_scope || "anonymous");
      this.state.chatConfigured = !!data.chat_enabled;
      this.state.chatEnabled = this.state.chatConfigured && data.can_chat === true;
      this.state.loginUrl = data.login_url || "";
      this.state.canSave = !!data.can_save;
      this.state.savedSpec = isObject(data.saved_spec) ? data.saved_spec : {};
      this.state.warnings = Array.isArray(data.warnings) ? data.warnings.slice() : [];
      this.state.suggestedPrompts = Array.isArray(data.suggested_prompts) ? data.suggested_prompts : [];
      var messages = readJson(this.storageGet(this.messageKey()));
      this.state.messages = Array.isArray(messages) ? messages.filter(function (m) {
        return isObject(m) && (m.role === "user" || m.role === "assistant") && typeof m.content === "string";
      }) : [];

      var activeSpec = isObject(data.active_spec) ? data.active_spec : this.state.savedSpec;
      var localDraft = this.loadLocalDraft();
      if (localDraft && stringify(localDraft.spec) !== stringify(this.state.savedSpec)) {
        this.state.pendingLocalDraft = localDraft;
        this.state.draftSpec = activeSpec;
        this.toggleBanner(true);
      } else {
        this.state.pendingLocalDraft = null;
        this.state.draftSpec = activeSpec;
        this.toggleBanner(false);
        if (localDraft) this.storageRemove(this.draftKey());
      }

      // The template already shows the saved map. Do not reset its camera on
      // bootstrap, but recover when the initial render could not make a map.
      if (data.encoded_config && this.el.iframe &&
          String(this.el.iframe.src || "").indexOf("#start=") === -1) {
        this.reloadMap(data.encoded_config);
      } else if (data.encoded_config && this.el.openWindow) {
        this.el.openWindow.href = this.buildShareUrl(data.encoded_config);
      }
      this.state.ready = true;
      this.renderAll();
      this.setStatus(this.state.chatConfigured ? "" : "Chat is currently unavailable.");
      return true;
    } catch (error) {
      this.state.ready = false;
      this.state.chatEnabled = false;
      this.state.canSave = false;
      this.state.warnings = [error.message || "Couldn't load the view."];
      this.renderWarnings();
      this.setStatus("Load error. Refresh this page to try again.");
      return false;
    } finally {
      this.setBusy(false);
    }
  };

  TerriassistantApp.prototype.requestJson = async function (url, body) {
    var options = { credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRFToken"] = this.state.csrfToken;
      options.body = JSON.stringify(body);
    }
    var response = await fetch(url, options);
    var data;
    try { data = await response.json(); } catch (error) {
      throw new Error("The server returned an unreadable response (HTTP " + response.status + "). Try again or refresh this page.");
    }
    if (!response.ok || !isObject(data) || data.success === false) {
      throw new Error((data && (data.message || data.error)) || "The request failed (HTTP " + response.status + ").");
    }
    return data;
  };

  // Storage can be unavailable in private browsing or full. It must not break
  // map loading, server saves or the current in-memory draft.
  TerriassistantApp.prototype.storageGet = function (key) {
    try { return window.localStorage.getItem(key); } catch (error) { this.state.storageAvailable = false; return null; }
  };
  TerriassistantApp.prototype.storageSet = function (key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { this.state.storageAvailable = false; }
  };
  TerriassistantApp.prototype.storageRemove = function (key) {
    try { window.localStorage.removeItem(key); } catch (error) { this.state.storageAvailable = false; }
  };
  TerriassistantApp.prototype.loadLocalDraft = function () {
    var stored = readJson(this.storageGet(this.draftKey()));
    if (!isObject(stored) || !isObject(stored.spec)) return null;
    return { spec: stored.spec, encoded: typeof stored.encoded === "string" ? stored.encoded : null };
  };
  TerriassistantApp.prototype.persistMessages = function () {
    this.storageSet(this.messageKey(), stringify(this.state.messages));
  };
  TerriassistantApp.prototype.persistDraft = function (encoded) {
    if (stringify(this.state.draftSpec) !== stringify(this.state.savedSpec)) {
      this.storageSet(this.draftKey(), stringify({ spec: this.state.draftSpec, encoded: encoded || null }));
    } else {
      this.storageRemove(this.draftKey());
    }
  };
  TerriassistantApp.prototype.messageKey = function () { return "terriassistant:messages:" + this.state.storageScope + ":" + this.viewId; };
  TerriassistantApp.prototype.draftKey = function () { return "terriassistant:draft:" + this.state.storageScope + ":" + this.viewId; };

  // ---- map iframe ------------------------------------------------------

  TerriassistantApp.prototype.buildShareUrl = function (encodedConfig) {
    var base = String(this.terriaInstanceUrl || "");
    try {
      var resolved = new URL(this.terriaInstanceUrl, window.location.href);
      resolved.hash = "";
      base = resolved.href;
    } catch (e) { /* keep as-is */ }
    return base + "#start=" + encodedConfig;
  };

  TerriassistantApp.prototype.reloadMap = function (encodedConfig) {
    if (!this.el.iframe || !encodedConfig) return;
    var newSrc = this.buildShareUrl(encodedConfig);
    this.showMapLoading();
    var iframe = this.el.iframe;
    iframe.src = "about:blank";
    window.setTimeout(function () { iframe.src = newSrc; }, 30);
    if (this.el.openWindow) this.el.openWindow.href = newSrc;
  };

  TerriassistantApp.prototype.showMapLoading = function () {
    if (this.el.mapLoading) this.el.mapLoading.classList.remove("ta-hidden");
  };
  TerriassistantApp.prototype.hideMapLoading = function () {
    if (this.el.mapLoading) this.el.mapLoading.classList.add("ta-hidden");
  };
  TerriassistantApp.prototype.toggleFullWidth = function () {
    document.querySelectorAll(".container, .container-fluid").forEach(function (c) {
      c.classList.toggle("ta-container-full");
    });
    this.root.classList.toggle("ta-app-full");
  };

  // ---- chat ------------------------------------------------------------

  TerriassistantApp.prototype.sendMessage = async function () {
    var content = (this.el.input.value || "").trim();
    if (!content || !this.state.chatEnabled || !this.state.ready || this.state.busy) return;
    this.setBusy(true);
    this.state.messages.push({ role: "user", content: content });
    this.persistMessages();
    this.el.input.value = "";
    this.renderMessages();
    this.setStatus("Thinking…", true);
    try {
      var data = await this.requestJson(this.urls.chat, {
        messages: this.state.messages.slice(-100), draft_spec: this.state.draftSpec
      });

      this.state.messages.push({ role: "assistant", content: data.assistant_message });
      this.persistMessages();
      if (isObject(data.next_spec) || data.next_spec === null) {
        this.state.draftSpec = data.next_spec || {};
      }
      this.state.warnings = Array.isArray(data.warnings) ? data.warnings : [];
      if (Array.isArray(data.suggested_prompts) && data.suggested_prompts.length) {
        this.state.suggestedPrompts = data.suggested_prompts;
      }
      if (data.summary) this.el.summary.textContent = data.summary;
      if (data.action === "apply_style" && data.encoded_config) {
        this.reloadMap(data.encoded_config);
        this.persistDraft(data.encoded_config);
      } else {
        this.persistDraft(data.encoded_config);
      }
      this.renderAll();
      this.setStatus("");
    } catch (error) {
      // Retry the same question without duplicating it in the conversation.
      this.state.messages.pop();
      this.persistMessages();
      if (!this.el.input.value) this.el.input.value = content;
      this.renderMessages();
      this.state.warnings = [error.message || "Couldn't update the map."];
      this.renderWarnings();
      this.setStatus("Update error.");
    } finally {
      this.setBusy(false);
    }
  };

  // ---- save / reset ----------------------------------------------------

  TerriassistantApp.prototype.saveDraft = async function () {
    if (!this.state.canSave || !this.state.ready || this.state.busy) return;
    this.setBusy(true);
    this.setStatus("Saving…", true);
    try {
      var data = await this.requestJson(this.urls.save, { draft_spec: this.state.draftSpec || {} });
      this.state.savedSpec = isObject(data.saved_spec) ? data.saved_spec : {};
      this.state.draftSpec = this.state.savedSpec;
      this.state.pendingLocalDraft = null;
      this.toggleBanner(false);
      this.storageRemove(this.draftKey());
      this.state.warnings = [];
      if (data.summary) this.el.summary.textContent = data.summary;
      if (data.encoded_config && this.el.openWindow) this.el.openWindow.href = this.buildShareUrl(data.encoded_config);
      this.renderAll();
      this.setStatus("Map saved.");
    } catch (error) {
      this.state.warnings = [error.message || "Couldn't save the map."];
      this.renderWarnings();
      this.setStatus("Save error.");
    } finally {
      this.setBusy(false);
    }
  };

  TerriassistantApp.prototype.resetToSaved = async function () {
    if (!this.state.ready || this.state.busy) return;
    // Keep the recoverable draft if the refresh fails.
    if (await this.loadBootstrap()) {
      this.state.draftSpec = this.state.savedSpec;
      this.state.pendingLocalDraft = null;
      this.toggleBanner(false);
      this.storageRemove(this.draftKey());
      if (this.state.bootstrap.encoded_config) this.reloadMap(this.state.bootstrap.encoded_config);
      this.renderAll();
    }
  };

  TerriassistantApp.prototype.resetToDefault = function () {
    if (!this.state.ready || this.state.busy) return;
    if (!window.confirm("Preview the default styling? Your saved map will stay unchanged until you choose Save.")) return;
    var data = this.state.bootstrap;
    this.state.draftSpec = isObject(data.default_spec) ? data.default_spec : {};
    this.state.pendingLocalDraft = null;
    this.toggleBanner(false);
    if (data.default_encoded_config) this.reloadMap(data.default_encoded_config);
    this.persistDraft(data.default_encoded_config);
    this.renderAll();
    this.setStatus("Default styling preview. The saved map has not changed.");
  };

  TerriassistantApp.prototype.usePendingDraft = function () {
    if (!this.state.ready || this.state.busy) return;
    var pending = this.state.pendingLocalDraft;
    if (!pending) return;
    this.state.draftSpec = pending.spec;
    this.state.pendingLocalDraft = null;
    this.toggleBanner(false);
    if (pending.encoded) this.reloadMap(pending.encoded);
    this.persistDraft(pending.encoded);
    this.renderAll();
  };
  TerriassistantApp.prototype.discardPendingDraft = function () {
    if (!this.state.ready || this.state.busy) return;
    this.state.pendingLocalDraft = null;
    this.state.draftSpec = this.state.savedSpec;
    this.toggleBanner(false);
    this.storageRemove(this.draftKey());
    if (this.state.bootstrap && this.state.bootstrap.encoded_config) {
      this.reloadMap(this.state.bootstrap.encoded_config);
    }
    this.renderAll();
  };

  // ---- chat housekeeping ----------------------------------------------

  TerriassistantApp.prototype.clearChat = function () {
    if (!this.state.messages.length || this.state.busy) return;
    if (!window.confirm("Clear the conversation? This can't be undone.")) return;
    this.state.messages = [];
    this.storageRemove(this.messageKey());
    this.renderMessages();
    this.setStatus("Chat cleared.");
  };

  // ---- rendering -------------------------------------------------------

  TerriassistantApp.prototype.renderAll = function () {
    this.renderWarnings();
    this.renderMessages();
    this.renderSuggestions();
    this.renderSummary();
    this.updateClearButton();
  };

  TerriassistantApp.prototype.renderWarnings = function () {
    var warnings = (this.state.warnings || []).slice();
    if (!this.state.storageAvailable) warnings.push("Browser storage is unavailable. Local drafts and chat will not survive a page reload.");
    this.el.warnings.innerHTML = warnings.map(function (w) {
      return '<div class="ta-warning">' + escapeHtml(w) + "</div>";
    }).join("");
  };

  TerriassistantApp.prototype.renderMessages = function () {
    var messages = this.state.messages;
    if (!messages.length) {
      this.el.messages.innerHTML =
        '<div class="ta-message ta-message-assistant ta-message-empty">' +
        escapeHtml("Tell me how to style the map. For example: \"color the regions by GDP with 5 classes\".") +
        "</div>";
      this.updateClearButton();
      return;
    }
    this.el.messages.innerHTML = messages.map(function (m) {
      var cls = m.role === "user" ? "ta-message ta-message-user" : "ta-message ta-message-assistant";
      return '<div class="' + cls + '">' + escapeHtml(m.content) + "</div>";
    }).join("");
    this.el.messages.scrollTop = this.el.messages.scrollHeight;
    this.updateClearButton();
  };

  TerriassistantApp.prototype.renderSuggestions = function () {
    var self = this;
    var suggestions = this.state.suggestedPrompts || [];
    this.el.suggestions.innerHTML = "";
    suggestions.forEach(function (s) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "ta-suggestion";
      button.textContent = s;
      button.addEventListener("click", function () {
        self.el.input.value = s;
        self.el.input.focus();
      });
      self.el.suggestions.appendChild(button);
    });
  };

  TerriassistantApp.prototype.renderSummary = function () {
    this.el.summary.textContent = describeSpec(this.state.draftSpec);
  };

  function describeSpec(spec) {
    if (isEmptyObj(spec)) return "Default map styling";
    var parts = [];
    if (spec.color_by) {
      var method = spec.color_method || "continuous";
      var bit = "colored by " + spec.color_by;
      if (method === "bins") bit += " (" + (spec.bins || "binned") + " classes)";
      else if (method === "categories") bit += " (by category)";
      if (spec.palette) bit += " · " + spec.palette;
      parts.push(bit);
    }
    if (spec.size_by) parts.push("sized by " + spec.size_by);
    if (spec.marker) parts.push("marker " + spec.marker);
    if (spec.marker_color) parts.push("markers " + spec.marker_color);
    if (spec.fill_color) parts.push("fill " + spec.fill_color);
    if (spec.stroke_color || spec.outline_color) parts.push("outline " + (spec.stroke_color || spec.outline_color));
    if (typeof spec.opacity === "number") parts.push("opacity " + spec.opacity);
    return parts.length ? parts.join(", ") : "Custom map styling";
  }

  TerriassistantApp.prototype.updateClearButton = function () {
    this.el.clearChat.disabled = this.state.busy || !this.state.messages.length;
  };
  TerriassistantApp.prototype.toggleBanner = function (visible) {
    this.el.banner.classList.toggle("ta-hidden", !visible);
  };
  TerriassistantApp.prototype.setStatus = function (message, loading) {
    this.el.status.textContent = message || "";
    if (loading) this.el.status.setAttribute("data-loading", "true");
    else this.el.status.removeAttribute("data-loading");
  };

  TerriassistantApp.prototype.setBusy = function (busy) {
    this.state.busy = busy;
    var unavailable = busy || !this.state.ready;
    this.el.input.disabled = unavailable || !this.state.chatEnabled;
    this.el.send.disabled = unavailable || !this.state.chatEnabled;
    this.el.save.classList.toggle("ta-hidden", !this.state.canSave);
    this.el.save.disabled = unavailable || !this.state.canSave;
    this.el.resetSaved.disabled = unavailable;
    this.el.resetDefault.disabled = unavailable;
    this.el.useDraft.disabled = unavailable;
    this.el.discardDraft.disabled = unavailable;
    this.updateClearButton();
    if (this.el.login) {
      this.el.login.classList.toggle("ta-hidden", !this.state.ready || !this.state.chatConfigured || this.state.chatEnabled);
      if (this.state.loginUrl) this.el.login.querySelector("a").href = this.state.loginUrl;
    }
  };

  function start() {
    var root = document.getElementById("terriassistant-app");
    if (!root || root.dataset.terriassistantInitialized) return;
    root.dataset.terriassistantInitialized = "true";
    new TerriassistantApp(root).init();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
