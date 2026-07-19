/** Hardcoded API — dev account 699013266218 / ap-south-1 */
const API_BASE = "https://21oe3vqlvl.execute-api.ap-south-1.amazonaws.com";

const AUTH_STORAGE_KEY = "walmart_dashboard_auth";

const loginScreen = document.getElementById("login-screen");
const appShell = document.getElementById("app-shell");
const loginForm = document.getElementById("login-form");
const loginEmail = document.getElementById("login-email");
const loginPassword = document.getElementById("login-password");
const loginError = document.getElementById("login-error");
const loginSubmit = document.getElementById("login-submit");
const logoutBtn = document.getElementById("logout-btn");

let authSession = null;

function loadAuthSession() {
  try {
    const raw = sessionStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && parsed.id_token ? parsed : null;
  } catch {
    return null;
  }
}

function saveAuthSession(session) {
  authSession = session;
  sessionStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
}

function clearAuthSession() {
  authSession = null;
  sessionStorage.removeItem(AUTH_STORAGE_KEY);
}

function jwtExpMs(token) {
  try {
    const part = token.split(".")[1];
    if (!part) return 0;
    const payload = JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/")));
    return payload.exp ? payload.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

function isSessionValid(session) {
  if (!session?.id_token) return false;
  const exp = jwtExpMs(session.id_token);
  if (!exp) return true;
  return Date.now() < exp - 60_000;
}

function authHeaders(extra) {
  const headers = { ...(extra || {}) };
  if (authSession?.id_token) {
    headers.Authorization = `Bearer ${authSession.id_token}`;
  }
  return headers;
}

function showLoginScreen(message) {
  appShell?.classList.add("hidden");
  loginScreen?.classList.remove("hidden");
  if (message && loginError) {
    loginError.textContent = message;
    loginError.classList.remove("hidden");
  } else if (loginError) {
    loginError.textContent = "";
    loginError.classList.add("hidden");
  }
}

function showAppShell() {
  loginScreen?.classList.add("hidden");
  appShell?.classList.remove("hidden");
  if (loginError) {
    loginError.textContent = "";
    loginError.classList.add("hidden");
  }
}

async function refreshAuthSession() {
  if (!authSession?.refresh_token) return false;
  const res = await fetch(`${API_BASE}/v1/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: authSession.refresh_token }),
  });
  const body = await res.text();
  let data = null;
  if (body) {
    try {
      data = JSON.parse(body);
    } catch {
      data = { error: body };
    }
  }
  if (!res.ok) return false;
  saveAuthSession({
    id_token: data.id_token,
    access_token: data.access_token,
    refresh_token: data.refresh_token || authSession.refresh_token,
    expires_in: data.expires_in,
  });
  return true;
}

async function loginWithPassword(email, password) {
  const res = await fetch(`${API_BASE}/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const body = await res.text();
  let data = null;
  if (body) {
    try {
      data = JSON.parse(body);
    } catch {
      data = { error: body };
    }
  }
  if (!res.ok) {
    throw new Error(data?.error || `${res.status} ${body}`);
  }
  saveAuthSession({
    id_token: data.id_token,
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_in: data.expires_in,
  });
}

async function ensureAuthenticated() {
  authSession = loadAuthSession();
  if (isSessionValid(authSession)) return true;
  if (authSession?.refresh_token && (await refreshAuthSession())) return true;
  clearAuthSession();
  return false;
}

async function handleUnauthorized() {
  if (authSession?.refresh_token && (await refreshAuthSession())) {
    return true;
  }
  clearAuthSession();
  showLoginScreen("Session expired — sign in again.");
  return false;
}

async function bootstrapApp() {
  if (isFileProtocol()) {
    showAppShell();
    showError(fileProtocolHelp());
    return;
  }

  const ok = await ensureAuthenticated();
  if (!ok) {
    showLoginScreen();
    return;
  }

  showAppShell();
  updateSortIndicators();
  try {
    await loadOrders(true);
  } catch (e) {
    showError(e.message);
    ordersBody.innerHTML =
      `<tr><td colspan="${COLSPAN}" class="loading-cell">Failed to load data.</td></tr>`;
  }
}

const COLSPAN = 14;

let currentOffset = 0;
let pageTotal = 0;
let lastDashboardStats = null;
let sortBy = "order_date";
let sortDir = "desc";

const statusFilter = document.getElementById("status-filter");
const poFilter = document.getElementById("po-filter");
const orderFilter = document.getElementById("order-filter");
const skuFilter = document.getElementById("sku-filter");
const itemFilter = document.getElementById("item-filter");
const qtyFilter = document.getElementById("qty-filter");
const orderDateFrom = document.getElementById("order-date-from");
const orderDateTo = document.getElementById("order-date-to");
const statusDateFrom = document.getElementById("status-date-from");
const statusDateTo = document.getElementById("status-date-to");
const updatedFrom = document.getElementById("updated-from");
const updatedTo = document.getElementById("updated-to");
const shippingFilter = document.getElementById("shipping-filter");
const countySurchargeFilter = document.getElementById("county-surcharge-filter");
const promotionDiscountFilter = document.getElementById("promotion-discount-filter");
const lineCountFilter = document.getElementById("line-count-filter");
const sapSyncFilter = document.getElementById("sap-sync-filter");
const clearFiltersBtn = document.getElementById("clear-filters-btn");
const pageSizeSelect = document.getElementById("page-size");
const lastSyncMeta = document.getElementById("last-sync-meta");
const ordersBody = document.getElementById("orders-body");
const syncBody = document.getElementById("sync-body");
const sapItemsBody = document.getElementById("sap-items-body");
const sapItemsMeta = document.getElementById("sap-items-meta");
const errorBanner = document.getElementById("error-banner");
const ordersPanel = document.getElementById("orders-panel");
const syncPanel = document.getElementById("sync-panel");
const pageTitle = document.getElementById("page-title");
const pageEyebrow = document.querySelector(".page-eyebrow");
const statChips = document.getElementById("stat-chips");
const ordersFilterCard = document.getElementById("orders-filter-card");
const paginationInfo = document.getElementById("pagination-info");
const prevPageBtn = document.getElementById("prev-page");
const nextPageBtn = document.getElementById("next-page");
const sapErrorModal = document.getElementById("sap-error-modal");
const sapErrorBackdrop = document.getElementById("sap-error-backdrop");
const sapErrorClose = document.getElementById("sap-error-close");
const sapErrorPo = document.getElementById("sap-error-po");
const sapErrorText = document.getElementById("sap-error-text");
const sapAlertBell = document.getElementById("sap-alert-bell");
const sapAlertCount = document.getElementById("sap-alert-count");
const sapAlertHint = document.getElementById("sap-alert-hint");
const sapAlertWrap = document.getElementById("sap-alert-wrap");
const sapAlertPanel = document.getElementById("sap-alert-panel");
const sapAlertPanelBody = document.getElementById("sap-alert-panel-body");
const sapAlertPanelCount = document.getElementById("sap-alert-panel-count");
const sapAlertViewAll = document.getElementById("sap-alert-view-all");
const sapRetryBtn = document.getElementById("sap-retry-btn");

function buildOrdersQueryParams(limit, offset) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  params.set("sort_by", sortBy);
  params.set("sort_dir", sortDir);

  const add = (key, value) => {
    const trimmed = String(value || "").trim();
    if (trimmed) params.set(key, trimmed);
  };

  add("purchase_order_id", poFilter.value);
  add("customer_order_id", orderFilter.value);
  add("status", statusFilter.value);
  add("sku", skuFilter.value);
  add("product_name", itemFilter.value);
  add("quantity", qtyFilter.value);
  add("order_date_from", orderDateFrom.value);
  add("order_date_to", orderDateTo.value);
  add("status_date_from", statusDateFrom.value);
  add("status_date_to", statusDateTo.value);
  add("updated_from", updatedFrom.value);
  add("updated_to", updatedTo.value);
  add("has_shipping", shippingFilter.value);
  add("county_surcharge_tax", countySurchargeFilter?.value);
  add("promotion_discount", promotionDiscountFilter?.value);
  add("line_count", lineCountFilter?.value);
  add("sap_sync", sapSyncFilter?.value);

  return params.toString();
}

function updateSortIndicators() {
  document.querySelectorAll(".sortable-th").forEach((th) => {
    const key = th.getAttribute("data-sort-key");
    const indicator = th.querySelector(".sort-indicator");
    const active = key === sortBy;
    th.classList.toggle("sortable-th--active", active);
    if (indicator) {
      indicator.textContent = active ? (sortDir === "asc" ? "▲" : "▼") : "";
    }
  });
}

function toggleSort(columnKey) {
  if (sortBy === columnKey) {
    sortDir = sortDir === "asc" ? "desc" : "asc";
  } else {
    sortBy = columnKey;
    sortDir = columnKey === "po" || columnKey === "order" || columnKey === "sku" ? "asc" : "desc";
  }
  updateSortIndicators();
  loadOrders(true).catch((e) => showError(e.message));
}

function pageSize() {
  return parseInt(pageSizeSelect.value, 10) || 100;
}

function hideError() {
  errorBanner.classList.add("hidden");
  errorBanner.textContent = "";
}

function isFileProtocol() {
  return window.location.protocol === "file:";
}

function fileProtocolHelp() {
  return (
    "Chrome blocks API calls when you open index.html as a file (file://). " +
    "In Terminal run: cd services/B2C_Invoice/dashboard && python3 -m http.server 8080 " +
    "Then open http://localhost:8080 in the browser."
  );
}

function showError(message) {
  errorBanner.textContent = message;
  errorBanner.classList.remove("hidden");
}

async function apiGet(path) {
  try {
    const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() });
    if (res.status === 401) {
      const recovered = await handleUnauthorized();
      if (recovered) {
        return apiGet(path);
      }
      throw new Error("Unauthorized — sign in again");
    }
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`${res.status} ${body}`);
    }
    return res.json();
  } catch (err) {
    if (isFileProtocol() && err instanceof TypeError) {
      throw new Error(fileProtocolHelp());
    }
    throw err;
  }
}

