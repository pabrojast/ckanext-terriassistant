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
      this.el.iframe.addEventListener("load", function () { self.hideMapLoading(); });
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
    this.setStatus("Loading map…", true);
    try {
      var response = await fetch(this.urls.bootstrap, { credentials: "same-origin" });
      var data = await response.json();
      if (!response.ok) throw new Error(data.message || "Couldn't load the view.");

      this.state.bootstrap = data;
      this.state.chatEnabled = !!data.chat_enabled;
      this.state.canSave = !!data.can_save;
      this.state.savedSpec = isObject(data.saved_spec) ? data.saved_spec : {};
      this.state.warnings = Array.isArray(data.warnings) ? data.warnings.slice() : [];
      this.state.suggestedPrompts = Array.isArray(data.suggested_prompts) ? data.suggested_prompts : [];
      this.state.messages = readJson(window.localStorage.getItem(this.messageKey())) || [];

      var activeSpec = isObject(data.active_spec) ? data.active_spec : this.state.savedSpec;
      var localDraft = this.loadLocalDraft();
      if (localDraft && stringify(localDraft.spec) !== stringify(this.state.savedSpec)) {
        this.state.pendingLocalDraft = localDraft;
        this.state.draftSpec = this.state.savedSpec;
        this.toggleBanner(true);
      } else {
        this.state.draftSpec = activeSpec;
        this.toggleBanner(false);
        if (localDraft) window.localStorage.removeItem(this.draftKey());
      }

      // The server already rendered the iframe with `active_spec`. Only force a
      // reload if the template fell back to a bare/empty map.
      if (data.encoded_config && this.el.iframe &&
          String(this.el.iframe.src || "").indexOf("#start=") === -1) {
        this.reloadMap(data.encoded_config);
      } else if (data.encoded_config && this.el.openWindow) {
        this.el.openWindow.href = this.buildShareUrl(data.encoded_config);
      }

      this.el.input.disabled = !this.state.chatEnabled;
      this.el.send.disabled = !this.state.chatEnabled;
      this.el.save.classList.toggle("ta-hidden", !this.state.canSave);
      this.renderAll();
      this.setStatus(this.state.chatEnabled ? "" : "Configure the AI service to use the chat.");
    } catch (error) {
      this.state.warnings = [error.message || "Couldn't load the view."];
      this.renderWarnings();
      this.setStatus("Load error.");
    }
  };

  TerriassistantApp.prototype.loadLocalDraft = function () {
    var stored = readJson(window.localStorage.getItem(this.draftKey()));
    if (!isObject(stored) || !isObject(stored.spec)) return null;
    return { spec: stored.spec, encoded: typeof stored.encoded === "string" ? stored.encoded : null };
  };

  TerriassistantApp.prototype.persistMessages = function () {
    window.localStorage.setItem(this.messageKey(), stringify(this.state.messages));
  };
  TerriassistantApp.prototype.persistDraft = function (encoded) {
    if (!isEmptyObj(this.state.draftSpec)) {
      window.localStorage.setItem(this.draftKey(), stringify({ spec: this.state.draftSpec, encoded: encoded || null }));
    } else {
      window.localStorage.removeItem(this.draftKey());
    }
  };
  TerriassistantApp.prototype.messageKey = function () { return "terriassistant:messages:" + this.viewId; };
  TerriassistantApp.prototype.draftKey = function () { return "terriassistant:draft:" + this.viewId; };

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
    if (!content || !this.state.chatEnabled) return;
    this.state.messages.push({ role: "user", content: content });
    this.persistMessages();
    this.el.input.value = "";
    this.renderMessages();
    this.setStatus("Thinking…", true);
    this.el.send.disabled = true;
    try {
      var response = await fetch(this.urls.chat, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: this.state.messages, draft_spec: this.state.draftSpec })
      });
      var data = await response.json();
      if (!response.ok) throw new Error(data.message || "Couldn't update the map.");

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
        this.persistDraft(null);
      }
      this.renderAll();
      this.setStatus("");
    } catch (error) {
      this.state.warnings = [error.message || "Couldn't update the map."];
      this.renderWarnings();
      this.setStatus("Update error.");
    } finally {
      this.el.send.disabled = !this.state.chatEnabled;
    }
  };

  // ---- save / reset ----------------------------------------------------

  TerriassistantApp.prototype.saveDraft = async function () {
    if (!this.state.canSave) return;
    this.setStatus("Saving…", true);
    try {
      var response = await fetch(this.urls.save, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_spec: this.state.draftSpec || {} })
      });
      var data = await response.json();
      if (!response.ok) throw new Error(data.message || "Couldn't save the map.");
      this.state.savedSpec = isObject(data.saved_spec) ? data.saved_spec : {};
      this.state.draftSpec = this.state.savedSpec;
      this.state.pendingLocalDraft = null;
      this.toggleBanner(false);
      window.localStorage.removeItem(this.draftKey());
      this.state.warnings = [];
      if (data.summary) this.el.summary.textContent = data.summary;
      if (data.encoded_config && this.el.openWindow) this.el.openWindow.href = this.buildShareUrl(data.encoded_config);
      this.renderAll();
      this.setStatus("Map saved.");
    } catch (error) {
      this.state.warnings = [error.message || "Couldn't save the map."];
      this.renderWarnings();
      this.setStatus("Save error.");
    }
  };

  TerriassistantApp.prototype.resetToSaved = async function () {
    this.state.draftSpec = this.state.savedSpec;
    this.state.pendingLocalDraft = null;
    this.toggleBanner(false);
    window.localStorage.removeItem(this.draftKey());
    this.setStatus("Reloading saved map…", true);
    await this.loadBootstrap();
    // loadBootstrap won't re-render the iframe (it's still showing the draft),
    // so force it from the freshly fetched config.
    if (this.state.bootstrap && this.state.bootstrap.encoded_config) {
      this.reloadMap(this.state.bootstrap.encoded_config);
    }
  };

  TerriassistantApp.prototype.resetToDefault = function () {
    if (!window.confirm("Discard the current styling and reload the map with default styling?")) return;
    window.localStorage.removeItem(this.draftKey());
    window.location.reload();
  };

  TerriassistantApp.prototype.usePendingDraft = function () {
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
    this.state.pendingLocalDraft = null;
    this.state.draftSpec = this.state.savedSpec;
    this.toggleBanner(false);
    window.localStorage.removeItem(this.draftKey());
    if (this.state.bootstrap && this.state.bootstrap.encoded_config) {
      this.reloadMap(this.state.bootstrap.encoded_config);
    }
    this.renderAll();
  };

  // ---- chat housekeeping ----------------------------------------------

  TerriassistantApp.prototype.clearChat = function () {
    if (!this.state.messages.length) return;
    if (!window.confirm("Clear the conversation? This can't be undone.")) return;
    this.state.messages = [];
    window.localStorage.removeItem(this.messageKey());
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
    var warnings = this.state.warnings || [];
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
    var spec = !isEmptyObj(this.state.draftSpec) ? this.state.draftSpec
      : (!isEmptyObj(this.state.savedSpec) ? this.state.savedSpec : null);
    this.el.summary.textContent = describeSpec(spec);
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
    this.el.clearChat.disabled = !this.state.messages.length;
  };
  TerriassistantApp.prototype.toggleBanner = function (visible) {
    this.el.banner.classList.toggle("ta-hidden", !visible);
  };
  TerriassistantApp.prototype.setStatus = function (message, loading) {
    this.el.status.textContent = message || "";
    if (loading) this.el.status.setAttribute("data-loading", "true");
    else this.el.status.removeAttribute("data-loading");
  };

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("terriassistant-app");
    if (!root) return;
    new TerriassistantApp(root).init();
  });
})();
