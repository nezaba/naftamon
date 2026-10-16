(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const API = "/api/v1/notifications";
  const note = {
    settings: null, rules: [], channels: [], history: [], historyTotal: 0,
    rulePage: 0, channelPage: 0, historyPage: 0, pageSize: 20,
    route: "monitor", loaded: false, loading: false, reloadAfterCurrent: false, confirmAction: null,
    testChannelId: null, toastTimer: null, channelHeaders: [],
    backupDocument: null, backupPreview: null, previousBackup: null,
  };
  const FIELD_OPTIONS = [
    ["source", "Fuente NaftaMon"], ["source_id", "ID local de fuente"], ["server", "Servidor Thruk"],
    ["backend", "Backend"], ["type", "Tipo de alerta"], ["host", "Host"], ["service", "Servicio"],
    ["state", "Estado"], ["status_information", "Status Information"], ["hard", "Hard state"], ["acknowledged", "Acknowledgement"], ["downtime", "Downtime"],
  ];
  const OP_OPTIONS = [
    ["equals", "Igual a"], ["not_equals", "Distinto de"], ["contains", "Contiene"],
    ["not_contains", "No contiene"], ["starts_with", "Comienza con"], ["ends_with", "Termina con"], ["wildcard", "Wildcard (*, ?)"],
  ];
  const STATUS_INFORMATION_OP_OPTIONS = [
    ["contains", "Contiene"], ["not_contains", "No contiene"],
    ["equals", "Es exactamente igual"], ["not_equals", "No es exactamente igual"],
  ];
  const EVENT_LABELS = { opened: "Nueva alerta", state_changed: "Cambio de estado", recovered: "Recuperación", reminder: "Recordatorio" };
  const STATUS_LABELS = { pending: "Pendiente", in_flight: "En curso", sent: "Enviado", failed: "Fallido", cancelled: "Cancelado" };
  const PROVIDER_LABELS = { telegram: "Telegram", gotify: "Gotify", ntfy: "ntfy", webhook: "Webhook HTTP" };

  function node(tag, className = "", text = null) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== null && text !== undefined) item.textContent = String(text);
    return item;
  }
  function button(text, className, action, aria = "") {
    const item = node("button", className, text); item.type = "button"; item.dataset.action = action;
    if (aria) item.setAttribute("aria-label", aria);
    return item;
  }
  function localDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("es-AR", { dateStyle: "medium", timeStyle: "short" }).format(date);
  }
  function intValue(selector, fallback = 0) {
    const value = Number($(selector).value);
    return Number.isInteger(value) ? value : fallback;
  }
  function apiErrorMessage(payload, fallback) {
    const detail = payload && payload.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((entry) => entry.msg || JSON.stringify(entry)).join(" · ");
    if (payload && typeof payload.error === "string") return payload.error;
    return fallback;
  }
  async function apiRequest(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    const init = { method: options.method || "GET", headers, cache: "no-store" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const response = await fetch(`${API}${path}`, init);
    let payload = null;
    if (response.status !== 204) {
      const raw = await response.text();
      try { payload = raw ? JSON.parse(raw) : null; } catch { payload = { detail: raw.slice(0, 500) }; }
    }
    if (!response.ok) {
      const error = new Error(apiErrorMessage(payload, `HTTP ${response.status}`));
      error.status = response.status;
      error.retryAfter = response.headers.get("Retry-After");
      error.payload = payload;
      throw error;
    }
    return payload;
  }
  async function fetchAll(path) {
    const limit = 500;
    const first = await apiRequest(`${path}?limit=${limit}&offset=0`);
    if (!first || !Array.isArray(first.items)) throw new Error("La API devolvió una lista con formato inesperado.");
    const result = new Map(first.items.map((entry) => [String(entry.id), entry]));
    const total = Number(first.total) || 0;
    for (let offset = first.items.length; offset < total; offset += limit) {
      const page = await apiRequest(`${path}?limit=${limit}&offset=${offset}`);
      if (!Array.isArray(page.items)) throw new Error("La API devolvió una página inválida.");
      for (const entry of page.items) result.set(String(entry.id), entry);
      if (page.items.length === 0) break;
    }
    return [...result.values()];
  }
  function showError(message) {
    $("#notification-error-text").textContent = message;
    $("#notification-error").hidden = false;
    $("#notification-notice").hidden = true;
  }
  function clearError() { $("#notification-error").hidden = true; }
  function showNotice(message, kind = "success") {
    const item = $("#notification-notice");
    item.className = `notification-notice ${kind === "error" ? "is-error" : "is-success"}`;
    item.textContent = message; item.hidden = false;
    if (note.toastTimer) window.clearTimeout(note.toastTimer);
    note.toastTimer = window.setTimeout(() => { item.hidden = true; }, 6500);
  }
  function setBusy(buttonElement, busy, busyText = "Guardando…") {
    if (!buttonElement) return;
    if (busy) { buttonElement.dataset.originalText = buttonElement.textContent; buttonElement.textContent = busyText; }
    else if (buttonElement.dataset.originalText) { buttonElement.textContent = buttonElement.dataset.originalText; delete buttonElement.dataset.originalText; }
    buttonElement.disabled = busy;
  }
  function syncOptions(select, values, label) {
    const selected = select.value;
    const options = [{ value: "", label }, ...values];
    const signature = JSON.stringify(options);
    if (select.dataset.optionsSignature === signature) return;
    select.replaceChildren(...options.map(({ value, label: text }) => {
      const option = node("option", "", text); option.value = String(value); return option;
    }));
    select.dataset.optionsSignature = signature;
    select.value = options.some((option) => String(option.value) === selected) ? selected : "";
  }
  function updateHistoryFilterOptions() {
    const rules = note.rules.map((rule) => ({ value: rule.id, label: rule.name }));
    const channels = note.channels.map((channel) => ({ value: channel.id, label: `${channel.name} · ${PROVIDER_LABELS[channel.provider] || channel.provider}` }));
    syncOptions($("#history-rule-filter"), rules, "Todas");
    syncOptions($("#history-channel-filter"), channels, "Todos");
  }
  function setEngineStatus() {
    const enabled = Boolean(note.settings && note.settings.enabled);
    const status = $("#notification-engine-status");
    status.dataset.enabled = String(enabled);
    $("#notification-engine-label").textContent = enabled ? "Motor habilitado" : "Motor deshabilitado";
    $("#settings-engine-enabled").checked = enabled;
    $("#settings-engine-value").textContent = enabled ? "Habilitado" : "Deshabilitado";
  }
  async function loadNotifications(force = false) {
    if (note.loading) { if (force) note.reloadAfterCurrent = true; return; }
    if (note.loaded && !force) return;
    note.loading = true;
    if (!note.loaded) $("#notification-loading").hidden = false;
    clearError();
    try {
      const [settings, rules, channels, previousBackup] = await Promise.all([
        apiRequest("/settings"), fetchAll("/rules"), fetchAll("/channels"), apiRequest("/backup/previous"),
      ]);
      note.settings = settings; note.rules = rules; note.channels = channels; note.previousBackup = previousBackup; note.loaded = true;
      setEngineStatus();
      $("#settings-retention-days").value = String(settings.history_retention_days);
      $("#settings-updated-at").textContent = `Guardado ${localDate(settings.updated_at)}`;
      renderPreviousBackup(previousBackup);
      updateHistoryFilterOptions(); renderRules(); renderChannels();
      if (note.route === "notifications/history") await loadDeliveryHistory();
      $("#notifications-footer-update").textContent = `Actualizado ${localDate(new Date().toISOString())}`;
    } catch (error) {
      showError(error instanceof Error ? error.message : "No se pudo cargar la configuración de notificaciones.");
    } finally {
      note.loading = false; $("#notification-loading").hidden = true;
      if (note.reloadAfterCurrent) { note.reloadAfterCurrent = false; loadNotifications(true); }
    }
  }
  function routeFromHash() {
    const match = /^#notifications\/(rules|channels|history|settings)$/.exec(window.location.hash);
    return match ? `notifications/${match[1]}` : "monitor";
  }
  function applyRoute() {
    note.route = routeFromHash();
    const notifications = note.route.startsWith("notifications/");
    $("#monitor-content").hidden = notifications;
    $("#notifications-content").hidden = !notifications;
    $("#nav-monitor").classList.toggle("is-active", !notifications);
    $("#nav-notifications").classList.toggle("is-active", notifications);
    $("#nav-monitor").setAttribute("aria-current", notifications ? "false" : "page");
    $("#nav-notifications").setAttribute("aria-current", notifications ? "page" : "false");
    $("#notification-links").hidden = !notifications;
    const view = notifications ? note.route.split("/")[1] : "rules";
    for (const link of $$("[data-notification-view]")) {
      const active = link.dataset.notificationView === view;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    }
    for (const panel of $$("[data-notification-panel]")) panel.hidden = !notifications || panel.dataset.notificationPanel !== view;
    if (notifications) {
      if (!note.loaded) loadNotifications();
      else if (view === "history") loadDeliveryHistory().catch((error) => showError(error.message));
    }
  }

  function ruleConditionLabel(conditions) {
    if (!conditions || !Array.isArray(conditions.conditions)) return "Condiciones no disponibles";
    return conditions.conditions.map((item) => {
      const label = FIELD_OPTIONS.find(([key]) => key === item.field)?.[1] || item.field;
      const value = typeof item.value === "boolean" ? (item.value ? "Sí" : "No") : item.value;
      return `${label} ${item.operator.replaceAll("_", " ")} “${value}”`;
    }).join(conditions.operator === "OR" ? " · O · " : " · Y · ");
  }
  function createRuleCard(rule) {
    const card = node("article", "notification-card rule-card"); card.dataset.ruleId = String(rule.id);
    const top = node("div", "notification-card-top");
    const titleBox = node("div", "notification-card-title");
    titleBox.append(node("h3", "", rule.name));
    if (rule.description) titleBox.append(node("p", "notification-description", rule.description));
    const enabledWrap = node("label", "switch-control card-switch"); enabledWrap.setAttribute("aria-label", `${rule.enabled ? "Deshabilitar" : "Habilitar"} regla ${rule.name}`);
    const enabled = node("input"); enabled.type = "checkbox"; enabled.checked = Boolean(rule.enabled); enabled.dataset.action = "toggle-rule"; enabled.dataset.ruleId = String(rule.id);
    enabledWrap.append(enabled, node("span", "switch-track", ""));
    const enabledText = node("span", `rule-enabled-label ${rule.enabled ? "is-enabled" : ""}`, rule.enabled ? "Activa" : "Desactivada");
    top.append(titleBox, enabledText, enabledWrap); card.append(top);
    const condition = node("div", "rule-condition-summary", ruleConditionLabel(rule.conditions)); card.append(condition);
    const meta = node("div", "rule-chip-row");
    for (const event of rule.events || []) meta.append(node("span", "notification-chip", EVENT_LABELS[event] || event));
    const channelNames = (rule.channels || []).map((channel) => `${channel.name}${channel.enabled ? "" : " (deshabilitado)"}`);
    meta.append(node("span", "notification-chip is-neutral", channelNames.length ? `${channelNames.length} canal${channelNames.length === 1 ? "" : "es"}: ${channelNames.join(", ")}` : "Sin canales asociados"));
    card.append(meta);
    const stats = node("div", "rule-stats");
    stats.append(node("span", "", `Coincidencias ${rule.match_count ?? 0}`));
    stats.append(node("span", "", `Última coincidencia ${localDate(rule.last_matched_at)}`));
    stats.append(node("span", "", `Última activación ${localDate(rule.last_triggered_at)}`)); card.append(stats);
    const actions = node("div", "notification-card-actions");
    actions.append(button("Test Rule", "secondary-action small-action", "test-rule"));
    actions.append(button("Duplicar", "secondary-action small-action", "duplicate-rule"));
    actions.append(button("Editar", "secondary-action small-action", "edit-rule"));
    actions.append(button("Eliminar", "text-danger-action small-action", "delete-rule"));
    card.append(actions); return card;
  }
  function renderRules() {
    const total = note.rules.length; const pages = Math.max(1, Math.ceil(total / note.pageSize));
    note.rulePage = Math.min(note.rulePage, pages - 1);
    const pageItems = note.rules.slice(note.rulePage * note.pageSize, (note.rulePage + 1) * note.pageSize);
    $("#rules-list").replaceChildren(...pageItems.map(createRuleCard));
    $("#rules-empty").hidden = total !== 0;
    $("#rules-total").textContent = `${total} ${total === 1 ? "regla" : "reglas"}`;
    const pager = $("#rules-pagination"); pager.hidden = total <= note.pageSize;
    $("#rules-page-label").textContent = `Página ${note.rulePage + 1} de ${pages}`;
    $("#rules-prev").disabled = note.rulePage <= 0; $("#rules-next").disabled = note.rulePage >= pages - 1;
  }
  function renderChannels() {
    const total = note.channels.length; const pages = Math.max(1, Math.ceil(total / note.pageSize));
    note.channelPage = Math.min(note.channelPage, pages - 1);
    const pageItems = note.channels.slice(note.channelPage * note.pageSize, (note.channelPage + 1) * note.pageSize);
    $("#channels-list").replaceChildren(...pageItems.map(createChannelCard));
    $("#channels-empty").hidden = total !== 0;
    $("#channels-total").textContent = `${total} ${total === 1 ? "canal" : "canales"}`;
    const pager = $("#channels-pagination"); pager.hidden = total <= note.pageSize;
    $("#channels-page-label").textContent = `Página ${note.channelPage + 1} de ${pages}`;
    $("#channels-prev").disabled = note.channelPage <= 0; $("#channels-next").disabled = note.channelPage >= pages - 1;
  }
  function channelConfigSummary(channel) {
    const config = channel.config || {};
    switch (channel.provider) {
      case "telegram": return `Chat ID · ${config.chat_id || "no configurado"}`;
      case "gotify": return `${config.base_url || "URL no configurada"} · Prioridad ${config.priority ?? 5}`;
      case "ntfy": return `${config.base_url || "URL no configurada"} / ${config.topic || "topic no configurado"} · Prioridad ${config.priority ?? 3}`;
      case "webhook": return `${config.url || "URL no configurada"} · POST JSON · ${(config.header_names || []).length} header(s)`;
      default: return "Configuración disponible";
    }
  }
  function createChannelCard(channel) {
    const card = node("article", "notification-card channel-card"); card.dataset.channelId = String(channel.id);
    const top = node("div", "notification-card-top"); const title = node("div", "notification-card-title");
    title.append(node("span", "provider-label", PROVIDER_LABELS[channel.provider] || channel.provider), node("h3", "", channel.name));
    const switchWrap = node("label", "switch-control card-switch"); switchWrap.setAttribute("aria-label", `${channel.enabled ? "Deshabilitar" : "Habilitar"} canal ${channel.name}`);
    const toggle = node("input"); toggle.type = "checkbox"; toggle.checked = Boolean(channel.enabled); toggle.dataset.action = "toggle-channel"; toggle.dataset.channelId = String(channel.id);
    switchWrap.append(toggle, node("span", "switch-track", ""));
    top.append(title, node("span", `channel-state-label ${channel.enabled ? "is-enabled" : ""}`, channel.enabled ? "Habilitado" : "Deshabilitado"), switchWrap); card.append(top);
    card.append(node("p", "channel-config-summary", channelConfigSummary(channel)));
    const flags = node("div", "rule-chip-row");
    flags.append(node("span", `notification-chip ${channel.secret_configured || channel.has_secret ? "is-secure" : "is-neutral"}`, channel.secret_configured || channel.has_secret ? "Credencial configurada" : (channel.provider === "webhook" || channel.provider === "ntfy") ? "Sin credencial" : "Falta configurar credencial"));
    flags.append(node("span", "notification-chip is-neutral", `Timeout ${channel.timeout_seconds}s`));
    if (channel.last_result) flags.append(node("span", `notification-chip ${channel.last_result === "sent" ? "is-success" : channel.last_result === "failed" ? "is-failure" : "is-neutral"}`, `Último resultado: ${channel.last_result === "sent" ? "enviado" : channel.last_result === "failed" ? "fallido" : channel.last_result}`));
    card.append(flags);
    const last = node("div", "channel-last-result");
    last.append(node("span", "", `Último éxito: ${localDate(channel.last_success_at)}`));
    if (channel.last_error) last.append(node("p", "channel-last-error", channel.last_error));
    card.append(last);
    const actions = node("div", "notification-card-actions");
    const test = button(note.testChannelId === channel.id ? "Probando…" : "Enviar prueba", "secondary-action small-action", "test-channel"); test.disabled = note.testChannelId === channel.id;
    actions.append(test, button("Editar", "secondary-action small-action", "edit-channel"), button("Eliminar", "text-danger-action small-action", "delete-channel"));
    card.append(actions); return card;
  }

  function makeConditionRow(condition = { field: "state", operator: "equals", value: "CRITICAL" }) {
    const row = node("div", "condition-row");
    const field = node("select", "condition-field"); field.setAttribute("aria-label", "Campo de condición");
    for (const [value, label] of FIELD_OPTIONS) { const option = node("option", "", label); option.value = value; field.append(option); }
    field.value = FIELD_OPTIONS.some(([value]) => value === condition.field) ? condition.field : "state";
    const operation = node("select", "condition-operation"); operation.setAttribute("aria-label", "Operador de condición");
    const valueWrap = node("div", "condition-value-wrap");
    const remove = button("×", "remove-condition", "remove-condition", "Quitar condición");
    row.append(field, operation, valueWrap, remove);
    const syncValue = (selectedValue) => {
      const boolean = ["hard", "acknowledged", "downtime"].includes(field.value);
      const options = field.value === "status_information"
        ? STATUS_INFORMATION_OP_OPTIONS
        : OP_OPTIONS.filter(([key]) => !boolean || ["equals", "not_equals"].includes(key));
      operation.replaceChildren(...options.map(([value, label]) => { const option = node("option", "", label); option.value = value; return option; }));
      operation.value = [...operation.options].some((option) => option.value === condition.operator) ? condition.operator : "equals";
      const current = selectedValue !== undefined ? selectedValue : condition.value;
      valueWrap.replaceChildren();
      if (boolean) {
        const select = node("select", "condition-value"); select.setAttribute("aria-label", "Valor booleano");
        for (const [value, label] of [["true", "Sí"], ["false", "No"]]) { const option = node("option", "", label); option.value = value; select.append(option); }
        select.value = String(Boolean(current === true || current === "true")); valueWrap.append(select);
      } else {
        const input = node("input", "condition-value"); input.type = "text"; input.maxLength = 256; input.placeholder = field.value === "status_information" ? "Texto de Status Information" : "Valor de coincidencia"; input.setAttribute("aria-label", "Valor de condición");
        input.value = String(current ?? ""); valueWrap.append(input);
      }
    };
    syncValue(condition.value);
    field.addEventListener("change", () => { condition.field = field.value; condition.operator = field.value === "status_information" ? "contains" : "equals"; condition.value = ["hard", "acknowledged", "downtime"].includes(field.value) ? false : ""; syncValue(condition.value); });
    return row;
  }
  function renderConditionList(group = { operator: "AND", conditions: [] }) {
    const list = $("#rule-condition-list");
    const conditions = Array.isArray(group.conditions) ? group.conditions : [];
    const values = conditions.length ? conditions : [{ field: "state", operator: "equals", value: "CRITICAL" }];
    list.replaceChildren(...values.map(makeConditionRow));
    $("#rule-condition-operator").value = group.operator || "AND";
  }
  function readConditions() {
    const rows = $$(".condition-row", $("#rule-condition-list"));
    if (!rows.length || rows.length > 32) throw new Error("Agrega entre 1 y 32 condiciones.");
    const conditions = rows.map((row) => {
      const field = $(".condition-field", row).value;
      const operation = $(".condition-operation", row).value;
      const raw = $(".condition-value", row).value;
      if (raw.trim() === "") throw new Error("Completa el valor de cada condición.");
      const value = ["hard", "acknowledged", "downtime"].includes(field) ? raw === "true" : raw.trim();
      return { field, operator: operation, value };
    });
    return { operator: $("#rule-condition-operator").value, conditions };
  }
  function renderRuleChannelChoices(selected = []) {
    const container = $("#rule-channel-choices");
    if (!note.channels.length) { container.replaceChildren(node("p", "field-hint", "No hay canales configurados. Guarda la regla y crea un canal en la sección Channels.")); return; }
    container.replaceChildren(...note.channels.map((channel) => {
      const label = node("label", `channel-choice${channel.enabled ? "" : " is-disabled"}`);
      const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.value = String(channel.id); checkbox.checked = selected.includes(channel.id); checkbox.dataset.channelChoice = "true";
      label.append(checkbox, node("span", "channel-choice-name", channel.name), node("small", "", `${PROVIDER_LABELS[channel.provider] || channel.provider}${channel.enabled ? "" : " · deshabilitado"}`));
      return label;
    }));
  }

  function clearFormError(selector) { const item = $(selector); item.hidden = true; item.textContent = ""; }
  function setFormError(selector, message) { const item = $(selector); item.textContent = message; item.hidden = false; }
  function openRuleEditor(rule = null) {
    const form = $("#rule-form"); form.reset(); clearFormError("#rule-form-error");
    $("#rule-id").value = rule ? String(rule.id) : "";
    $("#rule-dialog-title").textContent = rule ? "Editar regla" : "Nueva regla";
    $("#rule-name").value = rule?.name || "";
    $("#rule-description").value = rule?.description || "";
    $("#rule-enabled").checked = Boolean(rule?.enabled);
    $("#rule-cooldown").value = String(rule?.cooldown_seconds ?? 0);
    $("#rule-reminder").value = rule?.reminder_interval_seconds == null ? "" : String(rule.reminder_interval_seconds);
    $("#rule-subject-template").value = rule?.subject_template || "NaftaMon {event_type}: {state} {host}";
    $("#rule-body-template").value = rule?.body_template || "{event_type}: {host} {service} {state} ({server}, {source})";
    const events = new Set(rule?.events || ["opened", "state_changed"]);
    for (const checkbox of $$("input[name='rule-event']")) checkbox.checked = events.has(checkbox.value);
    renderConditionList(rule?.conditions || { operator: "AND", conditions: [] });
    renderRuleChannelChoices(rule?.channel_ids || []);
    $("#rule-dialog").showModal(); $("#rule-name").focus();
  }
  function collectRuleWrite() {
    const name = $("#rule-name").value.trim();
    if (!name) throw new Error("Escribe un nombre para la regla.");
    const events = $$("input[name='rule-event']:checked").map((item) => item.value);
    if (!events.length) throw new Error("Selecciona al menos un tipo de evento.");
    const channels = $$("[data-channel-choice]:checked").map((item) => Number(item.value));
    const reminderValue = $("#rule-reminder").value.trim();
    const reminder = reminderValue === "" ? null : Number(reminderValue);
    if (reminder !== null && (!Number.isInteger(reminder) || reminder <= 0)) throw new Error("El intervalo de recordatorio debe ser un entero positivo.");
    return {
      name, description: $("#rule-description").value.trim(), enabled: $("#rule-enabled").checked,
      conditions: readConditions(), events, channel_ids: channels,
      cooldown_seconds: intValue("#rule-cooldown", 0), reminder_interval_seconds: reminder,
      subject_template: $("#rule-subject-template").value,
      body_template: $("#rule-body-template").value,
    };
  }
  async function saveRule(event) {
    event.preventDefault(); clearFormError("#rule-form-error");
    const save = $("#save-rule");
    try {
      const body = collectRuleWrite();
      setBusy(save, true);
      const id = $("#rule-id").value;
      const saved = await apiRequest(id ? `/rules/${id}` : "/rules", { method: id ? "PUT" : "POST", body });
      $("#rule-dialog").close();
      await loadNotifications(true);
      const index = note.rules.findIndex((rule) => String(rule.id) === String(saved.id));
      if (index >= 0) { note.rulePage = Math.floor(index / note.pageSize); renderRules(); }
      showNotice(`Regla “${saved.name}” guardada.`);
    } catch (error) { setFormError("#rule-form-error", error instanceof Error ? error.message : "No se pudo guardar la regla."); }
    finally { setBusy(save, false, "Guardar regla"); }
  }
  async function testRule(rule) {
    const dialog = $("#rule-test-dialog");
    $("#rule-test-title").textContent = `Test Rule · ${rule.name}`;
    $("#rule-test-summary").textContent = "Evaluando alertas actuales y frescas…";
    $("#rule-test-summary").classList.remove("is-error");
    $("#rule-test-results").replaceChildren(); dialog.showModal();
    try {
      const result = await apiRequest(`/rules/${rule.id}/test`, { method: "POST" });
      if (result.dry_run !== true || result.sent !== false) throw new Error("El backend no confirmó el modo dry-run.");
      $("#rule-test-summary").textContent = `${result.match_count} coincidencias entre ${result.total_examined} alertas actuales y frescas. No se enviaron notificaciones.`;
      const matches = Array.isArray(result.matches) ? result.matches : [];
      if (!matches.length) $("#rule-test-results").append(node("p", "dry-run-empty", "Ninguna alerta actual coincide con esta regla."));
      for (const match of matches) {
        const row = node("div", "dry-run-row");
        const place = match.service ? `${match.host} · ${match.service}` : `${match.host} · host`;
        row.append(node("strong", "", place), node("span", "", `${match.state} · ${match.server} · ${match.backend || "sin backend"} · fuente #${match.source_id}`));
        $("#rule-test-results").append(row);
      }
    } catch (error) {
      $("#rule-test-summary").textContent = error instanceof Error ? error.message : "No se pudo evaluar la regla.";
      $("#rule-test-summary").classList.add("is-error");
    }
  }

  function currentChannel() { return note.channels.find((channel) => String(channel.id) === $("#channel-id").value) || null; }
  function setProviderFields() {
    const selected = $("#channel-provider").value;
    for (const panel of $$("[data-provider-fields]")) panel.hidden = panel.dataset.providerFields !== selected;
  }
  function setSecretStatus(provider, configured) {
    const text = configured ? "Configurado · vacío = conservar" : "No configurado";
    for (const item of $$(`[data-secret-state='${provider}']`)) item.textContent = text;
  }
  function makeWebhookHeaderRow(name = "", secretValue = "") {
    const row = node("div", "webhook-header-row");
    const key = node("input"); key.type = "text"; key.maxLength = 128; key.placeholder = "Nombre del header"; key.setAttribute("aria-label", "Nombre del header HTTP"); key.value = name;
    const value = node("input"); value.type = "password"; value.maxLength = 2048; value.autocomplete = "new-password"; value.placeholder = secretValue ? "Valor secreto (vacío = conservar)" : "Valor secreto opcional"; value.setAttribute("aria-label", "Valor secreto del header"); value.value = ""; value.spellcheck = false;
    const remove = button("×", "remove-condition", "remove-webhook-header", "Quitar header");
    row.append(key, value, remove); return row;
  }
  function openChannelEditor(channel = null) {
    const form = $("#channel-form"); form.reset(); clearFormError("#channel-form-error");
    $("#channel-id").value = channel ? String(channel.id) : "";
    $("#channel-dialog-title").textContent = channel ? "Editar canal" : "Nuevo canal";
    $("#channel-name").value = channel?.name || "";
    $("#channel-provider").value = channel?.provider || "telegram";
    $("#channel-enabled").checked = Boolean(channel?.enabled);
    $("#channel-timeout").value = String(channel?.timeout_seconds ?? 10);
    $("#channel-rate-limit").value = String(channel?.rate_limit_seconds ?? 1);
    const config = channel?.config || {};
    $("#telegram-chat-id").value = channel?.provider === "telegram" ? String(config.chat_id || "") : "";
    $("#telegram-bot-token").value = "";
    $("#gotify-base-url").value = channel?.provider === "gotify" ? config.base_url || "" : "";
    $("#gotify-priority").value = String(channel?.provider === "gotify" ? config.priority ?? 5 : 5);
    $("#gotify-app-token").value = "";
    $("#ntfy-base-url").value = channel?.provider === "ntfy" ? config.base_url || "" : "";
    $("#ntfy-topic").value = channel?.provider === "ntfy" ? config.topic || "" : "";
    $("#ntfy-priority").value = String(channel?.provider === "ntfy" ? config.priority ?? 3 : 3);
    $("#ntfy-access-token").value = ""; $("#ntfy-username").value = ""; $("#ntfy-password").value = "";
    $("#webhook-url").value = channel?.provider === "webhook" ? config.url || "" : "";
    const headers = channel?.provider === "webhook" && Array.isArray(config.header_names) ? config.header_names : [];
    $("#webhook-header-list").replaceChildren(...headers.map((name) => makeWebhookHeaderRow(name, channel?.has_secret)));
    $("#webhook-json-template").value = JSON.stringify(channel?.provider === "webhook" ? config.json_template || {} : {
      event_type: "{event_type}", host: "{host}", service: "{service}", state: "{state}", source: "{source}", timestamp: "{timestamp}",
    }, null, 2);
    const hasSecret = Boolean(channel?.secret_configured || channel?.has_secret);
    $("#channel-secret-note").hidden = !channel;
    for (const provider of Object.keys(PROVIDER_LABELS)) setSecretStatus(provider, hasSecret && channel?.provider === provider);
    setProviderFields(); $("#channel-dialog").showModal(); $("#channel-name").focus();
  }
  function collectChannelWrite() {
    const provider = $("#channel-provider").value;
    const name = $("#channel-name").value.trim();
    if (!name) throw new Error("Escribe un nombre para el canal.");
    const config = {}; const secrets = {};
    if (provider === "telegram") {
      config.chat_id = $("#telegram-chat-id").value.trim();
      const token = $("#telegram-bot-token").value.trim(); if (token) secrets.bot_token = token;
    } else if (provider === "gotify") {
      config.base_url = $("#gotify-base-url").value.trim(); config.priority = intValue("#gotify-priority", 5);
      const token = $("#gotify-app-token").value.trim(); if (token) secrets.app_token = token;
    } else if (provider === "ntfy") {
      config.base_url = $("#ntfy-base-url").value.trim(); config.topic = $("#ntfy-topic").value.trim(); config.priority = intValue("#ntfy-priority", 3);
      const token = $("#ntfy-access-token").value.trim(); const user = $("#ntfy-username").value.trim(); const password = $("#ntfy-password").value;
      if (token && (user || password)) throw new Error("Usa Access Token o usuario/contraseña, no ambos.");
      if ((user && !password) || (!user && password)) throw new Error("Completa juntos el usuario y la contraseña de ntfy.");
      if (token) secrets.access_token = token;
      if (user && password) { secrets.username = user; secrets.password = password; }
    } else if (provider === "webhook") {
      config.url = $("#webhook-url").value.trim(); config.method = "POST"; config.content_type = "application/json";
      const rows = $$(".webhook-header-row", $("#webhook-header-list"));
      const names = []; const headerSecrets = {};
      for (const row of rows) {
        const headerName = $("input[type='text']", row).value.trim(); const headerValue = $("input[type='password']", row).value;
        if (!headerName && !headerValue) continue;
        if (!headerName) throw new Error("Cada valor de header necesita un nombre.");
        names.push(headerName); if (headerValue) headerSecrets[headerName] = headerValue;
      }
      config.header_names = names;
      try { config.json_template = JSON.parse($("#webhook-json-template").value); }
      catch { throw new Error("La plantilla del webhook debe ser JSON válido."); }
      if (Object.keys(headerSecrets).length) secrets.headers = headerSecrets;
    }
    const timeout = intValue("#channel-timeout", -1); const rate = intValue("#channel-rate-limit", -1);
    if (timeout < 1 || timeout > 60) throw new Error("El timeout debe estar entre 1 y 60 segundos.");
    if (rate < 0 || rate > 3600) throw new Error("El intervalo mínimo entre pruebas debe estar entre 0 y 3600 segundos.");
    return { name, provider, config, secrets, enabled: $("#channel-enabled").checked, timeout_seconds: timeout, rate_limit_seconds: rate };
  }
  async function saveChannel(event) {
    event.preventDefault(); clearFormError("#channel-form-error");
    const save = $("#save-channel");
    try {
      const body = collectChannelWrite(); setBusy(save, true);
      const id = $("#channel-id").value;
      const result = await apiRequest(id ? `/channels/${id}` : "/channels", { method: id ? "PUT" : "POST", body });
      $("#channel-dialog").close();
      await loadNotifications(true);
      const index = note.channels.findIndex((channel) => String(channel.id) === String(result.id));
      if (index >= 0) { note.channelPage = Math.floor(index / note.pageSize); renderChannels(); }
      showNotice(`Canal “${result.name}” guardado.`);
    } catch (error) { setFormError("#channel-form-error", error instanceof Error ? error.message : "No se pudo guardar el canal."); }
    finally { setBusy(save, false, "Guardar canal"); }
  }
  async function runChannelTest(channel) {
    if (note.testChannelId !== null) return;
    note.testChannelId = channel.id; renderChannels(); clearError();
    try {
      const result = await apiRequest(`/channels/${channel.id}/test`, { method: "POST" });
      await loadNotifications(true);
      showNotice(result.ok ? `Prueba enviada a “${channel.name}” (HTTP ${result.status_code ?? "OK"}).` : `Falló la prueba de “${channel.name}”: ${result.error || "sin detalle"}`, result.ok ? "success" : "error");
    } catch (error) {
      await loadNotifications(true);
      if (error.status === 429) {
        showNotice(`El canal fue probado hace poco. Reintenta en ${error.retryAfter || error.payload?.retry_after_seconds || "unos"} segundos.`, "error");
      } else if (error.status === 502) {
        const payload = error.payload || {};
        showNotice(`Falló la prueba de “${channel.name}”${payload.status_code ? ` (HTTP ${payload.status_code})` : ""}: ${payload.error || error.message}`, "error");
      } else showNotice(`No se pudo probar “${channel.name}”: ${error.message}`, "error");
    } finally { note.testChannelId = null; renderChannels(); }
  }

  function statusLabel(status) { return STATUS_LABELS[status] || status || "—"; }
  function statusClass(status) {
    if (status === "sent") return "is-success";
    if (status === "failed") return "is-failure";
    if (status === "pending" || status === "in_flight") return "is-pending";
    return "is-neutral";
  }
  function historyUrl() {
    const params = new URLSearchParams({ limit: String(note.pageSize), offset: String(note.historyPage * note.pageSize) });
    for (const [field, id] of [["rule_id", $("#history-rule-filter").value], ["channel_id", $("#history-channel-filter").value]]) if (id) params.set(field, id);
    if ($("#history-result-filter").value) params.set("result", $("#history-result-filter").value);
    const from = $("#history-from-filter").value; const to = $("#history-to-filter").value;
    if (from) { const [y, m, d] = from.split("-").map(Number); params.set("from", new Date(y, m - 1, d).toISOString()); }
    if (to) {
      const [y, m, d] = to.split("-").map(Number);
      const until = new Date(y, m - 1, d + 1);
      until.setMilliseconds(until.getMilliseconds() - 1);
      params.set("to", until.toISOString());
    }
    return `/history?${params}`;
  }
  async function loadDeliveryHistory() {
    const data = await apiRequest(historyUrl());
    note.history = data.items || []; note.historyTotal = Number(data.total) || 0;
    renderDeliveryHistory();
  }
  function renderDeliveryHistory() {
    const total = note.historyTotal; const pages = Math.max(1, Math.ceil(total / note.pageSize));
    note.historyPage = Math.min(note.historyPage, pages - 1);
    const body = $("#notification-history-body");
    body.replaceChildren(...note.history.map((item) => {
      const row = node("tr");
      const event = node("td", ""); event.append(node("span", "history-event-name", EVENT_LABELS[item.event_type] || item.event_type), node("small", "history-date", localDate(item.occurred_at))); row.append(event);
      row.append(node("td", "", item.rule_name || "Regla eliminada"));
      row.append(node("td", "", item.service ? `${item.host} · ${item.service}` : `${item.host} · host`));
      const stateCell = node("td", "");
      if (item.old_state) stateCell.append(node("span", "old-state", `${item.old_state} → `));
      stateCell.append(node("span", "state-label", item.state || "—")); row.append(stateCell);
      row.append(node("td", "", `${item.channel_name || "Canal eliminado"} · ${PROVIDER_LABELS[item.provider] || item.provider}`));
      row.append(node("td", "", "")); row.lastElementChild.append(node("span", `delivery-status ${statusClass(item.status)}`, statusLabel(item.status)));
      row.append(node("td", "attempt-count", item.attempt_count ?? 0));
      row.append(node("td", "", localDate(item.last_attempt_at)));
      const error = node("td", "delivery-error", item.last_error || "—"); if (item.last_error) error.title = item.last_error; row.append(error);
      return row;
    }));
    $("#notification-history-empty").hidden = note.history.length !== 0;
    $("#notification-history-empty-title").textContent = total ? "No hay entregas para esos filtros" : "Sin entregas registradas";
    $("#notification-history-empty-text").textContent = total ? "Prueba otro rango de fechas o limpia los filtros." : "Cuando una transición coincida con una regla habilitada, el resultado aparecerá aquí.";
    $("#notification-history-count").textContent = `${total} ${total === 1 ? "entrega" : "entregas"}`;
    const pager = $("#notification-history-pagination"); pager.hidden = total <= note.pageSize;
    $("#notification-history-page-label").textContent = `Página ${note.historyPage + 1} de ${pages}`;
    $("#notification-history-prev").disabled = note.historyPage <= 0; $("#notification-history-next").disabled = note.historyPage >= pages - 1;
  }
  async function saveSettings(event) {
    event.preventDefault(); clearError();
    const save = $("#save-notification-settings"); const retention = intValue("#settings-retention-days", -1);
    if (retention < 0 || retention > 3650) { showError("La retención debe ser un entero entre 0 y 3650 días."); return; }
    setBusy(save, true);
    try {
      note.settings = await apiRequest("/settings", { method: "PUT", body: { enabled: $("#settings-engine-enabled").checked, history_retention_days: retention } });
      setEngineStatus(); $("#settings-updated-at").textContent = `Guardado ${localDate(note.settings.updated_at)}`;
      showNotice(note.settings.enabled ? "Motor de notificaciones habilitado." : "Motor deshabilitado; las entregas pendientes se cancelaron.");
    } catch (error) { showError(error.message); setEngineStatus(); }
    finally { setBusy(save, false); }
  }
  async function downloadNotificationBackup(password, buttonElement) {
    setBusy(buttonElement, true, "Preparando archivo…");
    try {
      const response = await fetch(`${API}/backup/export`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" },
        cache: "no-store", body: JSON.stringify({ password }),
      });
      if (!response.ok) {
        let payload = null;
        try { payload = await response.json(); } catch { /* use a safe fallback */ }
        throw new Error(apiErrorMessage(payload, `No se pudo exportar el backup (HTTP ${response.status}).`));
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = /filename="([A-Za-z0-9._-]+)"/.exec(disposition);
      link.href = objectUrl; link.download = match ? match[1] : "naftamon-notifications-backup.json";
      document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(objectUrl);
      showNotice(password ? "Backup cifrado descargado." : "Backup descargado; no incluye credenciales.");
    } catch (error) { showError(error instanceof Error ? error.message : "No se pudo exportar la configuración."); }
    finally { setBusy(buttonElement, false); }
  }
  function renderBackupPreview(preview) {
    const container = $("#notification-backup-preview");
    container.replaceChildren(); container.hidden = false;
    container.append(node("strong", "backup-preview-title", "Vista previa válida"));
    container.append(node("p", "", `${preview.rules} reglas (${preview.enabled_rules} habilitadas) · ${preview.channels} canales (${preview.enabled_channels} habilitados) · motor ${preview.settings.enabled ? "habilitado" : "deshabilitado"} · retención ${preview.settings.history_retention_days} días.`));
    container.append(node("p", "", preview.encrypted_secrets ? "Credenciales incluidas y cifradas; se volverán a cifrar con la clave de esta instalación." : "El archivo no incluye credenciales."));
    if (preview.warnings.length) {
      const list = node("ul", "backup-warning-list");
      for (const warning of preview.warnings) list.append(node("li", "", warning));
      container.append(list);
    } else container.append(node("p", "backup-no-warnings", "Sin advertencias de compatibilidad."));
    $("#restore-notification-backup").disabled = false;
  }
  function renderPreviousBackup(summary) {
    const container = $("#previous-notification-backup");
    container.hidden = !summary?.available;
    if (!summary?.available) return;
    $("#previous-notification-backup-info").textContent = `${localDate(summary.created_at)} · ${summary.rules} reglas · ${summary.channels} canales${summary.encrypted_secrets ? " · credenciales cifradas con la clave de esta instalación" : ""}.`;
  }
  function clearBackupPreview() {
    note.backupDocument = null; note.backupPreview = null;
    const preview = $("#notification-backup-preview"); preview.replaceChildren(); preview.hidden = true;
    $("#restore-notification-backup").disabled = true;
  }
  async function previewNotificationBackup() {
    clearBackupPreview(); clearError();
    const file = $("#notification-backup-file").files[0];
    if (!file) { showError("Selecciona un archivo JSON de backup."); return; }
    if (file.size > 2 * 1024 * 1024) { showError("El archivo supera el límite de 2 MiB."); return; }
    const buttonElement = $("#preview-notification-backup"); setBusy(buttonElement, true, "Validando…");
    try {
      const raw = await file.text();
      const backup = JSON.parse(raw);
      if (!backup || typeof backup !== "object" || Array.isArray(backup)) throw new Error("La raíz del archivo debe ser un objeto JSON.");
      const password = $("#backup-import-password").value || null;
      const preview = await apiRequest("/backup/preview", { method: "POST", body: { backup, password } });
      note.backupDocument = backup; note.backupPreview = preview;
      renderBackupPreview(preview);
    } catch (error) { showError(error instanceof Error ? error.message : "No se pudo validar el backup."); }
    finally { setBusy(buttonElement, false); }
  }
  async function restoreNotificationBackup() {
    if (!note.backupDocument || !note.backupPreview) return;
    const preview = note.backupPreview;
    openConfirm(
      "Reemplazar configuración actual",
      `Se reemplazarán ${preview.rules} reglas, ${preview.channels} canales y las opciones globales de notificaciones. Las entregas pendientes se cancelarán; el historial de entregas y los datos de monitoreo se conservarán. Si falla la operación, SQLite revierte el cambio completo.`,
      "Reemplazar configuración",
      async () => {
        const buttonElement = $("#restore-notification-backup"); setBusy(buttonElement, true, "Restaurando…");
        try {
          const result = await apiRequest("/backup/restore", {
            method: "POST",
            body: { backup: note.backupDocument, password: $("#backup-import-password").value || null, confirm_replace: true },
          });
          clearBackupPreview(); $("#notification-backup-file").value = ""; $("#backup-import-password").value = "";
          await loadNotifications(true);
          showNotice(`Configuración restaurada: ${result.rules} reglas y ${result.channels} canales.`);
        } catch (error) { showError(error instanceof Error ? error.message : "No se pudo restaurar la configuración."); }
        finally { setBusy(buttonElement, false); buttonElement.disabled = !note.backupDocument || !note.backupPreview; }
      },
    );
  }
  async function restorePreviousNotificationBackup() {
    const summary = note.previousBackup;
    if (!summary?.available) return;
    openConfirm(
      "Recuperar configuración anterior",
      `Se recuperará la copia creada ${localDate(summary.created_at)} con ${summary.rules} reglas y ${summary.channels} canales. La configuración actual se guardará como una nueva copia recuperable antes del cambio.`,
      "Recuperar copia",
      async () => {
        const buttonElement = $("#restore-previous-notification-backup"); setBusy(buttonElement, true, "Recuperando…");
        try {
          const result = await apiRequest("/backup/restore-previous", { method: "POST", body: { confirm_replace: true } });
          await loadNotifications(true);
          showNotice(`Copia recuperada: ${result.rules} reglas y ${result.channels} canales.`);
        } catch (error) { showError(error instanceof Error ? error.message : "No se pudo recuperar la copia previa."); }
        finally { setBusy(buttonElement, false); }
      },
    );
  }
  function openConfirm(title, message, actionLabel, action) {
    $("#confirm-title").textContent = title; $("#confirm-message").textContent = message;
    $("#confirm-accept").textContent = actionLabel; note.confirmAction = action;
    $("#confirm-dialog").showModal(); $("#confirm-accept").focus();
  }
  async function acceptConfirm() {
    const action = note.confirmAction; note.confirmAction = null; $("#confirm-dialog").close();
    if (action) await action();
  }
  async function deleteRule(rule) {
    openConfirm("Eliminar regla", `Se eliminará “${rule.name}”. Las entregas pendientes de esta regla se cancelarán.`, "Eliminar regla", async () => {
      try { await apiRequest(`/rules/${rule.id}`, { method: "DELETE" }); await loadNotifications(true); showNotice(`Regla “${rule.name}” eliminada.`); }
      catch (error) { showError(error.message); }
    });
  }
  async function deleteChannel(channel) {
    openConfirm("Eliminar canal", `Se eliminará “${channel.name}” y dejará de estar asociado a las reglas. Las entregas pendientes se cancelarán.`, "Eliminar canal", async () => {
      try { await apiRequest(`/channels/${channel.id}`, { method: "DELETE" }); await loadNotifications(true); showNotice(`Canal “${channel.name}” eliminado.`); }
      catch (error) { showError(error.message); }
    });
  }
  async function toggleRule(rule, enabled) {
    const body = {
      name: rule.name, description: rule.description || "", enabled,
      conditions: rule.conditions, events: rule.events, channel_ids: rule.channel_ids || [],
      cooldown_seconds: rule.cooldown_seconds, reminder_interval_seconds: rule.reminder_interval_seconds,
      subject_template: rule.subject_template, body_template: rule.body_template,
    };
    try { await apiRequest(`/rules/${rule.id}`, { method: "PUT", body }); await loadNotifications(true); showNotice(`Regla ${enabled ? "habilitada" : "deshabilitada"}.`); }
    catch (error) { showError(error.message); renderRules(); }
  }
  async function toggleChannel(channel, enabled) {
    const body = {
      name: channel.name, provider: channel.provider, config: channel.config || {}, enabled,
      timeout_seconds: channel.timeout_seconds, rate_limit_seconds: channel.rate_limit_seconds,
      // No secret is placed in a read response. Omitting it asks the API to preserve its encrypted value.
    };
    try { await apiRequest(`/channels/${channel.id}`, { method: "PUT", body }); await loadNotifications(true); showNotice(`Canal ${enabled ? "habilitado" : "deshabilitado"}.`); }
    catch (error) { showError(error.message); renderChannels(); }
  }
  async function duplicateRule(rule) {
    try {
      const copy = await apiRequest(`/rules/${rule.id}/duplicate`, { method: "POST" });
      await loadNotifications(true); showNotice(`Se creó “${copy.name}” deshabilitada.`);
    } catch (error) { showError(error.message); }
  }
  async function dispatchAction(target) {
    const action = target.dataset.action;
    const ruleCard = target.closest("[data-rule-id]");
    const channelCard = target.closest("[data-channel-id]");
    const rule = ruleCard ? note.rules.find((item) => String(item.id) === ruleCard.dataset.ruleId) : null;
    const channel = channelCard ? note.channels.find((item) => String(item.id) === channelCard.dataset.channelId) : null;
    if (action === "toggle-rule" && rule) return toggleRule(rule, target.checked);
    if (action === "toggle-channel" && channel) return toggleChannel(channel, target.checked);
    if (action === "edit-rule" && rule) return openRuleEditor(rule);
    if (action === "delete-rule" && rule) return deleteRule(rule);
    if (action === "duplicate-rule" && rule) return duplicateRule(rule);
    if (action === "test-rule" && rule) return testRule(rule);
    if (action === "edit-channel" && channel) return openChannelEditor(channel);
    if (action === "delete-channel" && channel) return deleteChannel(channel);
    if (action === "test-channel" && channel) return runChannelTest(channel);
    if (action === "remove-condition") { target.closest(".condition-row")?.remove(); return; }
    if (action === "remove-webhook-header") { target.closest(".webhook-header-row")?.remove(); return; }
  }

  function wireEvents() {
    window.addEventListener("hashchange", applyRoute);
    applyRoute();
    $("#notification-error-close").addEventListener("click", clearError);
    $("#notification-retry").addEventListener("click", () => loadNotifications(true));
    $("#create-rule").addEventListener("click", () => openRuleEditor());
    $("#empty-create-rule").addEventListener("click", () => openRuleEditor());
    $("#create-channel").addEventListener("click", () => openChannelEditor());
    $("#empty-create-channel").addEventListener("click", () => openChannelEditor());
    $("#rule-form").addEventListener("submit", saveRule);
    $("#channel-form").addEventListener("submit", saveChannel);
    $("#notification-settings-form").addEventListener("submit", saveSettings);
    $("#export-notification-backup").addEventListener("click", (event) => downloadNotificationBackup(null, event.currentTarget));
    $("#export-encrypted-notification-backup").addEventListener("click", (event) => {
      const password = $("#backup-export-password").value;
      if (password.length < 12) { showError("La contraseña de cifrado debe tener al menos 12 caracteres."); return; }
      if (password !== $("#backup-export-password-confirm").value) { showError("Las contraseñas no coinciden."); return; }
      downloadNotificationBackup(password, event.currentTarget).finally(() => {
        $("#backup-export-password").value = ""; $("#backup-export-password-confirm").value = "";
      });
    });
    $("#notification-backup-file").addEventListener("change", clearBackupPreview);
    $("#backup-import-password").addEventListener("input", clearBackupPreview);
    $("#preview-notification-backup").addEventListener("click", previewNotificationBackup);
    $("#restore-notification-backup").addEventListener("click", restoreNotificationBackup);
    $("#restore-previous-notification-backup").addEventListener("click", restorePreviousNotificationBackup);
    $("#channel-provider").addEventListener("change", setProviderFields);
    $("#add-condition").addEventListener("click", () => {
      const list = $("#rule-condition-list");
      if (list.children.length >= 32) { setFormError("#rule-form-error", "Una regla admite como máximo 32 condiciones."); return; }
      clearFormError("#rule-form-error"); list.append(makeConditionRow({ field: "state", operator: "equals", value: "CRITICAL" }));
    });
    $("#add-webhook-header").addEventListener("click", () => $("#webhook-header-list").append(makeWebhookHeaderRow()));
    $("#rules-list").addEventListener("click", (event) => {
      const target = event.target.closest("[data-action]");
      if (target && target.dataset.action !== "toggle-rule") dispatchAction(target);
    });
    $("#rules-list").addEventListener("change", (event) => {
      const target = event.target.closest("[data-action='toggle-rule']");
      if (target) dispatchAction(target);
    });
    $("#channels-list").addEventListener("click", (event) => {
      const target = event.target.closest("[data-action]");
      if (target && target.dataset.action !== "toggle-channel") dispatchAction(target);
    });
    $("#channels-list").addEventListener("change", (event) => {
      const target = event.target.closest("[data-action='toggle-channel']");
      if (target) dispatchAction(target);
    });
    $("#rule-dialog").addEventListener("click", (event) => {
      const target = event.target.closest("[data-action='remove-condition']");
      if (target) dispatchAction(target);
    });
    $("#channel-dialog").addEventListener("click", (event) => {
      const target = event.target.closest("[data-action='remove-webhook-header']");
      if (target) dispatchAction(target);
    });
    $("#rules-prev").addEventListener("click", () => { note.rulePage = Math.max(0, note.rulePage - 1); renderRules(); });
    $("#rules-next").addEventListener("click", () => { note.rulePage += 1; renderRules(); });
    $("#channels-prev").addEventListener("click", () => { note.channelPage = Math.max(0, note.channelPage - 1); renderChannels(); });
    $("#channels-next").addEventListener("click", () => { note.channelPage += 1; renderChannels(); });
    $("#notification-history-filters").addEventListener("submit", (event) => { event.preventDefault(); note.historyPage = 0; loadDeliveryHistory().catch((error) => showError(error.message)); });
    for (const selector of ["#history-rule-filter", "#history-channel-filter", "#history-result-filter", "#history-from-filter", "#history-to-filter"]) {
      $(selector).addEventListener("change", () => { note.historyPage = 0; loadDeliveryHistory().catch((error) => showError(error.message)); });
    }
    $("#refresh-notification-history").addEventListener("click", () => loadDeliveryHistory().catch((error) => showError(error.message)));
    $("#clear-notification-history-filters").addEventListener("click", () => {
      $("#notification-history-filters").reset(); note.historyPage = 0;
      loadDeliveryHistory().catch((error) => showError(error.message));
    });
    $("#notification-history-prev").addEventListener("click", () => { note.historyPage = Math.max(0, note.historyPage - 1); loadDeliveryHistory().catch((error) => showError(error.message)); });
    $("#notification-history-next").addEventListener("click", () => { note.historyPage += 1; loadDeliveryHistory().catch((error) => showError(error.message)); });
    $("#confirm-accept").addEventListener("click", acceptConfirm);
    for (const close of $$('[data-close-dialog]')) close.addEventListener("click", () => {
      const dialog = document.getElementById(close.dataset.closeDialog);
      if (dialog?.open) {
        dialog.close();
        if (dialog.id === "channel-dialog") {
          for (const input of $$("input[type='password']", dialog)) input.value = "";
        }
      }
    });
    $("#channel-dialog").addEventListener("close", () => {
      for (const input of $$("input[type='password']", $("#channel-dialog"))) input.value = "";
    });
  }

  wireEvents();
})();