async function apiPost(path, bodyObj) {
  try {
    const init = {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
    };
    if (bodyObj !== undefined) {
      init.body = JSON.stringify(bodyObj);
    }
    const res = await fetch(`${API_BASE}${path}`, init);
    if (res.status === 401) {
      const recovered = await handleUnauthorized();
      if (recovered) {
        return apiPost(path, bodyObj);
      }
      throw new Error("Unauthorized — sign in again");
    }
    const body = await res.text();
    let data = null;
    if (body) {
      try {
        data = JSON.parse(body);
      } catch {
        data = { error: body };
      }
    }
    if (!res.ok) {
      throw new Error(data?.error || data?.reason || `${res.status} ${body}`);
    }
    return data;
  } catch (err) {
    if (isFileProtocol() && err instanceof TypeError) {
      throw new Error(fileProtocolHelp());
    }
    throw err;
  }
}

const SAP_POST_ELIGIBLE_STATUSES = new Set(["Shipped", "Delivered", "Dispensed"]);

function canShowPostSap(row) {
  if (row.sap_sync?.status === "posted") return false;
  const lines = row.lines || [];
  return lines.some((line) =>
    SAP_POST_ELIGIBLE_STATUSES.has(String(line.status || "").trim())
  );
}

function renderPostSapButton(row) {
  if (!canShowPostSap(row)) return "";
  return '<button type="button" class="btn-post-sap" title="Post this order to SAP">Post-SAP</button>';
}

async function postOrderToSap(po, btn) {
  if (
    !window.confirm(
      `Post PO ${po} to SAP?\n\nThis builds the invoice payload and posts to SAP Service Layer.`
    )
  ) {
    return;
  }

  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "Posting…";

  try {
    const result = await apiPost(
      `/v1/walmart/sap-post?purchase_order_id=${encodeURIComponent(po)}`
    );
    await loadOrders(true);
    hideError();
    if (result?.status === "skipped") {
      showError(`SAP post skipped for PO ${po}: ${result.reason || "unknown reason"}`);
    }
  } catch (err) {
    showError(`SAP post failed for PO ${po}: ${err.message}`);
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}

function formatDateStack(value) {
  if (!value) {
    return '<span class="date-stack"><span class="date-line">—</span></span>';
  }
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) {
    return `<span class="date-stack"><span class="date-line">${escapeHtml(String(value))}</span></span>`;
  }
  const dateLine = d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
  const timeLine = d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return `<span class="date-stack"><span class="date-line">${dateLine}</span><span class="time-line">${timeLine}</span></span>`;
}

function statusBadge(status, label = status) {
  const key = String(status || "").toLowerCase();
  const map = {
    delivered: "badge-delivered",
    shipped: "badge-shipped",
    acknowledged: "badge-acknowledged",
    created: "badge-created",
    cancelled: "badge-cancelled",
    success: "badge-success",
    running: "badge-running",
    failed: "badge-failed",
  };
  const cls = map[key] || "badge-default";
  return `<span class="badge ${cls}">${escapeHtml(label || "—")}</span>`;
}

function lineStatusBadge(line) {
  const status = line?.status || "—";
  return statusBadge(status, line?.is_partial ? `${status} (Partial)` : status);
}

