(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = {
    alerts: [], statuses: [], sources: [], backends: [], stats: null, offlineAfter: 900, inactiveAfter: 86400, historyItems: [], historyTotal: 0,
    pollMs: 5000, timer: null, refreshing: false, refreshPending: false, lastSuccess: null,
    sort: { key: "state", direction: 1 }, page: 0, pageSize: 50, statusView: "desktop", filteredTotal: 0, virtualized: false, virtualTop: null, virtualBottom: null,
    virtualChunks: new Map(), virtualPending: new Set(), virtualRevision: null, virtualOffset: 0, virtualStart: 0,
    virtualChunkSize: 500, statusQueryVersion: 0,
    historyPage: 0, historyPageSize: 50, activeView: "alerts", sourceClocks: new Map(),
    statusRows: new Map(), sourceCards: new Map(), historyRows: new Map(),
  };
  const stateOrder = { CRITICAL: 0, DOWN: 1, UNREACHABLE: 2, WARNING: 3, UNKNOWN: 4, UP: 5 };
  const refs = {
    connection: $("#connection-state"), connectionLabel: $("#connection-label"),
    backendBanner: $("#backend-banner"), backendError: $("#backend-error-text"),
    warning: $("#data-warning"), warningText: $("#data-warning-text"), firstData: $("#first-data"),
    alertBody: $("#alerts-body"), tableEmpty: $("#table-empty"), tableEmptyTitle: $("#table-empty-title"), tableEmptyText: $("#table-empty-text"),
    sourcesGrid: $("#sources-grid"), sourcesEmpty: $("#sources-empty"), historyBody: $("#history-body"), historyEmpty: $("#history-empty"),
  };

  function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
  function storageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* Storage may be disabled. */ } }
  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function setText(selector, value) { const node = $(selector); if (node) node.textContent = String(value); }
  function normalize(value) { return String(value ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("es"); }
  function parseDate(value) { const time = Date.parse(value || ""); return Number.isFinite(time) ? new Date(time) : null; }
  const dateFormat = new Intl.DateTimeFormat("es-AR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  function formatDate(value) { const date = parseDate(value); return date ? dateFormat.format(date) : "—"; }
  function ageText(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    if (value < 60) return `Hace ${value} s`;
    const minutes = Math.floor(value / 60);
    if (minutes < 60) return `Hace ${minutes} min`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `Hace ${hours} h ${minutes % 60} min`;
    return `Hace ${Math.floor(hours / 24)} d ${hours % 24} h`;
  }
  function durationLabel(seconds) {
    const value = Math.max(1, Math.floor(Number(seconds) || 0));
    if (value % 86400 === 0) return `${value / 86400} d`;
    if (value % 3600 === 0) return `${value / 3600} h`;
    if (value % 60 === 0) return `${value / 60} min`;
    return `${value} s`;
  }
  function sourceAge(source) {
    const clock = state.sourceClocks.get(source.id);
    if (clock) return Math.max(0, clock.age + (Date.now() - clock.at) / 1000);
    return Math.max(0, Number(source.age_seconds) || 0);
  }
  function sourceIsOffline(source) { return Boolean(source.offline) || sourceAge(source) > state.offlineAfter; }
  function sourceIsInactive(source) { return sourceAge(source) > state.inactiveAfter; }
  function setConnection(kind, label) { refs.connection.dataset.state = kind; refs.connectionLabel.textContent = label; }
  function updateTheme(theme) {
    const selected = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = selected;
    $("#theme-label").textContent = selected === "dark" ? "Tema claro" : "Tema oscuro";
    $("#theme-toggle").setAttribute("aria-label", selected === "dark" ? "Cambiar a tema claro" : "Cambiar a tema oscuro");
    storageSet("naftamon-theme", selected);
  }
  function syncSelect(select, values, allLabel) {
    const current = select.value;
    const entries = [{ value: "", label: allLabel }, ...values];
    const signature = JSON.stringify(entries);
    if (select.dataset.optionsSignature === signature) return;
    if (current && !entries.some((entry) => String(entry.value) === current)) entries.push({ value: current, label: `${current} (sin alertas actuales)` });
    select.replaceChildren(...entries.map((entry) => {
      const option = document.createElement("option"); option.value = String(entry.value); option.textContent = entry.label; return option;
    }));
    select.dataset.optionsSignature = signature;
    select.value = entries.some((entry) => String(entry.value) === current) ? current : "";
  }
  function updateFilterOptions() {
    const servers = [...new Set(state.sources.flatMap((source) => (source.servers || []).map((server) => server.name)))].sort((a, b) => a.localeCompare(b, "es", { sensitivity: "base" }));
    const backends = state.backends.filter(Boolean).sort((a, b) => a.localeCompare(b, "es", { sensitivity: "base" }));
    const sourceValues = [...new Map(state.sources.map((source) => [String(source.id), { value: source.id, label: `${source.source} · ${source.peer_ip}` }])).values()]
      .sort((a, b) => a.label.localeCompare(b.label, "es", { sensitivity: "base" }));
    syncSelect($("#filter-server"), servers.map((value) => ({ value, label: value })), "Todos");
    syncSelect($("#filter-backend"), backends.map((value) => ({ value, label: value })), "Todos");
    syncSelect($("#filter-source"), sourceValues, "Todas");
    syncSelect($("#history-source"), sourceValues, "Todas");
    const sourceServerNames = [...new Set(state.sources.flatMap((source) => (source.servers || []).map((server) => server.name)))].sort((a, b) => a.localeCompare(b, "es", { sensitivity: "base" }));
    syncSelect($("#history-server"), sourceServerNames.map((value) => ({ value, label: value })), "Todos");
  }

  async function getJSON(url) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal: controller.signal });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        const failure = new Error(detail.detail || `HTTP ${response.status} en ${url}`);
        failure.status = response.status; failure.payload = detail; throw failure;
      }
      return await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new Error("La consulta al backend superó 12 segundos.");
      throw error;
    } finally { window.clearTimeout(timer); }
  }
  function statusQuery(offset, limit, revision = null) {
    const filter = readAlertFilters();
    const query = new URLSearchParams({
      limit: String(limit), offset: String(offset), sort: state.sort.key,
      direction: state.sort.direction === 1 ? "asc" : "desc",
      view: state.statusView,
    });
    if (filter.host) query.set("host_query", filter.host);
    if (filter.service) query.set("service_query", filter.service);
    if (filter.text) query.set("query", filter.text);
    if (filter.state) query.set("state", filter.state);
    if (filter.server) query.set("server", filter.server);
    if (filter.backend) query.set("backend", filter.backend);
    if (filter.source) query.set("source_id", filter.source);
    if (filter.ack !== "") query.set("acknowledged", filter.ack);
    if (filter.downtime !== "") query.set("downtime", filter.downtime);
    if (revision !== null) query.set("revision", String(revision));
    return query;
  }
  function statusOffsetForView() {
    return state.pageSize === "all"
      ? Math.floor(state.virtualStart / state.virtualChunkSize) * state.virtualChunkSize
      : state.page * state.pageSize;
  }
  async function requestStatuses(offset, limit, revision = null) {
    return getJSON(`/api/v1/statuses?${statusQuery(offset, limit, revision)}`);
  }
  function resetStatusQuery() {
    state.statusQueryVersion += 1;
    state.virtualChunks.clear(); state.virtualPending.clear(); state.virtualRevision = null;
    state.virtualOffset = 0; state.virtualStart = 0; state.page = 0;
    state.statusRows.clear();
    if (state.pageSize === "all") $("#monitoring-scroll").scrollTop = 0;
  }
  async function fetchHistory() {
    const query = new URLSearchParams({ limit: String(state.historyPageSize), offset: String(state.historyPage * state.historyPageSize) });
    if ($("#history-source").value) query.set("source_id", $("#history-source").value);
    if ($("#history-server").value) query.set("server", $("#history-server").value);
    const result = await getJSON(`/api/v1/history?${query}`);
    state.historyItems = Array.isArray(result.items) ? result.items : [];
    state.historyTotal = Number(result.total) || 0;
    renderHistory();
  }
  async function refresh() {
    if (state.refreshing) { state.refreshPending = true; return; }
    state.refreshing = true;
    setConnection("loading", "Actualizando datos");
    try {
      const offset = statusOffsetForView();
      const limit = state.pageSize === "all" ? state.virtualChunkSize : state.pageSize;
      const queryVersion = state.statusQueryVersion;
      const [health, sourceResult] = await Promise.all([
        getJSON("/healthz"), getJSON("/api/v1/sources"),
      ]);
      let statsResult, statusResult;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        statsResult = await getJSON("/api/v1/stats");
        const targetRevision = Number(statsResult.monitoring_revision);
        if (state.pageSize === "all" && state.virtualRevision !== targetRevision) {
          state.virtualChunks.clear(); state.virtualRevision = targetRevision;
        }
        try {
          statusResult = await requestStatuses(offset, limit, targetRevision);
          break;
        } catch (error) {
          if (error.status !== 409 || attempt === 2) throw error;
        }
      }
      if (queryVersion !== state.statusQueryVersion) { state.refreshPending = true; return; }
      if (health.status !== "ok" || !Array.isArray(sourceResult.items) || !Array.isArray(statusResult.items)) throw new Error("El backend respondió con un estado no válido.");
      state.sources = sourceResult.items;
      state.backends = Array.isArray(sourceResult.backends) ? sourceResult.backends : [];
      state.stats = statsResult;
      state.offlineAfter = Math.max(1, Number(sourceResult.offline_after_seconds) || 900);
      state.inactiveAfter = Math.max(1, Number(sourceResult.inactive_after_seconds) || 86400);
      state.statuses = statusResult.items;
      state.alerts = state.statuses.filter((item) => item.state !== "UP");
      state.filteredTotal = Number(statusResult.total) || 0;
      state.virtualOffset = offset;
      if (state.pageSize === "all") {
        state.virtualRevision = statusResult.revision;
        state.virtualChunks.set(offset, statusResult.items);
      }
      state.lastSuccess = Date.now();
      state.sourceClocks = new Map(state.sources.map((source) => [source.id, { age: Number(source.age_seconds) || 0, at: state.lastSuccess }]));
      refs.backendBanner.hidden = true;
      setConnection("ok", "Backend disponible");
      updateFilterOptions();
      renderDashboard();
      if (state.activeView === "history") {
        try { await fetchHistory(); }
        catch (error) { showHistoryError(error); }
      }
    } catch (error) {
      if (error.status === 409 && state.pageSize === "all") {
        state.virtualChunks.clear(); state.virtualRevision = null; state.virtualOffset = 0;
        state.refreshPending = true;
        return;
      }
      refs.backendBanner.hidden = false;
      refs.backendError.textContent = error instanceof Error ? error.message : "No se pudo consultar el backend.";
      setConnection("error", "Backend no disponible");
      if (state.lastSuccess !== null) refs.backendError.textContent += ` Se conserva en pantalla la última actualización correcta de ${formatDate(new Date(state.lastSuccess).toISOString())}.`;
    } finally {
      state.refreshing = false;
      if (state.lastSuccess !== null) setText("#footer-update", `Actualizado ${formatDate(new Date(state.lastSuccess).toISOString())}`);
      renderAges();
      if (state.refreshPending) { state.refreshPending = false; window.setTimeout(refresh, 0); }
    }
  }

  function sourceLabel(sourceId) {
    const source = state.sources.find((item) => Number(item.id) === Number(sourceId));
    return source ? `${source.source} · ${source.peer_ip}` : "Fuente desconocida";
  }
  function readAlertFilters() {
    return {
      host: normalize($("#filter-host").value), service: normalize($("#filter-service").value), text: normalize($("#filter-text").value),
      state: $("#filter-severity").value, server: $("#filter-server").value, backend: $("#filter-backend").value,
      source: $("#filter-source").value, ack: $("#filter-ack").value, downtime: $("#filter-downtime").value,
    };
  }
  function compareAlerts(a, b) {
    const key = state.sort.key;
    let comparison;
    if (key === "state") comparison = (stateOrder[a.state] ?? 9) - (stateOrder[b.state] ?? 9);
    else if (key === "acknowledged" || key === "downtime") comparison = Number(Boolean(a[key])) - Number(Boolean(b[key]));
    else if (key === "source") comparison = sourceLabel(a.source_id).localeCompare(sourceLabel(b.source_id), "es", { sensitivity: "base" });
    else if (key === "last_seen_at") comparison = (Date.parse(a.last_seen_at) || 0) - (Date.parse(b.last_seen_at) || 0);
    else comparison = String(a[key] ?? "").localeCompare(String(b[key] ?? ""), "es", { numeric: true, sensitivity: "base" });
    if (comparison === 0) comparison = String(a.host).localeCompare(String(b.host), "es", { numeric: true, sensitivity: "base" });
    return comparison * state.sort.direction;
  }
  function makeStatusRow(alert) {
    const tr = make("tr");
    tr.dataset.id = String(alert.id);
    const cells = Array.from({ length: 11 }, () => make("td"));
    cells.forEach((td) => tr.append(td));
    tr._cells = cells;
    return tr;
  }
  function updateAlertRow(tr, alert) {
    const signature = JSON.stringify([alert.source_id, alert.source, alert.peer_ip, alert.server, alert.backend, alert.host, alert.service, alert.state, alert.status_information, alert.stale, alert.acknowledged, alert.downtime, alert.last_seen_at]);
    if (tr.dataset.signature === signature) return;
    tr.dataset.signature = signature;
    const cells = tr._cells;
    cells.forEach((cell) => cell.replaceChildren());
    const source = make("span", "cell-primary", alert.source || "—");
    const sourceSub = make("small", "source-ident", `#${alert.source_id} · ${alert.peer_ip || "IP no disponible"}`);
    cells[0].append(source, sourceSub); cells[0].title = `${alert.source || "Fuente"} · ${alert.peer_ip || ""}`;
    cells[1].append(make("span", "cell-primary", alert.server));
    cells[2].append(make("span", alert.backend ? "" : "cell-muted", alert.backend || "—"));
    cells[3].append(make("span", "cell-primary", alert.host));
    cells[4].append(make("span", alert.service ? "" : "cell-muted", alert.service || "— (host)"));
    const severity = make("span", "severity-badge", alert.state); severity.dataset.state = alert.state; cells[5].append(severity);
    const statusInformation = typeof alert.status_information === "string" ? alert.status_information : "";
    const informationText = statusInformation.trim() ? statusInformation : "-";
    const information = make("span", `status-information-text${statusInformation.trim() ? "" : " is-unavailable"}`, informationText);
    if (statusInformation.trim()) {
      information.title = statusInformation;
      information.setAttribute("aria-label", `Status Information completo: ${statusInformation}`);
      information.tabIndex = 0;
    }
    cells[6].className = "status-information-cell"; cells[6].append(information);
    const confirmed = make("span", `confirm-badge${alert.stale ? " is-stale" : ""}`, alert.stale ? "Sin confirmar" : "Actual"); cells[7].append(confirmed);
    const ack = make("span", `flag-badge${alert.acknowledged ? " is-on" : ""}`, alert.acknowledged ? "Sí" : "No"); cells[8].append(ack);
    const down = make("span", `flag-badge${alert.downtime ? " is-downtime" : ""}`, alert.downtime ? "Sí" : "No"); cells[9].append(down);
    const received = make("span", "cell-mono", formatDate(alert.last_seen_at)); received.title = alert.last_seen_at || ""; cells[10].append(received);
  }
  function moveNodes(container, nodes) {
    nodes.forEach((node, index) => {
      const current = container.children[index];
      if (current !== node) container.insertBefore(node, current || null);
    });
    while (container.children.length > nodes.length) container.lastElementChild.remove();
  }
  function statusRow(alert, rowIndex) {
    const key = String(alert.id);
    let tr = state.statusRows.get(key);
    if (!tr) { tr = makeStatusRow(alert); state.statusRows.set(key, tr); }
    updateAlertRow(tr, alert);
    tr.setAttribute("aria-rowindex", String(rowIndex + 2));
    return tr;
  }
  function clearHiddenStatusRows(items) {
    const visibleIds = new Set(items.map((alert) => String(alert.id)));
    for (const key of state.statusRows.keys()) if (!visibleIds.has(key)) state.statusRows.delete(key);
  }
  async function fetchVirtualChunk(offset) {
    if (state.virtualChunks.has(offset) || state.virtualPending.has(offset)) return;
    const queryVersion = state.statusQueryVersion;
    const revision = state.virtualRevision;
    state.virtualPending.add(offset);
    try {
      const result = await requestStatuses(offset, state.virtualChunkSize, revision);
      if (queryVersion !== state.statusQueryVersion || state.pageSize !== "all") return;
      if (revision === null) state.virtualRevision = result.revision;
      else if (result.revision !== revision) {
        state.virtualChunks.clear(); state.virtualRevision = null; state.virtualOffset = offset;
        window.setTimeout(refresh, 0); return;
      }
      state.virtualChunks.set(offset, result.items);
      state.filteredTotal = Number(result.total) || 0;
      state.virtualOffset = offset;
      const keep = new Set([offset - state.virtualChunkSize, offset, offset + state.virtualChunkSize]);
      for (const cachedOffset of state.virtualChunks.keys()) if (!keep.has(cachedOffset)) state.virtualChunks.delete(cachedOffset);
      state.statuses = result.items;
      state.alerts = result.items.filter((item) => item.state !== "UP");
      renderAlerts();
    } catch (error) {
      if (queryVersion !== state.statusQueryVersion) return;
      if (error.status === 409) {
        state.virtualChunks.clear(); state.virtualRevision = null; state.virtualOffset = offset;
        window.setTimeout(refresh, 0);
      } else {
        refs.backendBanner.hidden = false;
        refs.backendError.textContent = error instanceof Error ? error.message : "No se pudo consultar el backend.";
        setConnection("error", "Error consultando monitoreo");
      }
    } finally {
      state.virtualPending.delete(offset);
    }
  }
  function renderVirtualRows() {
    const total = state.filteredTotal;
    const scroll = $("#monitoring-scroll");
    if (!total) { moveNodes(refs.alertBody, []); state.statusRows.clear(); return; }
    const headerHeight = scroll.querySelector("thead")?.getBoundingClientRect().height || 40;
    let sampleRow = refs.alertBody.querySelector("tr:not(.virtual-spacer)");
    if (!sampleRow && state.statuses.length) { sampleRow = statusRow(state.statuses[0], 0); refs.alertBody.append(sampleRow); }
    const measuredHeight = sampleRow?.getBoundingClientRect().height || 56;
    const rowHeight = measuredHeight;
    const overscan = 8;
    const visibleCapacity = Math.max(30, Math.ceil(Math.max(0, scroll.clientHeight - headerHeight) / rowHeight) + overscan * 2);
    const requestedStart = Math.floor(Math.max(0, scroll.scrollTop - headerHeight) / rowHeight) - overscan;
    const start = Math.max(0, Math.min(total - visibleCapacity, requestedStart));
    const end = Math.min(total, start + visibleCapacity);
    state.virtualStart = start;
    if (!state.virtualTop) {
      state.virtualTop = make("tr", "virtual-spacer");
      state.virtualTop.setAttribute("aria-hidden", "true");
      const topCell = make("td"); topCell.colSpan = 11; state.virtualTop.append(topCell);
    }
    if (!state.virtualBottom) {
      state.virtualBottom = make("tr", "virtual-spacer");
      state.virtualBottom.setAttribute("aria-hidden", "true");
      const bottomCell = make("td"); bottomCell.colSpan = 11; state.virtualBottom.append(bottomCell);
    }
    state.virtualTop.firstElementChild.style.height = `${start * rowHeight}px`;
    state.virtualBottom.firstElementChild.style.height = `${(total - end) * rowHeight}px`;
    const firstChunk = Math.floor(start / state.virtualChunkSize) * state.virtualChunkSize;
    const lastChunk = Math.floor(Math.max(start, end - 1) / state.virtualChunkSize) * state.virtualChunkSize;
    for (let offset = firstChunk; offset <= lastChunk; offset += state.virtualChunkSize)
      if (!state.virtualChunks.has(offset)) fetchVirtualChunk(offset);
    const visible = [];
    const rows = [];
    for (let index = start; index < end; index += 1) {
      const offset = Math.floor(index / state.virtualChunkSize) * state.virtualChunkSize;
      const item = state.virtualChunks.get(offset)?.[index - offset];
      if (item) { visible.push(item); rows.push(statusRow(item, index)); }
      else {
        const placeholder = make("tr", "virtual-placeholder"); placeholder.dataset.index = String(index); placeholder.setAttribute("aria-hidden", "true");
        const cell = make("td"); cell.colSpan = 11; cell.style.height = `${rowHeight}px`; placeholder.append(cell); rows.push(placeholder);
      }
    }
    clearHiddenStatusRows(visible);
    moveNodes(refs.alertBody, [state.virtualTop, ...rows, state.virtualBottom]);
  }
  function renderAlerts() {
    const allFiltered = state.statuses;
    const total = state.filteredTotal;
    const scroll = $("#monitoring-scroll");
    const showAll = state.pageSize === "all";
    const totalPages = showAll ? 1 : Math.max(1, Math.ceil(total / state.pageSize));
    state.page = Math.min(state.page, totalPages - 1);
    scroll.querySelector(".alerts-table").setAttribute("aria-rowcount", String(total + 1));
    const virtualized = showAll && total > 250;
    if (virtualized !== state.virtualized) {
      state.virtualized = virtualized;
      scroll.classList.toggle("is-virtualized", virtualized);
      scroll.scrollTop = 0;
    }
    let pageItems;
    if (virtualized) {
      pageItems = [];
      renderVirtualRows();
    } else {
      pageItems = allFiltered;
      const rowOffset = showAll ? 0 : state.page * state.pageSize;
      const nodes = pageItems.map((alert, index) => statusRow(alert, rowOffset + index));
      clearHiddenStatusRows(pageItems);
      moveNodes(refs.alertBody, nodes);
    }
    const noSources = state.sources.length === 0;
    const noStatuses = state.filteredTotal === 0;
    refs.tableEmpty.hidden = total !== 0;
    if (noSources) {
      refs.tableEmptyTitle.textContent = "Todavía no hay snapshots recibidos";
      refs.tableEmptyText.textContent = "El backend está disponible, pero no tiene fuentes registradas. Esperando el primer webhook periódico.";
    } else if (noStatuses && currentlyHealthy()) {
      refs.tableEmptyTitle.textContent = "Sin estados de hosts o servicios";
      refs.tableEmptyText.textContent = "Las consultas están completas y no hay alertas. No se recibieron objetos UP para listar en este snapshot.";
    } else if (noStatuses) {
      refs.tableEmptyTitle.textContent = "Sin estados de monitoreo confirmados";
      refs.tableEmptyText.textContent = "Hay fuentes offline, datos desactualizados o consultas parciales. La ausencia de datos no confirma que la infraestructura esté saludable.";
    } else {
      refs.tableEmptyTitle.textContent = "No hay resultados con esos filtros";
      refs.tableEmptyText.textContent = "Prueba cambiar los criterios o limpiar los filtros.";
    }
    const start = total ? (showAll ? 1 : state.page * state.pageSize + 1) : 0;
    const end = showAll ? total : Math.min((state.page + 1) * state.pageSize, total);
    if (total === (state.stats?.monitoring?.total ?? total)) {
      setText("#filtered-count", showAll ? `Mostrando todos los ${total} hosts y servicios` : `Mostrando ${start}–${end} de ${total} hosts y servicios`);
    } else {
      setText("#filtered-count", showAll ? `${total} estados coinciden · consulta completa bajo demanda` : `${total} estados coinciden`);
    }
    setText("#alert-count-label", `${total} ${total === 1 ? "elemento" : "elementos"}`);
    setText("#tab-alert-count", total);
    const pagination = $("#pagination"); pagination.hidden = showAll || total <= state.pageSize;
    setText("#page-summary", showAll ? `Todos · ${total} registros (carga por desplazamiento)` : `Página ${state.page + 1} de ${totalPages} · ${start}–${end} de ${total}`);
    $("#page-prev").disabled = state.page <= 0; $("#page-next").disabled = state.page >= totalPages - 1;
    for (const button of $$("[data-sort]")) {
      const selected = button.dataset.sort === state.sort.key;
      button.querySelector(".sort-mark").textContent = selected ? (state.sort.direction === 1 ? "↑" : "↓") : "";
      button.closest("th").setAttribute("aria-sort", selected ? (state.sort.direction === 1 ? "ascending" : "descending") : "none");
    }
  }

  function currentlyHealthy() {
    return state.sources.length > 0 && state.sources.every((source) => !sourceIsOffline(source) && source.complete && Array.isArray(source.servers) && source.servers.length > 0 && source.servers.every((server) => server.status === "ok" && !server.stale && !server.removed));
  }
  function renderMetrics() {
    const alerts = (state.statusView === "desktop" ? state.stats?.desktop_visible_alerts : state.stats?.alerts) || {};
    const byState = alerts.by_state || {};
    const byStateType = alerts.by_state_and_type || {};
    const critical = byState.CRITICAL || 0;
    const warning = byState.WARNING || 0;
    const unknown = byState.UNKNOWN || 0;
    const down = byStateType["DOWN:host"] || 0;
    const unreachable = byStateType["UNREACHABLE:host"] || 0;
    const affectedHosts = alerts.affected_hosts || 0;
    const online = state.sources.filter((source) => !sourceIsOffline(source)).length;
    setText("#metric-total", alerts.total || 0);
    setText("#metric-critical", critical); setText("#metric-warning", warning); setText("#metric-unknown", unknown);
    setText("#metric-hosts", affectedHosts); setText("#metric-host-detail", `Down ${down} · Unreachable ${unreachable}`);
    setText("#metric-sources", `${online} / ${state.sources.length}`);
    setText("#metric-source-detail", `${state.sources.length - online} offline`);
    const staleCount = alerts.stale || 0;
    const scopeLabel = state.statusView === "desktop" ? "Vista visible en Desktop" : "Vista RAW de alertas";
    setText("#metric-stale-note", `${scopeLabel} · ${staleCount ? `${staleCount} sin confirmar` : "sin estados obsoletos"}`);
    const latest = state.sources.map((source) => source.last_received_at).filter(Boolean).sort().at(-1);
    setText("#last-received", latest ? formatDate(latest) : "Sin recepciones");
    if (latest) {
      const source = state.sources.find((item) => item.last_received_at === latest);
      setText("#last-received-age", source ? ageText(sourceAge(source)) : "—");
    } else setText("#last-received-age", "Esperando datos");
    refs.firstData.hidden = state.sources.length !== 0;
    setText("#source-count-label", `${state.sources.length} ${state.sources.length === 1 ? "fuente" : "fuentes"}`);
  }
  function renderWarning() {
    const issues = [];
    const offline = state.sources.filter(sourceIsOffline).length;
    const staleItems = state.stats?.alerts?.stale || 0;
    const failedServers = state.sources.flatMap((source) => (source.servers || []).filter((server) => server.status !== "ok" || server.stale || server.removed).map((server) => ({ source, server })));
    const partial = state.sources.filter((source) => !source.complete).length;
    if (offline) issues.push(`${offline} ${offline === 1 ? "fuente offline" : "fuentes offline"}`);
    if (failedServers.length) issues.push(`${failedServers.length} ${failedServers.length === 1 ? "servidor con error, pendiente o retirado" : "servidores con error, pendientes o retirados"}`);
    if (staleItems) issues.push(`${staleItems} ${staleItems === 1 ? "estado sin confirmar" : "estados sin confirmar"}`);
    else if (partial && !failedServers.length) issues.push(`${partial} ${partial === 1 ? "snapshot parcial" : "snapshots parciales"}`);
    refs.warning.hidden = issues.length === 0;
    if (issues.length) refs.warningText.textContent = `${issues.join(" · ")}. Los datos se conservan, pero no deben interpretarse como estado saludable.`;
  }
  function makeSourceCard(source) {
    const card = make("article", "source-card"); card.dataset.sourceId = String(source.id);
    const top = make("div", "source-card-top");
    const info = make("div");
    const name = make("h3", "source-name", source.source); info.append(name);
    const ident = make("small", "source-ident", `ID ${source.id} · ${source.peer_ip || "peer desconocido"}`); info.append(ident);
    const badges = make("div", "source-badges");
    const badge = make("span", "source-status"); const activity = make("span", "source-activity"); badges.append(badge, activity); top.append(info, badges); card.append(top);
    const meta = make("div", "source-meta-grid");
    const lastMeta = make("div", "source-meta"); lastMeta.append(make("span", "", "Último webhook")); const last = make("strong", "source-last", formatDate(source.last_received_at)); lastMeta.append(last);
    const ageMeta = make("div", "source-meta"); ageMeta.append(make("span", "", "Tiempo transcurrido")); const age = make("strong", "age-value", "—"); ageMeta.append(age);
    const alertMeta = make("div", "source-meta"); alertMeta.append(make("span", "", "Alertas activas")); const alerts = make("strong", "source-alert-count", "0"); alertMeta.append(alerts);
    const snapMeta = make("div", "source-meta"); snapMeta.append(make("span", "", "Snapshot")); const complete = make("strong", "source-complete", "—"); snapMeta.append(complete);
    meta.append(lastMeta, ageMeta, alertMeta, snapMeta); card.append(meta);
    const serverBlock = make("div", "source-servers"); serverBlock.append(make("div", "source-servers-title", "Servidores Thruk"));
    const serverList = make("div", "source-server-list"); serverBlock.append(serverList); card.append(serverBlock);
    const actions = make("div", "source-actions");
    const remove = make("button", "danger-action", "Eliminar fuente inactiva"); remove.type = "button"; remove.hidden = true;
    remove.addEventListener("click", () => openSourceDelete(card._source)); actions.append(remove); card.append(actions);
    card._refs = { badge, activity, last, age, alerts, complete, serverList, remove };
    return card;
  }
  function updateSourceCard(card, source) {
    card._source = source;
    const servers = Array.isArray(source.servers) ? source.servers : [];
    const signature = JSON.stringify([source.source, source.peer_ip, source.last_received_at, source.complete, source.alert_count, servers.map((server) => [server.name, server.status, server.stale, server.removed])]);
    if (card.dataset.signature !== signature) {
      card.dataset.signature = signature;
      const r = card._refs;
      r.last.textContent = formatDate(source.last_received_at); r.last.title = source.last_received_at || "";
      r.complete.textContent = source.complete ? "Completo" : "Parcial";
      const serverNodes = servers.map((server) => {
        const row = make("div", "source-server-row");
        row.append(make("span", "source-server-name", server.name));
        const statusText = server.removed ? "Retirado" : server.status === "ok" ? (server.stale ? "Sin confirmar" : "OK") : server.status === "error" ? "Error" : "Pendiente";
        const status = make("span", `server-status${server.removed ? " is-removed" : server.status === "error" ? " is-error" : server.status !== "ok" || server.stale ? " is-stale" : ""}`, statusText);
        row.append(status); return row;
      });
      moveNodes(r.serverList, serverNodes);
      if (!servers.length) r.serverList.replaceChildren(make("div", "source-server-row", "No hay servidores configurados en el snapshot."));
    }
    const offline = sourceIsOffline(source);
    card._refs.badge.textContent = offline ? "Offline" : "Online";
    card._refs.badge.classList.toggle("is-offline", offline);
    const inactive = sourceIsInactive(source);
    card._refs.activity.textContent = inactive ? "Inactiva" : "Activa";
    card._refs.activity.classList.toggle("is-inactive", inactive);
    card._refs.activity.title = `Umbral de inactividad: ${durationLabel(state.inactiveAfter)}`;
    card._refs.remove.hidden = !inactive;
    card._refs.age.textContent = ageText(sourceAge(source));
    card._refs.age.title = `${Math.floor(sourceAge(source))} s desde la última recepción; offline tras ${state.offlineAfter} s`;
    card._refs.alerts.textContent = String(source.alert_count || 0);
    return card;
  }
  function renderSources() {
    const nodes = state.sources.map((source) => {
      const key = String(source.id);
      let card = state.sourceCards.get(key);
      if (!card) { card = makeSourceCard(source); state.sourceCards.set(key, card); }
      return updateSourceCard(card, source);
    });
    moveNodes(refs.sourcesGrid, nodes);
    refs.sourcesEmpty.hidden = state.sources.length !== 0;
  }
  function renderDashboard() { renderMetrics(); renderWarning(); renderAlerts(); renderSources(); renderAges(); }

  function eventTitle(event) {
    return ({ opened: "Abierta", state_changed: "Cambio de estado", attributes_changed: "Cambio de atributos", recovered: "Recuperada" })[event] || event;
  }
  function summarizeChange(value) {
    if (!value) return "—";
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object") {
        const pieces = [];
        if (parsed.state) pieces.push(parsed.state);
        if (typeof parsed.hard === "boolean") pieces.push(parsed.hard ? "HARD" : "SOFT");
        if (typeof parsed.acknowledged === "boolean") pieces.push(parsed.acknowledged ? "Acknowledged" : "Sin ack");
        if (typeof parsed.downtime === "boolean") pieces.push(parsed.downtime ? "Downtime" : "Sin downtime");
        return pieces.join(" · ") || "Cambio registrado";
      }
    } catch { /* State changes are stored as plain state labels. */ }
    return String(value);
  }
  function makeHistoryRow(item) {
    const tr = make("tr"); tr.dataset.id = String(item.id);
    for (let i = 0; i < 6; i++) tr.append(make("td"));
    tr._cells = [...tr.children]; return tr;
  }
  function updateHistoryRow(tr, item) {
    const signature = JSON.stringify([item.observed_at, item.source_name, item.peer_ip, item.server, item.host, item.service, item.event, item.old_value, item.new_value]);
    if (tr.dataset.signature === signature) return;
    tr.dataset.signature = signature;
    const cells = tr._cells;
    cells[0].textContent = formatDate(item.observed_at); cells[0].title = item.observed_at || "";
    cells[1].textContent = `${item.source_name || "NaftaMon"} · ${item.peer_ip || ""}`;
    cells[2].textContent = item.server || "—";
    cells[3].textContent = item.service ? `${item.host} / ${item.service}` : `${item.host} (host)`;
    cells[4].textContent = eventTitle(item.event); cells[4].className = `event-${item.event || ""}`;
    const oldValue = summarizeChange(item.old_value); const newValue = summarizeChange(item.new_value);
    cells[5].textContent = item.event === "opened" ? `Nueva: ${newValue}` : item.event === "recovered" ? `Anterior: ${oldValue}` : `${oldValue} → ${newValue}`;
  }
  function renderHistory() {
    const pages = Math.max(1, Math.ceil(state.historyTotal / state.historyPageSize));
    state.historyPage = Math.min(state.historyPage, pages - 1);
    const nodes = state.historyItems.map((item) => {
      const key = String(item.id); let tr = state.historyRows.get(key);
      if (!tr) { tr = makeHistoryRow(item); state.historyRows.set(key, tr); }
      updateHistoryRow(tr, item); return tr;
    });
    const visibleIds = new Set(state.historyItems.map((item) => String(item.id)));
    for (const key of state.historyRows.keys()) if (!visibleIds.has(key)) state.historyRows.delete(key);
    moveNodes(refs.historyBody, nodes);
    refs.historyEmpty.hidden = state.historyItems.length !== 0;
    $("#history-empty-title").textContent = state.historyTotal === 0 ? "Todavía no hay transiciones registradas" : "No hay cambios para estos filtros";
    $("#history-empty-text").textContent = "El historial solo muestra aperturas, cambios y recuperaciones observados en snapshots recibidos.";
    setText("#history-total", `${state.historyTotal} ${state.historyTotal === 1 ? "evento" : "eventos"}`);
    const pagination = $("#history-pagination"); pagination.hidden = state.historyTotal <= state.historyPageSize;
    setText("#history-page-summary", `Página ${state.historyPage + 1} de ${pages}`);
    $("#history-prev").disabled = state.historyPage <= 0; $("#history-next").disabled = state.historyPage >= pages - 1;
  }
  function showHistoryError(error) {
    refs.historyEmpty.hidden = false;
    $("#history-empty-title").textContent = "No se pudo consultar el historial";
    $("#history-empty-text").textContent = error instanceof Error ? error.message : "Error consultando historial.";
  }
  function renderAges() {
    for (const source of state.sources) {
      const card = state.sourceCards.get(String(source.id));
      if (!card) continue;
      const age = sourceAge(source); const offline = sourceIsOffline(source);
      card._refs.age.textContent = ageText(age);
      card._refs.age.title = `${Math.floor(age)} s desde la última recepción; offline tras ${state.offlineAfter} s`;
      card._refs.badge.textContent = offline ? "Offline" : "Online";
      card._refs.badge.classList.toggle("is-offline", offline);
      const inactive = sourceIsInactive(source);
      card._refs.activity.textContent = inactive ? "Inactiva" : "Activa";
      card._refs.activity.classList.toggle("is-inactive", inactive);
      card._refs.activity.title = `Umbral de inactividad: ${durationLabel(state.inactiveAfter)}`;
      card._refs.remove.hidden = !inactive;
    }
    if (state.sources.length && state.lastSuccess !== null) { renderMetrics(); renderWarning(); }
  }

  function openSourceDelete(source) {
    if (!source || !sourceIsInactive(source)) return;
    const dialog = $("#source-delete-dialog");
    dialog.dataset.sourceId = String(source.id);
    $("#source-delete-message").textContent = `¿Eliminar “${source.source}” (${source.peer_ip || "peer desconocido"})? Última información recibida ${formatDate(source.last_received_at)} (${ageText(sourceAge(source))}).`;
    $("#source-delete-error").hidden = true;
    $("#source-delete-error").textContent = "";
    $("#source-delete-accept").disabled = false;
    dialog.showModal();
    $("#source-delete-cancel").focus();
  }

  async function deleteSelectedSource() {
    const dialog = $("#source-delete-dialog");
    const sourceId = Number(dialog.dataset.sourceId);
    if (!Number.isInteger(sourceId) || sourceId < 1) return;
    const button = $("#source-delete-accept"); const error = $("#source-delete-error");
    button.disabled = true; error.hidden = true;
    try {
      const response = await fetch(`/api/v1/sources/${sourceId}`, { method: "DELETE", headers: { Accept: "application/json" }, cache: "no-store" });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      dialog.close();
      state.sourceCards.delete(String(sourceId));
      await refresh();
    } catch (cause) {
      error.textContent = cause instanceof Error ? cause.message : "No se pudo eliminar la fuente.";
      error.hidden = false;
    } finally { button.disabled = false; }
  }

  function chooseView(view) {
    state.activeView = view;
    const alertsActive = view === "alerts";
    $("#alerts-view").hidden = !alertsActive; $("#history-view").hidden = alertsActive;
    $("#tab-alerts").classList.toggle("is-active", alertsActive); $("#tab-history").classList.toggle("is-active", !alertsActive);
    $("#tab-alerts").setAttribute("aria-selected", String(alertsActive)); $("#tab-history").setAttribute("aria-selected", String(!alertsActive));
    $("#tab-alerts").tabIndex = alertsActive ? 0 : -1; $("#tab-history").tabIndex = alertsActive ? -1 : 0;
    if (!alertsActive) fetchHistory().catch(showHistoryError);
  }
  function restartPolling() { if (state.timer) window.clearTimeout(state.timer); state.timer = window.setTimeout(async () => { await refresh(); restartPolling(); }, state.pollMs); }
  function scheduleRefresh() { if (state.timer) window.clearTimeout(state.timer); state.timer = window.setTimeout(async () => { await refresh(); restartPolling(); }, 0); }

  $("#filters").addEventListener("submit", (event) => event.preventDefault());
  function resetMonitoringPosition() { if (state.virtualized || state.pageSize === "all") $("#monitoring-scroll").scrollTop = 0; }
  let filterDebounce = null;
  function applyServerFilters() {
    resetStatusQuery(); resetMonitoringPosition();
    if (filterDebounce) window.clearTimeout(filterDebounce);
    filterDebounce = window.setTimeout(scheduleRefresh, 180);
  }
  $("#filters").addEventListener("input", (event) => { if (event.target.matches("input")) applyServerFilters(); });
  $("#filters").addEventListener("change", (event) => { if (event.target.matches("select")) applyServerFilters(); });
  $("#clear-filters").addEventListener("click", () => {
    for (const selector of ["#filter-host", "#filter-service", "#filter-text", "#filter-severity", "#filter-server", "#filter-backend", "#filter-source", "#filter-ack", "#filter-downtime"]) $(selector).value = "";
    applyServerFilters(); $("#filter-host").focus();
  });
  for (const button of $$("[data-sort]")) button.addEventListener("click", () => {
    const key = button.dataset.sort;
    if (state.sort.key === key) state.sort.direction *= -1;
    else { state.sort.key = key; state.sort.direction = 1; }
    applyServerFilters();
  });
  $("#monitoring-view").addEventListener("change", (event) => {
    if (!["desktop", "all"].includes(event.target.value)) return;
    state.statusView = event.target.value;
    storageSet("naftamon-monitor-view", state.statusView);
    resetStatusQuery(); resetMonitoringPosition();
    scheduleRefresh();
  });
  $("#page-prev").addEventListener("click", () => { state.page = Math.max(0, state.page - 1); scheduleRefresh(); });
  $("#page-next").addEventListener("click", () => { state.page += 1; scheduleRefresh(); });
  $("#page-size").addEventListener("change", (event) => {
    const selected = event.target.value;
    const newSize = selected === "all" ? "all" : Number(selected);
    if (![25, 50, 100, 250, "all"].includes(newSize)) return;
    state.pageSize = newSize;
    resetStatusQuery(); resetMonitoringPosition();
    storageSet("naftamon-monitor-page-size", String(newSize));
    scheduleRefresh();
  });
  let virtualRenderPending = false;
  function scheduleVirtualRender() {
    if (!state.virtualized || virtualRenderPending) return;
    virtualRenderPending = true;
    window.requestAnimationFrame(() => { virtualRenderPending = false; if (state.virtualized) renderVirtualRows(); });
  }
  $("#monitoring-scroll").addEventListener("scroll", scheduleVirtualRender, { passive: true });
  window.addEventListener("resize", scheduleVirtualRender, { passive: true });
  $("#history-prev").addEventListener("click", () => { state.historyPage = Math.max(0, state.historyPage - 1); fetchHistory().catch(showHistoryError); });
  $("#history-next").addEventListener("click", () => { state.historyPage += 1; fetchHistory().catch(showHistoryError); });
  $("#history-source").addEventListener("change", () => { state.historyPage = 0; fetchHistory().catch(showHistoryError); });
  $("#history-server").addEventListener("change", () => { state.historyPage = 0; fetchHistory().catch(showHistoryError); });
  $("#tab-alerts").addEventListener("click", () => chooseView("alerts"));
  $("#tab-history").addEventListener("click", () => chooseView("history"));
  for (const tab of [$("#tab-alerts"), $("#tab-history")]) tab.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); const target = tab.id === "tab-alerts" ? $("#tab-history") : $("#tab-alerts"); target.focus(); target.click(); }
  });
  $("#poll-interval").addEventListener("change", (event) => {
    state.pollMs = Math.max(5000, Number(event.target.value) || 5000); storageSet("naftamon-poll-ms", String(state.pollMs)); restartPolling();
  });
  $("#refresh-now").addEventListener("click", scheduleRefresh);
  $("#retry-now").addEventListener("click", scheduleRefresh);
  $("#theme-toggle").addEventListener("click", () => updateTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("#source-delete-accept").addEventListener("click", deleteSelectedSource);
  $("#source-delete-cancel").addEventListener("click", () => $("#source-delete-dialog").close());
  $("#source-delete-close").addEventListener("click", () => $("#source-delete-dialog").close());

  const savedTheme = storageGet("naftamon-theme"); updateTheme(savedTheme === "light" ? "light" : "dark");
  const savedInterval = Number(storageGet("naftamon-poll-ms"));
  if ([5000, 10000, 15000, 30000, 60000].includes(savedInterval)) { state.pollMs = savedInterval; $("#poll-interval").value = String(savedInterval); }
  const savedPageSize = storageGet("naftamon-monitor-page-size");
  if (["25", "50", "100", "250", "all"].includes(savedPageSize)) {
    state.pageSize = savedPageSize === "all" ? "all" : Number(savedPageSize);
    $("#page-size").value = savedPageSize;
  }
  const savedMonitoringView = storageGet("naftamon-monitor-view");
  if (["desktop", "all"].includes(savedMonitoringView)) {
    state.statusView = savedMonitoringView;
    $("#monitoring-view").value = savedMonitoringView;
  }
  window.setInterval(renderAges, 1000);
  refresh().finally(restartPolling);
})();