function lineQuantitySummary(line) {
  const total = line?.quantity_amount || "—";
  if (!line?.is_partial) return escapeHtml(total);
  return `${escapeHtml(total)} <span class="muted">(Delivered ${escapeHtml(
    line.invoiceable_quantity ?? line.delivered_quantity ?? "—"
  )}, Cancelled ${escapeHtml(line.cancelled_quantity ?? "—")})</span>`;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function truncate(text, max = 80) {
  const s = String(text || "");
  if (s.length <= max) return s;
  return `${s.slice(0, max)}…`;
}

function populateStatusFilter(byStatus) {
  const current = statusFilter.value;
  statusFilter.innerHTML = `
    <option value="">All statuses</option>
    <option value="partial">Delivered (Partial)</option>`;
  for (const row of byStatus || []) {
    if (String(row.status || "").toLowerCase() === "partial") continue;
    const opt = document.createElement("option");
    opt.value = row.status;
    opt.textContent = `${row.status} (${row.count})`;
    statusFilter.appendChild(opt);
  }
  statusFilter.value = current;
}

function setPageTitleRecords(countText) {
  if (!pageTitle) return;
  pageTitle.innerHTML = `Records <span class="page-title-muted">(<span id="record-count">${escapeHtml(countText)}</span>)</span>`;
}

function renderStatChips(stats) {
  if (!statChips) return;
  const sync = stats?.last_sync;
  const failed = stats?.sap_sync?.failed ?? 0;
  const posted = stats?.sap_sync?.posted ?? 0;
  const finished = sync?.sync_finished_at
    ? new Date(sync.sync_finished_at).toLocaleString()
    : "—";
  const syncLabel = sync
    ? `Last sync #${sync.id} · ${sync.status}`
    : "No sync runs yet";

  statChips.innerHTML = `
    <span class="stat-chip">${escapeHtml(String(stats?.total_orders ?? 0))} orders</span>
    <span class="stat-chip">${escapeHtml(String(stats?.total_lines ?? 0))} lines</span>
    <span class="stat-chip stat-chip--success">${escapeHtml(String(posted))} SAP posted</span>
    <span class="stat-chip ${failed > 0 ? "stat-chip--danger" : "stat-chip--muted"}">${escapeHtml(String(failed))} SAP failed</span>
    <span class="stat-chip stat-chip--muted">${escapeHtml(syncLabel)} · ${escapeHtml(finished)}</span>
  `;
}

function setOrdersChromeVisible(visible) {
  ordersFilterCard?.classList.toggle("hidden", !visible);
  statChips?.classList.toggle("hidden", !visible);
  sapRetryBtn?.classList.toggle("hidden", !visible);
  sapAlertWrap?.classList.toggle("hidden", !visible);
}

async function runSapRetryBatch(btn) {
  if (
    !window.confirm(
      "Retry all failed SAP posts?\n\nThis runs the same job as the daily 08:00 IST sap_retry schedule (up to 50 failed orders)."
    )
  ) {
    return;
  }

  const button = btn || sapRetryBtn;
  if (!button) return;

  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = "Retrying…";

  try {
    const result = await apiPost("/v1/walmart/sap-retry");
    hideError();
    await loadOrders(true);
    const posted = result?.posted ?? 0;
    const failed = result?.failed ?? 0;
    const skipped = result?.skipped ?? 0;
    const candidates = result?.candidates ?? 0;
    window.alert(
      `SAP retry finished.\n\nCandidates: ${candidates}\nPosted: ${posted}\nFailed: ${failed}\nSkipped: ${skipped}`
    );
  } catch (err) {
    showError(`SAP retry failed: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function setRecordCount(shown, total) {
  const el = document.getElementById("record-count");
  if (el) el.textContent = `${shown} of ${total}`;
}

function updatePagination(offset, total, itemCount) {
  pageTotal = total;
  const start = total === 0 ? 0 : offset + 1;
  const end = offset + itemCount;
  paginationInfo.textContent = `Showing ${start}–${end} of ${total}`;
  prevPageBtn.disabled = offset <= 0;
  nextPageBtn.disabled = end >= total;
}

function normalizeOrderItems(items) {
  if (!items?.length || items[0].lines) {
    return items || [];
  }

  const grouped = [];
  const byPo = new Map();

  for (const row of items) {
    const po = row.purchase_order_id;
    let order = byPo.get(po);
    if (!order) {
      order = {
        purchase_order_id: po,
        customer_order_id: row.customer_order_id,
        ship_node_type: row.ship_node_type,
        order_date_ms: row.order_date_ms,
        has_shipping_charges: row.has_shipping_charges,
        ship_to_state: row.ship_to_state,
        status_date: row.status_date,
        updated_at: row.updated_at,
        line_count: 0,
        lines: [],
      };
      byPo.set(po, order);
      grouped.push(order);
    }

    order.lines.push({
      line_number: row.line_number,
      status: row.status,
      status_date: row.status_date,
      sku: row.sku,
      product_name: row.product_name,
      quantity_amount: row.quantity_amount,
      tracking_number: row.tracking_number,
      carrier_name: row.carrier_name,
      updated_at: row.updated_at,
      raw_payload: row.raw_payload,
    });
    order.line_count += 1;
    if (row.status_date && (!order.status_date || row.status_date > order.status_date)) {
      order.status_date = row.status_date;
    }
    if (row.updated_at && (!order.updated_at || row.updated_at > order.updated_at)) {
      order.updated_at = row.updated_at;
    }
  }

  return grouped;
}

function sortedLines(lines) {
  return [...(lines || [])].sort((a, b) => {
    const an = parseInt(a.line_number, 10);
    const bn = parseInt(b.line_number, 10);
    if (!Number.isNaN(an) && !Number.isNaN(bn)) return an - bn;
    return String(a.line_number || "").localeCompare(String(b.line_number || ""));
  });
}

function formatPacificDateOnly(value) {
  if (value == null || value === "") return "—";
  const text = String(value).slice(0, 10);
  const parts = text.split("-");
  if (parts.length !== 3) return text;
  const d = new Date(Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2])));
  if (Number.isNaN(d.getTime())) return text;
  const date = d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
  return `${date} Pacific`;
}

function lineShipDateTimeMsFromJson(jsonLine) {
  if (!jsonLine) return null;
  const statuses = asArray(jsonLine.orderLineStatuses?.orderLineStatus);
  if (!statuses.length) return null;
  const shipDates = statuses
    .map((status) => Number(status?.trackingInfo?.shipDateTime))
    .filter((ms) => Number.isFinite(ms) && ms > 0);
  return shipDates.length ? Math.max(...shipDates) : null;
}

function resolveShipDateTimeMs(line, jsonLine) {
  if (line?.ship_date_time_ms != null) return line.ship_date_time_ms;
  return lineShipDateTimeMsFromJson(jsonLine);
}

function formatShipDatePacific(line) {
  const ms = line?.ship_date_time_ms;
  if (ms != null) {
    return formatOrderDatePacific(ms);
  }
  if (line?.ship_date_pacific) {
    return formatPacificDateOnly(line.ship_date_pacific);
  }
  return "—";
}

function orderShipDatePacificLabel(detail) {
  const jsonLines = orderLineItemsFromDetail(detail);
  const jsonByLine = new Map(jsonLines.map((line) => [String(line.lineNumber), line]));
  const lines = detail?.lines || [];
  let maxMs = null;
  let maxPacific = null;
  for (const line of lines) {
    const ms = resolveShipDateTimeMs(line, jsonByLine.get(String(line.line_number)));
    if (ms != null && (maxMs == null || ms > maxMs)) {
      maxMs = ms;
      maxPacific = line.ship_date_pacific || null;
    }
  }
  if (maxMs == null) {
    for (const jsonLine of jsonLines) {
      const ms = lineShipDateTimeMsFromJson(jsonLine);
      if (ms != null && (maxMs == null || ms > maxMs)) maxMs = ms;
    }
  }
  if (maxMs != null) return formatOrderDatePacific(maxMs);
  if (maxPacific) return formatPacificDateOnly(maxPacific);
  return "—";
}

function formatOrderDateUtc(ms) {
  return formatOrderDateWithTz(ms, "UTC", "UTC");
}

function formatOrderDatePacific(ms) {
  return formatOrderDateWithTz(ms, "America/Los_Angeles", "Pacific");
}

function formatOrderDateWithTz(ms, timeZone, tzLabel) {
  if (ms == null || ms === "") return "—";
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) return "—";
  const date = d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone,
  });
  const time = d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
  });
  return `${date} ${time} ${tzLabel}`;
}

function formatDateStackFromMs(ms) {
  return formatDateStackFromMsWithTz(ms, "UTC", "UTC");
}

function formatDateStackFromMsPacific(ms) {
  return formatDateStackFromMsWithTz(ms, "America/Los_Angeles", "Pacific");
}

function formatDateStackFromMsWithTz(ms, timeZone, tzLabel) {
  if (ms == null || ms === "") {
    return '<span class="date-stack"><span class="date-line">—</span></span>';
  }
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) {
    return '<span class="date-stack"><span class="date-line">—</span></span>';
  }
  const dateLine = d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone,
  });
  const timeLine = d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
  });
  return `<span class="date-stack"><span class="date-line">${dateLine}</span><span class="time-line">${timeLine} ${tzLabel}</span></span>`;
}

function displayLineIndex(index) {
  return String(index + 1);
}

function shippingLabel(applicable) {
  if (applicable === true) return '<span class="ship-tag ship-yes">Shipping</span>';
  if (applicable === false) return '<span class="ship-tag ship-no">No shipping</span>';
  return "";
}

function renderLineStack(lines, renderLine) {
  const ordered = sortedLines(lines);
  if (!ordered.length) {
    return '<span class="line-stack">—</span>';
  }
  return ordered.map((line, index) => renderLine(line, index)).join("");
}

function sapAttemptLabel(sync, { emphasize = false } = {}) {
  const count = sync?.attempt_count;
  if (count == null || count < 1) return "";
  const label = count === 1 ? "1 try" : `${count} tries`;
  const cls = emphasize ? "sap-attempt-pill sap-attempt-pill--warn" : "sap-attempt-pill";
  return `<span class="${cls}" title="SAP sync attempts">↻ ${escapeHtml(label)}</span>`;
}

function renderSapSyncCell(row) {
  const sync = row.sap_sync;
  if (!sync || !sync.status) {
    return '<span class="sap-sync-empty">—</span>';
  }
  if (sync.status === "posted") {
    const entry = sync.sap_doc_entry != null ? sync.sap_doc_entry : "—";
    const num = sync.sap_doc_num != null ? sync.sap_doc_num : "—";
    return `
      <div class="sap-sync-cell sap-sync-ok">
        <span class="badge badge-success">Posted</span>
        <div class="sap-sync-meta">DocEntry: ${escapeHtml(String(entry))}</div>
        <div class="sap-sync-meta">DocNum: ${escapeHtml(String(num))}</div>
        ${sapAttemptLabel(sync)}
      </div>`;
  }
  if (sync.status === "failed") {
    return `
      <div class="sap-sync-cell sap-sync-fail">
        <div class="sap-sync-status-row">
          <span class="badge badge-failed">Failed</span>
          ${sapAttemptLabel(sync, { emphasize: true })}
        </div>
        <button type="button" class="btn-sap-error" data-po="${escapeHtml(row.purchase_order_id)}">See error</button>
      </div>`;
  }
  if (sync.status === "prepared") {
    return `
      <div class="sap-sync-cell">
        <span class="badge badge-acknowledged">Prepared</span>
        ${sapAttemptLabel(sync)}
        <div class="sap-sync-meta">Not posted yet</div>
      </div>`;
  }
  return `<span class="sap-sync-empty">${escapeHtml(sync.status)}</span>`;
}

function closeSapErrorModal() {
  if (!sapErrorModal) return;
  sapErrorModal.classList.add("hidden");
  sapErrorModal.setAttribute("aria-hidden", "true");
}

async function openSapErrorModal(po) {
  if (!sapErrorModal) return;
  sapErrorPo.textContent = `PO ${po}`;
  sapErrorText.textContent = "Loading…";
  sapErrorModal.classList.remove("hidden");
  sapErrorModal.setAttribute("aria-hidden", "false");

  try {
    const detail = await apiGet(
      `/v1/walmart/sap-sync?purchase_order_id=${encodeURIComponent(po)}`
    );
    const parts = [];
    if (detail.error_phase) {
      parts.push(`Phase: ${detail.error_phase}`);
    }
    if (detail.attempt_count != null && detail.attempt_count > 0) {
      parts.push(`Sync attempts: ${detail.attempt_count}`);
    }
    if (detail.error_message) {
      parts.push(detail.error_message);
    }
    if (detail.sap_response) {
      parts.push("\n--- SAP response ---\n" + JSON.stringify(detail.sap_response, null, 2));
    }
    sapErrorText.textContent = parts.length ? parts.join("\n\n") : "No error details stored.";
  } catch (err) {
    sapErrorText.textContent = err.message || String(err);
  }
}

if (sapErrorBackdrop) {
  sapErrorBackdrop.addEventListener("click", closeSapErrorModal);
}
if (sapErrorClose) {
  sapErrorClose.addEventListener("click", closeSapErrorModal);
}

function renderOrders(items, offset, total) {
  if (!items.length) {
    ordersBody.innerHTML =
      `<tr><td colspan="${COLSPAN}" class="loading-cell">No orders found.</td></tr>`;
    setRecordCount(0, total);
    updatePagination(offset, total, 0);
    return;
  }

  setRecordCount(items.length, total);
  updatePagination(offset, total, items.length);

  ordersBody.innerHTML = items
    .map((row, i) => {
      const lines = sortedLines(row.lines);
      const lineCount = lines.length;
      const poMetaParts = [];
      if (lineCount > 1) poMetaParts.push(`<span class="po-line-count">${lineCount} lines</span>`);
      if (row.has_shipping_charges === true || row.has_shipping_charges === false) {
        poMetaParts.push(shippingLabel(row.has_shipping_charges));
      }
      const poMeta = poMetaParts.length
        ? `<div class="po-meta">${poMetaParts.join("")}</div>`
        : "";
      return `
    <tr class="data-row" data-po="${escapeHtml(row.purchase_order_id)}">
      <td class="cell-center cell-muted">${offset + i + 1}</td>
      <td>
        <span class="po-number">${escapeHtml(row.purchase_order_id)}</span>
        ${poMeta}
      </td>
      <td class="cell-mono">${escapeHtml(row.customer_order_id || "—")}</td>
      <td class="cell-center cell-state">${escapeHtml(row.ship_to_state || "—")}</td>
      <td>${formatDateStackFromMs(row.order_date_ms)}</td>
      <td>${formatDateStackFromMsPacific(row.order_date_ms)}</td>
      <td class="cell-stack cell-center">${renderLineStack(lines, (_line, index) => `<div class="line-stack line-num">${displayLineIndex(index)}</div>`)}</td>
      <td class="cell-stack">${renderLineStack(lines, (line) => `<div class="line-stack">${lineStatusBadge(line)}</div>`)}</td>
      <td class="cell-stack cell-mono">${renderLineStack(lines, (line) => `<div class="line-stack">${escapeHtml(line.sku || "—")}</div>`)}</td>
      <td class="cell-stack cell-center">${renderLineStack(lines, (line) => `<div class="line-stack">${lineQuantitySummary(line)}</div>`)}</td>
      <td class="cell-stack">${renderLineStack(lines, (line) => `<div class="line-stack">${formatDateStack(line.status_date)}</div>`)}</td>
      <td>${formatDateStack(row.updated_at)}</td>
      <td class="cell-sap-sync">${renderSapSyncCell(row)}</td>
      <td class="cell-center cell-actions">
        <button type="button" class="btn-view">View</button>
        ${renderPostSapButton(row)}
      </td>
    </tr>`;
    })
    .join("");

  ordersBody.querySelectorAll(".data-row .btn-view").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest(".data-row");
      toggleOrderDetail(btn, row.getAttribute("data-po"));
    });
  });

  ordersBody.querySelectorAll(".data-row .btn-post-sap").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const row = btn.closest(".data-row");
      postOrderToSap(row.getAttribute("data-po"), btn);
    });
  });

  ordersBody.querySelectorAll(".btn-sap-error").forEach((btn) => {
    btn.addEventListener("click", () => {
      openSapErrorModal(btn.getAttribute("data-po"));
    });
  });
}

async function fetchOrderDetail(po) {
  try {
    return await apiGet(
      `/v1/walmart/order-detail?purchase_order_id=${encodeURIComponent(po)}`
    );
  } catch {
    const page = await apiGet(
      `/v1/walmart/orders?purchase_order_id=${encodeURIComponent(po)}&limit=1`
    );
    const grouped = page.items?.[0];
    const lines = grouped?.lines || [];
    if (!lines.length) {
      throw new Error(`No order found for PO ${po}`);
    }
    return {
      purchase_order_id: po,
      customer_order_id: grouped.customer_order_id,
      ship_node_type: grouped.ship_node_type,
      line_count: lines.length,
      lines,
      json_unavailable: true,
    };
  }
}

function formatJson(value) {
  if (value == null) return "{}";
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

function asArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function getOrderFromDetail(detail) {
  if (detail?.order_json?.order) return detail.order_json.order;
  if (detail?.raw_payload?.order) return detail.raw_payload.order;
  if (!detail?.raw_payload) return null;
  try {
    const parsed =
      typeof detail.raw_payload === "string"
        ? JSON.parse(detail.raw_payload)
        : detail.raw_payload;
    return parsed?.order || null;
  } catch {
    return null;
  }
}

function orderLineItemsFromDetail(detail) {
  const order = getOrderFromDetail(detail);
  if (!order) return [];
  return asArray(order.orderLines?.orderLine);
}

function parseLineCharges(orderLine) {
  const charges = asArray(orderLine?.charges?.charge);
  const taxLines = [];
  const result = {
    itemPrice: null,
    discount: 0,
    netItemPrice: null,
    currency: "USD",
    productTax: null,
    shipping: null,
    shippingTax: null,
    taxLines,
    totalTax: 0,
  };

  function promotionDiscountAmount(charge) {
    const chargeName = String(charge.chargeName || "").trim();
    const chargeType = String(charge.chargeType || "").toUpperCase();
    const amount = charge.chargeAmount?.amount;
    if (amount == null || Number(amount) >= 0) return null;
    if (chargeName === "Promotion" || chargeType === "PRODUCT") {
      return Math.abs(Number(amount));
    }
    return null;
  }

  function appendTaxLine(name, amount, category) {
    if (amount == null || Number.isNaN(Number(amount)) || Number(amount) <= 0) return;
    taxLines.push({
      name,
      amount: Number(amount),
      category,
    });
  }

  function chargeTax(charge) {
    const taxName = charge.tax?.taxName;
    let taxAmount = charge.tax?.taxAmount?.amount;
    if (taxAmount == null) {
      taxAmount = charge.taxAndOtherFees?.taxAndOtherFeesAmount?.amount;
    }
    const label = taxName || charge.taxAndOtherFees?.taxAndOtherFeesName || null;
    return { name: label, amount: taxAmount != null ? Number(taxAmount) : null };
  }

  for (const charge of charges) {
    const chargeType = String(charge.chargeType || "").toUpperCase();
    const chargeName = String(charge.chargeName || "");
    const amount = charge.chargeAmount?.amount;
    const { name: taxName, amount: taxAmount } = chargeTax(charge);

    if (chargeType === "PRODUCT" || chargeName === "ItemPrice") {
      result.itemPrice = amount != null ? Number(amount) : null;
      result.productTax = taxAmount;
      result.currency = charge.chargeAmount?.currency || result.currency;
      appendTaxLine("Product tax", taxAmount, "product");
      continue;
    }

    if (chargeType === "SHIPPING" || chargeName === "Shipping") {
      result.shipping = amount != null ? Number(amount) : null;
      result.shippingTax = taxAmount;
      appendTaxLine("Shipping tax", taxAmount, "shipping");
      continue;
    }

    if (taxAmount != null && taxAmount > 0) {
      appendTaxLine(taxName || chargeName || "Other tax", taxAmount, "other");
    }
  }

  for (const refundCharge of asArray(orderLine?.refund?.refundCharges?.refundCharge)) {
    const amount = promotionDiscountAmount(refundCharge?.charge || {});
    if (amount != null) result.discount += amount;
  }

  for (const charge of charges) {
    const amount = promotionDiscountAmount(charge);
    if (amount != null) result.discount += amount;
  }

  result.discount = result.discount > 0 ? Math.round(result.discount * 100) / 100 : 0;
  if (result.itemPrice != null) {
    result.netItemPrice = Math.round((result.itemPrice - result.discount) * 100) / 100;
    result.grossUnitPrice = perUnitPrice(result.itemPrice, orderLine?.orderLineQuantity?.amount || "1");
    result.netUnitPrice = perUnitPrice(result.netItemPrice, orderLine?.orderLineQuantity?.amount || "1");
  }

  result.totalTax = taxLines.reduce((sum, row) => sum + row.amount, 0);
  return result;
}

function renderTaxRows(charges, currency, hasPricing, hasShipping, qty) {
  if (!hasPricing || !charges.taxLines?.length) {
    return '<div><dt>Total tax</dt><dd class="money">—</dd></div>';
  }

  const rows = charges.taxLines
    .filter((tax) => tax.category !== "shipping" || hasShipping)
    .map(
      (tax) => `
        <div><dt>${escapeHtml(tax.name)} (total)</dt><dd class="money">${formatMoney(tax.amount, currency)}</dd></div>
        <div><dt>${escapeHtml(tax.name)} (per unit)</dt><dd class="money">${formatMoney(perUnitPrice(tax.amount, qty), currency)}</dd></div>`
    )
    .join("");

  return `
    ${rows}
    <div class="detail-dl-subtotal"><dt>Total tax</dt><dd class="money">${formatMoney(charges.totalTax, currency)}</dd></div>`;
}

function perUnitPrice(itemPrice, qty) {
  if (itemPrice == null || Number.isNaN(Number(itemPrice))) return null;
  const q = parseInt(qty, 10) || 1;
  return Number(itemPrice) / q;
}

function formatMoney(amount, currency = "USD") {
  if (amount == null || Number.isNaN(Number(amount))) return "—";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
    }).format(Number(amount));
  } catch {
    return `${currency} ${Number(amount).toFixed(2)}`;
  }
}

function mergeLinesWithCharges(dbLines, jsonLines) {
  const ordered = sortedLines(dbLines);
  const jsonByLineNumber = new Map(
    jsonLines.map((line) => [String(line.lineNumber), line])
  );

  return ordered.map((line, index) => {
    const jsonLine =
      jsonByLineNumber.get(String(line.line_number)) || jsonLines[index] || null;
    const charges = jsonLine ? parseLineCharges(jsonLine) : {};
    const qty = parseInt(
      line.quantity_amount || jsonLine?.orderLineQuantity?.amount || "1",
      10
    ) || 1;
    const hasShipping = charges.shipping != null && charges.shipping > 0;
    const netItemPrice =
      charges.netItemPrice != null
        ? charges.netItemPrice
        : charges.itemPrice != null
          ? charges.itemPrice - (charges.discount || 0)
          : null;
    const lineTotal =
      (netItemPrice ?? 0) +
      (charges.totalTax ?? 0) +
      (charges.shipping ?? 0);

    return {
      ...line,
      ship_date_time_ms: resolveShipDateTimeMs(line, jsonLine),
      charges: {
        ...charges,
        grossUnitPrice: charges.grossUnitPrice ?? perUnitPrice(charges.itemPrice, qty),
        unitPrice:
          charges.discount > 0
            ? (charges.netUnitPrice ?? perUnitPrice(netItemPrice, qty))
            : (charges.grossUnitPrice ?? perUnitPrice(charges.itemPrice, qty)),
      },
      qty,
      hasShipping,
      lineTotal: jsonLine ? lineTotal : null,
      shipMethod: jsonLine?.fulfillment?.shipMethod || null,
    };
  });
}

function renderDiscountRow(charges, currency, hasPricing, qty) {
  if (!hasPricing || !charges.discount) return "";
  return `<div><dt>Promotion (total)</dt><dd class="money money-discount">-${formatMoney(charges.discount, currency)}</dd></div>
    <div><dt>Promotion (per unit)</dt><dd class="money money-discount">-${formatMoney(perUnitPrice(charges.discount, qty), currency)}</dd></div>`;
}

function renderUnitPriceRow(charges, currency, hasPricing) {
  if (!hasPricing || charges.unitPrice == null) {
    return `<div><dt>Unit price</dt><dd class="money">—</dd></div>`;
  }
  if (charges.discount > 0) {
    return `<div><dt>Unit price (Discount applied)</dt><dd class="money">${formatMoney(charges.unitPrice, currency)}</dd></div>`;
  }
  return `<div><dt>Unit price</dt><dd class="money">${formatMoney(charges.unitPrice, currency)}</dd></div>`;
}

function renderLineDetailBox(row, index, currency, hasPricing) {
  const c = row.charges;
  const shippingCell = row.hasShipping
    ? formatMoney(c.shipping, currency)
    : "Not applicable";

  return `
    <div class="line-item-box">
      <div class="line-item-head">
        <span class="line-item-num">Line ${displayLineIndex(index)}</span>
        ${lineStatusBadge(row)}
        <span class="line-item-meta cell-mono">${escapeHtml(row.sku || "—")}</span>
        <span class="line-item-meta">Qty ${lineQuantitySummary({...row, quantity_amount: row.qty})}</span>
      </div>
      <p class="line-item-product">${escapeHtml(row.product_name || "—")}</p>
      <div class="line-item-panels">
        <div class="line-item-panel">
          <div class="panel-title">Charges &amp; tax</div>
          <dl class="detail-dl">
            <div><dt>Item price (total)</dt><dd class="money">${hasPricing ? formatMoney(c.itemPrice, currency) : "—"}</dd></div>
            ${renderDiscountRow(c, currency, hasPricing, row.qty)}
            ${renderUnitPriceRow(c, currency, hasPricing)}
            ${renderTaxRows(c, currency, hasPricing, row.hasShipping, row.qty)}
            <div><dt>Shipping (total)</dt><dd class="money">${hasPricing ? shippingCell : "—"}</dd></div>
            <div><dt>Shipping (per unit)</dt><dd class="money">${hasPricing && row.hasShipping ? formatMoney(perUnitPrice(c.shipping, row.qty), currency) : "—"}</dd></div>
            <div class="detail-dl-total"><dt>Line total</dt><dd class="money money-total">${hasPricing ? formatMoney(row.lineTotal, currency) : "—"}</dd></div>
          </dl>
        </div>
        <div class="line-item-panel">
          <div class="panel-title">Shipment</div>
          <dl class="detail-dl">
            <div><dt>Tracking</dt><dd class="cell-mono">${escapeHtml(row.tracking_number || "—")}</dd></div>
            <div><dt>Carrier</dt><dd>${escapeHtml(row.carrier_name || "—")}</dd></div>
            <div><dt>Ship date (Pacific)</dt><dd>${escapeHtml(formatShipDatePacific(row))}</dd></div>
            <div><dt>Ship method</dt><dd>${escapeHtml(row.shipMethod || "—")}</dd></div>
          </dl>
        </div>
      </div>
    </div>`;
}

function renderUnifiedOrderTable(detail, lines) {
  const enriched = mergeLinesWithCharges(lines, orderLineItemsFromDetail(detail));
  const hasPricing = enriched.some((row) => row.charges.itemPrice != null);
  const currency = enriched.find((row) => row.charges.currency)?.charges.currency || "USD";

  let subtotal = 0;
  let totalDiscount = 0;
  let totalTax = 0;
  let shipping = 0;
  const taxBreakdown = new Map();

  const lineBoxes = enriched
    .map((row, index) => {
      const c = row.charges;
      if (c.itemPrice != null) subtotal += c.itemPrice;
      if (c.discount) totalDiscount += c.discount;
      if (c.totalTax != null) totalTax += c.totalTax;
      if (c.shipping != null) shipping += c.shipping;
      for (const tax of c.taxLines || []) {
        taxBreakdown.set(tax.name, (taxBreakdown.get(tax.name) || 0) + tax.amount);
      }
      return renderLineDetailBox(row, index, currency, hasPricing);
    })
    .join("");

  const orderTotal = subtotal - totalDiscount + totalTax + shipping;

  const taxRows = [...taxBreakdown.entries()]
    .map(
      ([name, amount]) =>
        `<div><dt>${escapeHtml(name)}</dt><dd class="money">${formatMoney(amount, currency)}</dd></div>`
    )
    .join("");

  const totalsBox = hasPricing
    ? `
    <div class="order-totals-box">
      <div class="panel-title">Order totals</div>
      <dl class="totals-dl">
        <div><dt>Subtotal</dt><dd class="money">${formatMoney(subtotal, currency)}</dd></div>
        ${totalDiscount > 0 ? `<div><dt>Promotion</dt><dd class="money money-discount">-${formatMoney(totalDiscount, currency)}</dd></div>` : ""}
        ${taxRows}
        <div><dt>Total tax</dt><dd class="money">${formatMoney(totalTax, currency)}</dd></div>
        <div><dt>Shipping</dt><dd class="money">${shipping > 0 ? formatMoney(shipping, currency) : "—"}</dd></div>
        <div class="totals-dl-grand"><dt>Grand total</dt><dd class="money money-total">${formatMoney(orderTotal, currency)}</dd></div>
      </dl>
    </div>`
    : `<p class="muted order-pricing-note">Pricing details appear when full order JSON is stored from sync.</p>`;

  return `
    <div class="order-detail-box">
      <div class="order-detail-box-title">Order lines</div>
      <div class="line-items-stack">${lineBoxes}</div>
      ${totalsBox}
    </div>`;
}

function sapSyncStatusLabel(sapSync) {
  if (!sapSync?.status) return "—";
  if (sapSync.status === "posted") return "Posted";
  if (sapSync.status === "failed") return "Failed";
  if (sapSync.status === "prepared") return "Prepared";
  return sapSync.status;
}

function hasSapPayload(sapSync) {
  const payload = sapSync?.sap_payload;
  if (payload == null) return false;
  if (typeof payload === "string") {
    const trimmed = payload.trim();
    return trimmed !== "" && trimmed !== "{}";
  }
  if (typeof payload === "object") {
    return Object.keys(payload).length > 0;
  }
  return false;
}

function isSapPayloadPreview(payload) {
  if (!payload || typeof payload !== "object") return false;
  return payload._meta?.preview === true;
}

function renderSapPayloadSection(sapSync) {
  if (!sapSync) {
    return `
      <details class="trace-section json-collapse">
        <summary class="trace-section-title">SAP request payload (JSON)</summary>
        <p class="muted">No SAP sync attempt yet — payload appears after a line ships.</p>
      </details>`;
  }

  if (hasSapPayload(sapSync)) {
    const payload = sapSync.sap_payload;
    const preview = isSapPayloadPreview(payload);
    const hint = preview
      ? `<p class="sap-payload-hint sap-payload-hint--preview"><strong>Preview only</strong> — ${escapeHtml(payload._meta?.note || "Build failed before POST. Showing partial payload for review.")}${payload._meta?.build_error ? `<br><span class="muted">Build error: ${escapeHtml(payload._meta.build_error)}</span>` : ""}</p>`
      : `<p class="muted sap-payload-hint">This is the body sent to SAP Service Layer (<code>POST /Invoices</code>). Use it to verify item codes, batches, warehouse, and accounts.</p>`;
    return `
      <details class="trace-section json-collapse" open>
        <summary class="trace-section-title">SAP request payload (JSON)${preview ? " — preview" : ""}</summary>
        ${hint}
        <pre class="json-pre">${escapeHtml(formatJson(payload))}</pre>
      </details>`;
  }

  const phase = sapSync.error_phase || sapSync.status;
  return `
    <details class="trace-section json-collapse">
      <summary class="trace-section-title">SAP request payload (JSON)</summary>
      <p class="muted">No SAP request payload stored${phase === "build" ? " — error occurred while building the payload (before POST)." : "."}</p>
    </details>`;
}

function renderSapTraceSection(sapSync) {
  if (!sapSync) {
    return `
      <details class="trace-section json-collapse sap-trace-section">
        <summary class="trace-section-title">SAP sync</summary>
        <p class="muted sap-trace-empty">No SAP sync attempt recorded for this PO yet (triggers when a line ships).</p>
      </details>`;
  }

  const status = sapSync.status;
  let summaryHtml = "";

  if (status === "posted") {
    summaryHtml = `
      <div class="sap-trace-summary sap-trace-ok">
        <span class="badge badge-success">Posted to SAP</span>
        <dl class="detail-dl sap-trace-dl">
          <div><dt>DocEntry</dt><dd class="cell-mono">${escapeHtml(String(sapSync.sap_doc_entry ?? "—"))}</dd></div>
          <div><dt>DocNum</dt><dd class="cell-mono">${escapeHtml(String(sapSync.sap_doc_num ?? "—"))}</dd></div>
          ${sapSync.attempt_count > 0 ? `<div><dt>Sync attempts</dt><dd>${escapeHtml(String(sapSync.attempt_count))}</dd></div>` : ""}
          ${sapSync.updated_at ? `<div><dt>Synced at</dt><dd>${formatDateStack(sapSync.updated_at)}</dd></div>` : ""}
        </dl>
      </div>`;
  } else if (status === "failed") {
    summaryHtml = `
      <div class="sap-trace-summary sap-trace-fail">
        <span class="badge badge-failed">Failed</span>
        ${sapSync.error_phase ? `<p class="sap-trace-phase">Phase: <strong>${escapeHtml(sapSync.error_phase)}</strong></p>` : ""}
        ${sapSync.attempt_count > 0 ? `<p class="sap-trace-phase">Sync attempts: <strong>${escapeHtml(String(sapSync.attempt_count))}</strong></p>` : ""}
        <pre class="sap-error-inline">${escapeHtml(sapSync.error_message || "Unknown error")}</pre>
      </div>`;
  } else if (status === "prepared") {
    summaryHtml = `
      <div class="sap-trace-summary">
        <span class="badge badge-acknowledged">Prepared</span>
        <p class="muted">Payload built; not posted to SAP yet.</p>
      </div>`;
  } else {
    summaryHtml = `<p class="muted">Status: ${escapeHtml(status)}</p>`;
  }

  const hasResponse = sapSync.sap_response != null;
  const responseBlock =
    hasResponse || status === "posted"
      ? `
        <div class="sap-response-block">
          <div class="panel-title">SAP response</div>
          ${
            hasResponse
              ? `<pre class="json-pre">${escapeHtml(formatJson(sapSync.sap_response))}</pre>`
              : '<p class="muted">No SAP response body stored.</p>'
          }
        </div>`
      : "";

  return `
    <details class="trace-section json-collapse sap-trace-section" open>
      <summary class="trace-section-title">SAP sync</summary>
      ${summaryHtml}
      ${responseBlock}
    </details>`;
}

function getShippingStateFromDetail(detail) {
  if (detail?.ship_to_state) return detail.ship_to_state;
  const order = getOrderFromDetail(detail);
  const state = order?.shippingInfo?.postalAddress?.state;
  return state ? String(state).trim().toUpperCase() : null;
}

function renderTracePanel(detail, highlightLine) {
  const lines = sortedLines(detail.lines || []);

  const jsonBlocks = detail.json_unavailable
    ? '<p class="muted">Raw JSON will appear here after you redeploy the API (order-detail endpoint).</p>'
    : (() => {
        if (detail.order_json) {
          return `<pre class="json-pre">${escapeHtml(formatJson(detail.order_json))}</pre>`;
        }
        if (detail.raw_payload) {
          return `<pre class="json-pre">${escapeHtml(formatJson(detail.raw_payload))}</pre>`;
        }
        return '<p class="muted">No raw JSON stored for this order yet. It will appear after the next Walmart sync.</p>';
      })();

  return `
    <div class="trace-panel">
      <div class="trace-header">
        <div>
          <div class="trace-title">Order trace</div>
          <div class="trace-sub">
            PO ${escapeHtml(detail.purchase_order_id)} · ${lines.length} line item(s)
            · Order date ${escapeHtml(formatOrderDateUtc(detail.order_date_ms))}
            · ${escapeHtml(formatOrderDatePacific(detail.order_date_ms))}
          </div>
        </div>
        <button type="button" class="btn-outline btn-hide-trace">Hide trace</button>
      </div>

      <div class="trace-cards">
        <div class="trace-card"><span class="trace-card-label">Customer order</span><span class="trace-card-value">${escapeHtml(detail.customer_order_id || "—")}</span></div>
        <div class="trace-card"><span class="trace-card-label">Order date (UTC)</span><span class="trace-card-value">${escapeHtml(formatOrderDateUtc(detail.order_date_ms))}</span></div>
        <div class="trace-card"><span class="trace-card-label">Order date (Pacific)</span><span class="trace-card-value">${escapeHtml(formatOrderDatePacific(detail.order_date_ms))}</span></div>
        <div class="trace-card"><span class="trace-card-label">Ship date (Pacific)</span><span class="trace-card-value">${escapeHtml(orderShipDatePacificLabel(detail))}</span></div>
        <div class="trace-card"><span class="trace-card-label">Ship to state (PayTo / ShipTo)</span><span class="trace-card-value">${escapeHtml(getShippingStateFromDetail(detail) || "—")}</span></div>
        <div class="trace-card"><span class="trace-card-label">Shipping charge</span><span class="trace-card-value">${detail.has_shipping_charges ? "Applicable" : "Not applicable"}</span></div>
        <div class="trace-card"><span class="trace-card-label">Ship node</span><span class="trace-card-value">${escapeHtml(detail.ship_node_type || "—")}</span></div>
        <div class="trace-card"><span class="trace-card-label">Line items</span><span class="trace-card-value">${lines.length}</span></div>
        <div class="trace-card"><span class="trace-card-label">SAP sync</span><span class="trace-card-value">${escapeHtml(sapSyncStatusLabel(detail.sap_sync))}</span></div>
      </div>

      ${renderUnifiedOrderTable(detail, lines)}

      ${renderSapTraceSection(detail.sap_sync)}

      ${renderSapPayloadSection(detail.sap_sync)}

      <details class="trace-section json-collapse">
        <summary class="trace-section-title">Walmart raw payload (JSON)</summary>
        ${jsonBlocks || '<p class="muted">No JSON stored.</p>'}
      </details>
    </div>`;
}

function closeAllTracePanels() {
  ordersBody.querySelectorAll(".detail-row").forEach((row) => row.remove());
  ordersBody.querySelectorAll(".btn-view").forEach((btn) => {
    btn.textContent = "View";
    btn.classList.remove("active");
  });
}

async function toggleOrderDetail(btn, po) {
  const parentTr = btn.closest("tr");
  const next = parentTr.nextElementSibling;
  if (next?.classList.contains("detail-row")) {
    next.remove();
    btn.textContent = "View";
    btn.classList.remove("active");
    return;
  }

  closeAllTracePanels();

  const detailTr = document.createElement("tr");
  detailTr.className = "detail-row";
  detailTr.innerHTML =
    `<td colspan="${COLSPAN}"><div class="trace-panel trace-loading">Loading order trace…</div></td>`;
  parentTr.after(detailTr);
  btn.textContent = "Hide";
  btn.classList.add("active");

  try {
    const data = await fetchOrderDetail(po);
    detailTr.querySelector("td").innerHTML = renderTracePanel(data, null);
    detailTr.querySelector(".btn-hide-trace")?.addEventListener("click", () => {
      detailTr.remove();
      btn.textContent = "View";
      btn.classList.remove("active");
    });
  } catch (err) {
    detailTr.querySelector("td").innerHTML = `<div class="trace-panel trace-error">${escapeHtml(err.message)}</div>`;
  }
}

function renderSyncLog(items) {
  syncBody.innerHTML = (items || [])
    .map(
      (row, i) => `
    <tr>
      <td>${row.id ?? i + 1}</td>
      <td>${statusBadge(row.status)}</td>
      <td>${formatDateStack(row.sync_finished_at)}</td>
      <td>${row.orders_seen ?? "—"}</td>
      <td>${row.lines_stored ?? "—"}</td>
      <td>${escapeHtml(row.last_modified_start_date || "—")} → ${escapeHtml(row.last_modified_end_date || "—")}</td>
    </tr>`
    )
    .join("");
}

function renderSapItems(items, count, schema) {
  const rows = items || [];
  if (!rows.length) {
    sapItemsBody.innerHTML =
      '<tr><td colspan="4" class="loading-cell">No FG items found in SAP (ItemCode LIKE FG%).</td></tr>';
    sapItemsMeta.textContent = "0 items from SAP HANA";
    return;
  }

  sapItemsMeta.textContent = `${count ?? rows.length} items from SAP HANA · schema ${escapeHtml(schema || "default")}`;
  sapItemsBody.innerHTML = rows
    .map(
      (row, i) => `
    <tr>
      <td>${i + 1}</td>
      <td class="cell-mono">${escapeHtml(row.item_code || "—")}</td>
      <td class="cell-mono">${escapeHtml(row.amz_sku || "—")}</td>
      <td class="cell-mono">${escapeHtml(row.wm_sku || "—")}</td>
    </tr>`
    )
    .join("");
}

function updateSapAlertBell(statsOrTotal) {
  if (!sapAlertBell || !sapAlertCount) return;
  const failed =
    typeof statsOrTotal === "number"
      ? statsOrTotal
      : statsOrTotal?.sap_sync?.failed ?? statsOrTotal?.total ?? 0;
  sapAlertCount.textContent = String(failed);
  sapAlertCount.classList.toggle("hidden", failed < 1);
  sapAlertBell.classList.toggle("sap-alert-bell--active", failed > 0);
  sapAlertBell.classList.toggle("sap-alert-bell--filter-on", sapSyncFilter?.value === "failed");
  sapAlertBell.title =
    failed > 0
      ? `${failed} SAP invoice sync failure(s) — click to view`
      : "No SAP sync failures";
  if (sapAlertPanelCount) sapAlertPanelCount.textContent = String(failed);
}

function truncateError(text, max = 140) {
  const s = String(text || "").trim();
  if (!s) return "No error message stored.";
  return s.length <= max ? s : `${s.slice(0, max - 3)}...`;
}

function renderSapFailurePanelItems(items) {
  if (!items?.length) {
    return '<p class="muted sap-alert-empty">No SAP sync failures.</p>';
  }
  return items
    .map((row) => {
      const po = escapeHtml(row.purchase_order_id || "—");
      const phase = row.error_phase ? escapeHtml(row.error_phase) : "—";
      const tries = row.attempt_count > 0 ? `${row.attempt_count} try(s)` : "—";
      const err = escapeHtml(truncateError(row.error_message, 200));
      return `
        <article class="sap-alert-item">
          <div class="sap-alert-item-head">
            <span class="sap-alert-item-po">${po}</span>
            <span class="sap-attempt-pill sap-attempt-pill--warn">↻ ${escapeHtml(tries)}</span>
          </div>
          <div class="sap-alert-item-meta">Phase: ${phase}</div>
          <pre class="sap-alert-item-error">${err}</pre>
          <button type="button" class="btn-sap-error btn-sap-error--block" data-po="${po}">Full error</button>
        </article>`;
    })
    .join("");
}

function closeSapAlertPanel() {
  if (!sapAlertPanel || !sapAlertBell) return;
  sapAlertPanel.classList.add("hidden");
  sapAlertBell.setAttribute("aria-expanded", "false");
}

function openSapAlertPanel() {
  if (!sapAlertPanel || !sapAlertBell) return;
  sapAlertPanel.classList.remove("hidden");
  sapAlertBell.setAttribute("aria-expanded", "true");
}

async function loadSapFailures() {
  if (!sapAlertPanelBody) return { total: 0, items: [] };
  try {
    const data = await apiGet("/v1/walmart/sap-failures?limit=50");
    updateSapAlertBell(data.total ?? 0);
    if (sapAlertPanelCount) sapAlertPanelCount.textContent = String(data.total ?? 0);
    sapAlertPanelBody.innerHTML = renderSapFailurePanelItems(data.items || []);
    sapAlertPanelBody.querySelectorAll(".btn-sap-error").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        openSapErrorModal(btn.getAttribute("data-po"));
      });
    });
    return data;
  } catch (err) {
    sapAlertPanelBody.innerHTML = `<p class="sap-alert-empty sap-alert-empty--error">${escapeHtml(err.message)}</p>`;
    return { total: 0, items: [] };
  }
}

async function refreshSapAlert() {
  const data = await loadSapFailures();
  if (lastDashboardStats) {
    lastDashboardStats.sap_sync = {
      ...(lastDashboardStats.sap_sync || {}),
      failed: data.total ?? 0,
    };
    setSapAlertHint(lastDashboardStats);
  }
  return data;
}

function setSapAlertHint(stats) {
  if (!sapAlertHint) return;
  const failed = stats?.sap_sync?.failed ?? 0;
  const posted = stats?.sap_sync?.posted ?? 0;
  if (sapSyncFilter?.value === "failed") {
    sapAlertHint.textContent = `Showing orders with SAP sync failed (${failed} total in database).`;
    sapAlertHint.classList.remove("hidden");
    return;
  }
  if (failed > 0) {
    sapAlertHint.textContent = `${failed} SAP sync failed · ${posted} posted — use the bell or SAP sync filter.`;
    sapAlertHint.classList.remove("hidden");
    return;
  }
  sapAlertHint.classList.add("hidden");
  sapAlertHint.textContent = "";
}

function setLastSyncMeta(stats) {
  lastDashboardStats = stats;
  const sync = stats?.last_sync;
  updateSapAlertBell(stats);
  setSapAlertHint(stats);
  renderStatChips(stats);
  if (!sync) {
    lastSyncMeta.textContent = "No sync runs recorded yet.";
    return;
  }
  const finished = sync.sync_finished_at
    ? new Date(sync.sync_finished_at).toLocaleString()
    : "—";
  const sapFailed = stats?.sap_sync?.failed ?? 0;
  const sapPart = sapFailed > 0 ? ` · SAP failed: ${sapFailed}` : "";
  lastSyncMeta.textContent = `Total in database: ${stats.total_orders ?? 0} orders (${stats.total_lines ?? 0} lines) · Last sync #${sync.id} (${sync.status}) at ${finished}${sapPart}`;
}

function filteredTotal(stats, status) {
  if (!status) return stats.total_orders ?? stats.total_lines ?? 0;
  const row = (stats.by_status || []).find((r) => r.status === status);
  return row?.count ?? 0;
}

async function loadOrders(resetPage = false) {
  if (resetPage) currentOffset = 0;
  hideError();
  ordersBody.innerHTML =
    `<tr><td colspan="${COLSPAN}" class="loading-cell">Loading orders…</td></tr>`;

  const limit = pageSize();
  updateSortIndicators();

  let stats = null;
  try {
    stats = await apiGet("/v1/walmart/stats");
    populateStatusFilter(stats.by_status);
    setLastSyncMeta(stats);
  } catch (err) {
    showError(`Stats failed: ${err.message}`);
    ordersBody.innerHTML =
      `<tr><td colspan="${COLSPAN}" class="loading-cell">Failed to load stats.</td></tr>`;
    return;
  }
  refreshSapAlert().catch(() => {});

  try {
    const orders = await apiGet(
      `/v1/walmart/orders?${buildOrdersQueryParams(limit, currentOffset)}`
    );
    if (orders.sort_by) sortBy = orders.sort_by;
    if (orders.sort_dir) sortDir = orders.sort_dir;
    updateSortIndicators();
    const status = statusFilter.value;
    const total =
      orders.total ?? filteredTotal(stats, status) ?? orders.items?.length ?? 0;
    const groupedItems = normalizeOrderItems(orders.items || []);
    renderOrders(groupedItems, orders.offset ?? currentOffset, total);
  } catch (err) {
    let hint = err.message;
    if (String(err.message).includes("has_shipping_charges")) {
      hint +=
        " — Run DB migration: sql/walmart_order_headers_add_shipping.sql (adds has_shipping_charges column).";
    }
    if (String(err.message).includes("has_county_surcharge_tax")) {
      hint +=
        " — Run DB migration: sql/walmart_order_headers_add_county_surcharge.sql (adds has_county_surcharge_tax column).";
    }
    if (String(err.message).includes("has_promotion_discount")) {
      hint +=
        " — Run DB migration: sql/walmart_order_headers_add_promotion_discount.sql (adds has_promotion_discount column).";
    }
    if (String(err.message).includes("ship_to_state")) {
      hint +=
        " — Run DB migration: sql/walmart_order_headers_add_ship_to_state.sql (adds ship_to_state column).";
    }
    if (String(err.message).includes("ship_date_pacific") || String(err.message).includes("ship_date_time_ms")) {
      hint +=
        " — Run: psql \"$DATABASE_URL\" -f sql/walmart_b2c_apply_migrations.sql";
    }
    showError(`Orders failed: ${hint}`);
    ordersBody.innerHTML =
      `<tr><td colspan="${COLSPAN}" class="loading-cell">Failed to load orders. See error above.</td></tr>`;
  }
}

async function loadSapItemsOnly() {
  hideError();
  sapItemsBody.innerHTML =
    '<tr><td colspan="4" class="loading-cell">Loading SAP items…</td></tr>';
  sapItemsMeta.textContent = "Loading from SAP HANA…";
  try {
    const sapData = await apiGet("/v1/walmart/sap-items");
    renderSapItems(sapData.items || [], sapData.count, sapData.schema);
  } catch (err) {
    showError(`SAP items failed: ${err.message}`);
    sapItemsBody.innerHTML =
      '<tr><td colspan="4" class="loading-cell">Failed to load SAP items.</td></tr>';
    sapItemsMeta.textContent = "SAP HANA query failed";
  }
}

async function loadSyncLog() {
  hideError();
  syncBody.innerHTML =
    '<tr><td colspan="6" class="loading-cell">Loading sync history…</td></tr>';
  sapItemsBody.innerHTML =
    '<tr><td colspan="4" class="loading-cell">Loading SAP items…</td></tr>';
  sapItemsMeta.textContent = "Loading from SAP…";

  try {
    const syncData = await apiGet("/v1/walmart/sync-log?limit=25");
    renderSyncLog(syncData.items || []);
  } catch (err) {
    showError(`Sync history failed: ${err.message}`);
    syncBody.innerHTML =
      '<tr><td colspan="6" class="loading-cell">Failed to load sync history.</td></tr>';
  }

  await loadSapItemsOnly();
}

function showOrdersPanel() {
  document.querySelectorAll(".nav-item").forEach((el) => el.classList.remove("active"));
  document.querySelector('.nav-item[href="#"]').classList.add("active");
  ordersPanel.classList.remove("hidden");
  syncPanel.classList.add("hidden");
  setOrdersChromeVisible(true);
  if (pageEyebrow) pageEyebrow.textContent = "Orders report";
  const countEl = document.getElementById("record-count");
  setPageTitleRecords(countEl?.textContent || "—");
}

function showSyncPanel(scrollToSap = false) {
  document.querySelectorAll(".nav-item").forEach((el) => el.classList.remove("active"));
  document.getElementById("nav-sync").classList.add("active");
  ordersPanel.classList.add("hidden");
  syncPanel.classList.remove("hidden");
  setOrdersChromeVisible(false);
  if (pageEyebrow) pageEyebrow.textContent = scrollToSap ? "SAP catalog" : "Sync history";
  pageTitle.textContent = scrollToSap ? "SAP FG items" : "Sync history";
  loadSyncLog()
    .then(() => {
      if (scrollToSap) {
        document.getElementById("sap-items-section")?.scrollIntoView({ behavior: "smooth" });
      }
    })
    .catch((e) => showError(e.message));
}

function showSapPanel() {
  document.querySelectorAll(".nav-item").forEach((el) => el.classList.remove("active"));
  document.getElementById("nav-sap").classList.add("active");
  ordersPanel.classList.add("hidden");
  syncPanel.classList.remove("hidden");
  setOrdersChromeVisible(false);
  if (pageEyebrow) pageEyebrow.textContent = "SAP catalog";
  pageTitle.textContent = "SAP FG items (HANA)";
  syncBody.innerHTML =
    '<tr><td colspan="6" class="loading-cell text-muted">Sync history is on the menu item above.</td></tr>';
  loadSapItemsOnly();
}

document.getElementById("refresh-btn").addEventListener("click", () => {
  if (!ordersPanel.classList.contains("hidden")) {
    loadOrders(false).catch((e) => showError(e.message));
    refreshSapAlert().catch(() => {});
  } else if (document.getElementById("nav-sap").classList.contains("active")) {
    loadSapItemsOnly();
  } else {
    loadSyncLog().catch((e) => showError(e.message));
  }
});

let poFilterTimer = null;

function scheduleFilterSearch() {
  clearTimeout(poFilterTimer);
  poFilterTimer = setTimeout(() => {
    loadOrders(true).catch((e) => showError(e.message));
  }, 400);
}

function bindTextFilter(el) {
  if (!el) return;
  el.addEventListener("input", scheduleFilterSearch);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      clearTimeout(poFilterTimer);
      loadOrders(true).catch((err) => showError(err.message));
    }
  });
}

statusFilter.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

orderDateFrom.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

orderDateTo.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

statusDateFrom.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

statusDateTo.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

updatedFrom.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

updatedTo.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

shippingFilter.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

if (countySurchargeFilter) {
  countySurchargeFilter.addEventListener("change", () => {
    loadOrders(true).catch((e) => showError(e.message));
  });
}

if (promotionDiscountFilter) {
  promotionDiscountFilter.addEventListener("change", () => {
    loadOrders(true).catch((e) => showError(e.message));
  });
}

if (lineCountFilter) {
  lineCountFilter.addEventListener("change", () => {
    loadOrders(true).catch((e) => showError(e.message));
  });
}

if (sapSyncFilter) {
  sapSyncFilter.addEventListener("change", () => {
    if (lastDashboardStats) updateSapAlertBell(lastDashboardStats);
    loadOrders(true).catch((e) => showError(e.message));
  });
}

bindTextFilter(poFilter);
bindTextFilter(orderFilter);
bindTextFilter(skuFilter);
bindTextFilter(itemFilter);
bindTextFilter(qtyFilter);

document.querySelectorAll(".sortable-th .sortable-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const th = btn.closest(".sortable-th");
    const key = th?.getAttribute("data-sort-key");
    if (key) toggleSort(key);
  });
});

clearFiltersBtn.addEventListener("click", () => {
  poFilter.value = "";
  orderFilter.value = "";
  statusFilter.value = "";
  skuFilter.value = "";
  itemFilter.value = "";
  qtyFilter.value = "";
  orderDateFrom.value = "";
  orderDateTo.value = "";
  statusDateFrom.value = "";
  statusDateTo.value = "";
  updatedFrom.value = "";
  updatedTo.value = "";
  shippingFilter.value = "";
  if (countySurchargeFilter) countySurchargeFilter.value = "";
  if (promotionDiscountFilter) promotionDiscountFilter.value = "";
  if (lineCountFilter) lineCountFilter.value = "";
  if (sapSyncFilter) sapSyncFilter.value = "";
  sortBy = "order_date";
  sortDir = "desc";
  updateSortIndicators();
  loadOrders(true).catch((e) => showError(e.message));
});

if (sapAlertBell) {
  sapAlertBell.addEventListener("click", async (e) => {
    e.stopPropagation();
    const isOpen = sapAlertPanel && !sapAlertPanel.classList.contains("hidden");
    if (isOpen) {
      closeSapAlertPanel();
      return;
    }
    openSapAlertPanel();
    if (sapAlertPanelBody) sapAlertPanelBody.innerHTML = '<p class="muted">Loading…</p>';
    await loadSapFailures();
  });
}

if (sapAlertViewAll) {
  sapAlertViewAll.addEventListener("click", () => {
    if (sapSyncFilter) sapSyncFilter.value = "failed";
    closeSapAlertPanel();
    loadOrders(true).catch((e) => showError(e.message));
  });
}

if (sapRetryBtn) {
  sapRetryBtn.addEventListener("click", () => {
    runSapRetryBatch(sapRetryBtn).catch((e) => showError(e.message));
  });
}

document.addEventListener("click", (e) => {
  if (!sapAlertWrap?.contains(e.target)) {
    closeSapAlertPanel();
  }
});

pageSizeSelect.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

prevPageBtn.addEventListener("click", () => {
  currentOffset = Math.max(0, currentOffset - pageSize());
  loadOrders(false).catch((e) => showError(e.message));
});

nextPageBtn.addEventListener("click", () => {
  if (currentOffset + pageSize() < pageTotal) {
    currentOffset += pageSize();
    loadOrders(false).catch((e) => showError(e.message));
  }
});

document.getElementById("nav-sync").addEventListener("click", (e) => {
  e.preventDefault();
  showSyncPanel(false);
});

document.getElementById("nav-sap").addEventListener("click", (e) => {
  e.preventDefault();
  showSapPanel();
});

document.querySelector('.nav-item[href="#"]').addEventListener("click", (e) => {
  e.preventDefault();
  showOrdersPanel();
});

updateSortIndicators();

if (loginForm) {
  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (loginError) {
      loginError.textContent = "";
      loginError.classList.add("hidden");
    }
    loginSubmit.disabled = true;
    const originalLabel = loginSubmit.textContent;
    loginSubmit.textContent = "Signing in…";
    try {
      await loginWithPassword(loginEmail.value.trim(), loginPassword.value);
      loginPassword.value = "";
      showAppShell();
      await loadOrders(true);
      hideError();
    } catch (err) {
      if (loginError) {
        loginError.textContent = err.message || "Sign in failed";
        loginError.classList.remove("hidden");
      }
    } finally {
      loginSubmit.disabled = false;
      loginSubmit.textContent = originalLabel;
    }
  });
}

if (logoutBtn) {
  logoutBtn.addEventListener("click", () => {
    clearAuthSession();
    showLoginScreen();
  });
}

bootstrapApp().catch((e) => {
  showLoginScreen(e.message);
});
