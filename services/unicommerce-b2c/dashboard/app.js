/** Unicommerce B2C dashboard — prod stack, ap-south-1.
 * Auth: Cognito User Pool JWT authorizer on the HTTP API directly (no
 * backend login proxy) — this file calls Cognito's IdP endpoint straight,
 * same pattern as the amazon-us-b2c dashboard this was cloned from.
 */
const API_BASE = "https://coqi1t0nvb.execute-api.ap-south-1.amazonaws.com";
const COGNITO_REGION = "ap-south-1";
const COGNITO_CLIENT_ID = "5bvg6pblh922ldd5n065vvpf84";
const COGNITO_IDP_URL = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`;

const AUTH_STORAGE_KEY = "unicommerce_b2c_dashboard_auth";

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

async function cognitoRequest(target, body) {
  const res = await fetch(COGNITO_IDP_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = data?.message || data?.__type || `${res.status} Cognito error`;
    throw new Error(message);
  }
  return data;
}

async function loginWithPassword(email, password) {
  const data = await cognitoRequest("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: COGNITO_CLIENT_ID,
    AuthParameters: { USERNAME: email, PASSWORD: password },
  });
  if (data.ChallengeName) {
    throw new Error(`Login requires extra step: ${data.ChallengeName} (contact admin)`);
  }
  const result = data.AuthenticationResult;
  if (!result?.IdToken) throw new Error("Cognito did not return a token");
  saveAuthSession({
    id_token: result.IdToken,
    access_token: result.AccessToken,
    refresh_token: result.RefreshToken,
  });
}

async function refreshAuthSession() {
  if (!authSession?.refresh_token) return false;
  try {
    const data = await cognitoRequest("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: COGNITO_CLIENT_ID,
      AuthParameters: { REFRESH_TOKEN: authSession.refresh_token },
    });
    const result = data.AuthenticationResult;
    if (!result?.IdToken) return false;
    saveAuthSession({
      id_token: result.IdToken,
      access_token: result.AccessToken,
      refresh_token: authSession.refresh_token,
    });
    return true;
  } catch {
    return false;
  }
}

async function ensureAuthenticated() {
  authSession = loadAuthSession();
  if (isSessionValid(authSession)) return true;
  if (authSession?.refresh_token && (await refreshAuthSession())) return true;
  clearAuthSession();
  return false;
}

async function handleUnauthorized() {
  if (authSession?.refresh_token && (await refreshAuthSession())) return true;
  clearAuthSession();
  showLoginScreen("Session expired — sign in again.");
  return false;
}

function isFileProtocol() {
  return window.location.protocol === "file:";
}

function fileProtocolHelp() {
  return (
    "Chrome blocks API calls when you open index.html as a file (file://). " +
    "In Terminal run: cd services/unicommerce-b2c/dashboard && ./serve.sh " +
    "Then open http://localhost:8090 in the browser."
  );
}

function showError(message) {
  const el = document.getElementById("error-banner");
  el.textContent = message;
  el.classList.remove("hidden");
}

function hideError() {
  const el = document.getElementById("error-banner");
  el.classList.add("hidden");
  el.textContent = "";
}

async function apiGet(path) {
  try {
    const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() });
    if (res.status === 401) {
      if (await handleUnauthorized()) return apiGet(path);
      throw new Error("Unauthorized — sign in again");
    }
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  } catch (err) {
    if (isFileProtocol() && err instanceof TypeError) throw new Error(fileProtocolHelp());
    throw err;
  }
}

async function apiPost(path) {
  try {
    const res = await fetch(`${API_BASE}${path}`, { method: "POST", headers: authHeaders() });
    if (res.status === 401) {
      if (await handleUnauthorized()) return apiPost(path);
      throw new Error("Unauthorized — sign in again");
    }
    const body = await res.text();
    const data = body ? JSON.parse(body) : null;
    if (!res.ok) throw new Error(data?.error || `${res.status} ${body}`);
    return data;
  } catch (err) {
    if (isFileProtocol() && err instanceof TypeError) throw new Error(fileProtocolHelp());
    throw err;
  }
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function truncate(text, max = 60) {
  const s = String(text || "");
  return s.length <= max ? s : `${s.slice(0, max)}…`;
}

function formatDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return escapeHtml(String(value));
  return d.toLocaleString("en-GB", {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function formatDateOnly(value) {
  if (!value) return "—";
  return String(value).slice(0, 10);
}

function formatMoney(amount) {
  if (amount == null || Number.isNaN(Number(amount))) return "—";
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(Number(amount));
  } catch {
    return `₹${Number(amount).toFixed(2)}`;
  }
}

// Pipeline-status values confirmed live in the codebase (post_invoices.py,
// credit_notes.py, store.py): fetched, ready, posting, verify, batch_hold,
// posted, failed, dead. Same map used for both orders (pipeline_status) and
// credit notes (status) -- the vocabularies overlap.
const STAGE_BADGE = {
  fetched: "badge-created",
  ready: "badge-acknowledged",
  posting: "badge-running",
  processing: "badge-running",
  verify: "badge-running",
  po_created: "badge-running",
  grn_done: "badge-running",
  batch_hold: "badge-created",
  posted: "badge-success",
  failed: "badge-failed",
  dead: "badge-failed",
};

function stageBadge(stage) {
  const cls = STAGE_BADGE[stage] || "badge-default";
  return `<span class="badge ${cls}">${escapeHtml(stage || "—")}</span>`;
}

// ── Panel switching ─────────────────────────────────────────────────────

const PANELS = ["orders-panel", "cn-panel", "sync-panel", "recon-panel"];
const PANEL_TITLES = {
  "orders-panel": "Orders",
  "cn-panel": "Returns / credit notes",
  "sync-panel": "Sync history",
  "recon-panel": "Recon / mismatches",
};
let activePanel = "orders-panel";

function setActivePanel(panelId) {
  activePanel = panelId;
  PANELS.forEach((id) => document.getElementById(id).classList.toggle("hidden", id !== panelId));
  document.querySelectorAll(".nav-item").forEach((a) => a.classList.toggle("active", a.dataset.panel === panelId));
  document.getElementById("orders-filter-card").classList.toggle("hidden", panelId !== "orders-panel");
  document.getElementById("page-eyebrow").textContent = PANEL_TITLES[panelId];
  document.getElementById("page-title").classList.toggle("hidden", panelId !== "orders-panel");
  renderPanelActions(panelId);
  loadPanel(panelId).catch((e) => showError(e.message));
}

function renderPanelActions(panelId) {
  const el = document.getElementById("panel-actions");
  el.innerHTML = "";
  if (panelId === "orders-panel") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-sap-retry";
    btn.textContent = "Retry failed invoices";
    btn.addEventListener("click", () => runRetryInvoices(btn));
    el.appendChild(btn);
  } else if (panelId === "cn-panel") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-sap-retry";
    btn.textContent = "Retry failed credit notes";
    btn.addEventListener("click", () => runRetryCn(btn));
    el.appendChild(btn);
  }
}

async function loadPanel(panelId) {
  if (panelId === "orders-panel") return loadOrders(true);
  if (panelId === "cn-panel") return loadReturns();
  if (panelId === "sync-panel") return loadSyncLog();
  if (panelId === "recon-panel") return loadReconGaps();
}

document.querySelectorAll(".nav-item[data-panel]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    setActivePanel(a.dataset.panel);
  });
});

// ── Stats ────────────────────────────────────────────────────────────────

async function loadStats() {
  const stats = await apiGet("/v1/unicommerce/stats");
  const chips = document.getElementById("stat-chips");
  const byStatus = stats.by_pipeline_status || {};
  const posted = byStatus.posted || 0;
  const ready = byStatus.ready || 0;
  const fetched = byStatus.fetched || 0;
  const batchHold = byStatus.batch_hold || 0;
  const failed = (byStatus.failed || 0) + (byStatus.dead || 0);
  const cnPosted = stats.cn_by_status?.posted || 0;
  const cnFailed = (stats.cn_by_status?.failed || 0) + (stats.cn_by_status?.dead || 0);
  chips.innerHTML = `
    <span class="stat-chip">${escapeHtml(String(stats.total_orders ?? 0))} orders</span>
    <span class="stat-chip stat-chip--success">${escapeHtml(String(posted))} posted</span>
    <span class="stat-chip">${escapeHtml(String(ready))} ready</span>
    <span class="stat-chip">${escapeHtml(String(fetched))} fetched</span>
    <span class="stat-chip ${batchHold > 0 ? "stat-chip--muted" : "stat-chip--muted"}">${escapeHtml(String(batchHold))} batch hold</span>
    <span class="stat-chip ${failed > 0 ? "stat-chip--danger" : "stat-chip--muted"}">${escapeHtml(String(failed))} failed</span>
    <span class="stat-chip stat-chip--success">${escapeHtml(String(cnPosted))} CN posted</span>
    <span class="stat-chip ${cnFailed > 0 ? "stat-chip--danger" : "stat-chip--muted"}">${escapeHtml(String(cnFailed))} CN failed</span>
  `;
}

// ── Orders panel ─────────────────────────────────────────────────────────

let currentOffset = 0;
let sortBy = "updated_at";
let sortDir = "desc";

const orderFilter = document.getElementById("order-filter");
const stageFilter = document.getElementById("stage-filter");
const orderDateFrom = document.getElementById("order-date-from");
const orderDateTo = document.getElementById("order-date-to");
const pageSizeSelect = document.getElementById("page-size");
const ordersBody = document.getElementById("orders-body");
const paginationInfo = document.getElementById("pagination-info");
const prevPageBtn = document.getElementById("prev-page");
const nextPageBtn = document.getElementById("next-page");

function pageSize() {
  return parseInt(pageSizeSelect.value, 10) || 100;
}

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
  add("uniware_id", orderFilter.value);
  add("pipeline_status", stageFilter.value);
  add("order_date_from", orderDateFrom.value);
  add("order_date_to", orderDateTo.value);
  return params.toString();
}

function updateSortIndicators() {
  document.querySelectorAll(".sortable-th").forEach((th) => {
    const key = th.getAttribute("data-sort-key");
    const indicator = th.querySelector(".sort-indicator");
    const active = key === sortBy;
    th.classList.toggle("sortable-th--active", active);
    if (indicator) indicator.textContent = active ? (sortDir === "asc" ? "▲" : "▼") : "";
  });
}

document.querySelectorAll(".sortable-th").forEach((th) => {
  th.querySelector(".sortable-btn")?.addEventListener("click", () => {
    const key = th.getAttribute("data-sort-key");
    if (sortBy === key) {
      sortDir = sortDir === "asc" ? "desc" : "asc";
    } else {
      sortBy = key;
      sortDir = "desc";
    }
    updateSortIndicators();
    loadOrders(true).catch((e) => showError(e.message));
  });
});

function setRecordCount(shown, total) {
  document.getElementById("record-count").textContent = `${shown} of ${total}`;
}

function updatePagination(offset, total, itemCount) {
  const start = total === 0 ? 0 : offset + 1;
  const end = offset + itemCount;
  paginationInfo.textContent = `Showing ${start}–${end} of ${total}`;
  prevPageBtn.disabled = offset <= 0;
  nextPageBtn.disabled = end >= total;
}

async function loadOrders(reset) {
  if (reset) currentOffset = 0;
  hideError();
  const query = buildOrdersQueryParams(pageSize(), currentOffset);
  const page = await apiGet(`/v1/unicommerce/orders?${query}`);
  renderOrders(page.items || [], currentOffset, page.total || 0);
}

function renderOrders(items, offset, total) {
  if (!items.length) {
    ordersBody.innerHTML = `<tr><td colspan="11" class="loading-cell">No orders found.</td></tr>`;
    setRecordCount(0, total);
    updatePagination(offset, total, 0);
    return;
  }
  setRecordCount(items.length, total);
  updatePagination(offset, total, items.length);

  ordersBody.innerHTML = items
    .map(
      (row, i) => `
    <tr class="data-row" data-oid="${escapeHtml(row.uniware_id)}">
      <td class="cell-center cell-muted">${offset + i + 1}</td>
      <td class="cell-mono">${escapeHtml(row.uniware_id)}</td>
      <td>${escapeHtml(row.status || "—")}</td>
      <td>${stageBadge(row.pipeline_status)}</td>
      <td>${formatDateOnly(row.order_date)}</td>
      <td>${escapeHtml(row.channel_name || "—")}</td>
      <td>${escapeHtml(row.city || "—")}</td>
      <td class="cell-mono">${formatMoney(row.uc_total)}</td>
      <td class="cell-mono">${row.sap_ar_invoice_docentry != null ? escapeHtml(String(row.sap_ar_invoice_docentry)) : "—"}</td>
      <td>${formatDate(row.updated_at)}</td>
      <td class="cell-center"><button type="button" class="btn-view" data-oid="${escapeHtml(row.uniware_id)}">View</button></td>
    </tr>`
    )
    .join("");

  ordersBody.querySelectorAll(".btn-view").forEach((btn) => {
    btn.addEventListener("click", () => openOrderDetail(btn.dataset.oid));
  });
}

document.getElementById("refresh-btn").addEventListener("click", () => {
  loadStats().catch(() => {});
  loadOrders(true).catch((e) => showError(e.message));
});
document.getElementById("clear-filters-btn").addEventListener("click", () => {
  orderFilter.value = "";
  stageFilter.value = "";
  orderDateFrom.value = "";
  orderDateTo.value = "";
  loadOrders(true).catch((e) => showError(e.message));
});
[orderFilter, stageFilter, orderDateFrom, orderDateTo, pageSizeSelect].forEach((el) => {
  el.addEventListener("change", () => loadOrders(true).catch((e) => showError(e.message)));
});
orderFilter.addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadOrders(true).catch((err) => showError(err.message));
});
prevPageBtn.addEventListener("click", () => {
  currentOffset = Math.max(0, currentOffset - pageSize());
  loadOrders(false).catch((e) => showError(e.message));
});
nextPageBtn.addEventListener("click", () => {
  currentOffset += pageSize();
  loadOrders(false).catch((e) => showError(e.message));
});

async function runRetryInvoices(btn) {
  if (!window.confirm("Retry all ready/failed invoices (up to 50)? This posts real SAP invoices for any orders it finds.")) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Retrying…";
  try {
    await apiPost("/v1/unicommerce/retry-invoices?limit=50");
    hideError();
    window.alert("Retry started -- check the Sync history panel below (mode: retry_invoices) for progress and results.");
    await Promise.all([loadStats(), loadOrders(false)]);
  } catch (err) {
    showError(`Retry failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// ── Order detail modal ───────────────────────────────────────────────────

const orderDetailModal = document.getElementById("order-detail-modal");
const orderDetailBody = document.getElementById("order-detail-body");
const orderDetailTitle = document.getElementById("order-detail-title");

function closeOrderDetail() {
  orderDetailModal.classList.add("hidden");
  orderDetailModal.setAttribute("aria-hidden", "true");
}
document.getElementById("order-detail-backdrop").addEventListener("click", closeOrderDetail);
document.getElementById("order-detail-close").addEventListener("click", closeOrderDetail);

async function openOrderDetail(uniwareId) {
  orderDetailTitle.textContent = `Order ${uniwareId}`;
  orderDetailBody.innerHTML = `<p class="muted">Loading…</p>`;
  orderDetailModal.classList.remove("hidden");
  orderDetailModal.setAttribute("aria-hidden", "false");
  try {
    const detail = await apiGet(`/v1/unicommerce/order-detail?uniware_id=${encodeURIComponent(uniwareId)}`);
    orderDetailBody.innerHTML = renderOrderDetail(detail);
  } catch (err) {
    orderDetailBody.innerHTML = `<p class="trace-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderOrderDetail(detail) {
  const h = detail.header || {};
  const lines = detail.lines || [];
  const creditNotes = detail.credit_notes || [];

  const linesRows = lines
    .map(
      (l) => `
      <tr>
        <td class="cell-mono">${escapeHtml(l.sellerskucode || l.itemsku)}</td>
        <td class="cell-center">${escapeHtml(String(l.quantity))}</td>
        <td class="cell-mono">${formatMoney(l.selling_price)}</td>
        <td class="cell-mono">${formatMoney(l.total_price)}</td>
        <td class="cell-mono">${escapeHtml(l.batchcode || "—")}</td>
        <td>${escapeHtml(l.statuscode || "—")}</td>
      </tr>`
    )
    .join("");

  const cnRows = creditNotes
    .map(
      (r) => `
      <tr>
        <td class="cell-mono">${escapeHtml(r.uc_return_id)}</td>
        <td>${escapeHtml(r.return_type || "—")}</td>
        <td>${stageBadge(r.status)}</td>
        <td class="cell-mono">${r.sap_cn_docentry ?? "—"}</td>
        <td>${escapeHtml(truncate(r.last_error, 60) || "—")}</td>
      </tr>`
    )
    .join("");

  return `
    <div class="trace-cards">
      <div class="trace-card"><span class="trace-card-label">Stage</span><span class="trace-card-value">${stageBadge(h.pipeline_status)}</span></div>
      <div class="trace-card"><span class="trace-card-label">Status</span><span class="trace-card-value">${escapeHtml(h.status || "—")}</span></div>
      <div class="trace-card"><span class="trace-card-label">Channel</span><span class="trace-card-value">${escapeHtml(h.channel_name || "—")}</span></div>
      <div class="trace-card"><span class="trace-card-label">Order date</span><span class="trace-card-value">${formatDateOnly(h.order_date)}</span></div>
      <div class="trace-card"><span class="trace-card-label">City / State</span><span class="trace-card-value">${escapeHtml(h.city || "—")}, ${escapeHtml(h.state || "—")}</span></div>
      <div class="trace-card"><span class="trace-card-label">Total</span><span class="trace-card-value">${formatMoney(h.uc_total)}</span></div>
      <div class="trace-card"><span class="trace-card-label">SAP Invoice DocEntry</span><span class="trace-card-value">${h.sap_ar_invoice_docentry ?? "—"}</span></div>
      <div class="trace-card"><span class="trace-card-label">SAP Invoice DocNum</span><span class="trace-card-value">${h.sap_ar_invoice_docnum ?? "—"}</span></div>
      <div class="trace-card"><span class="trace-card-label">Attempts</span><span class="trace-card-value">${h.attempt_count ?? 0}</span></div>
    </div>
    ${h.last_error ? `<p class="sap-error-inline">${escapeHtml(h.last_error)}</p>` : ""}

    <div class="order-detail-box" style="margin-top:1rem">
      <div class="order-detail-box-title">Reconciliation</div>
      <table class="trace-table">
        <thead><tr><th></th><th>Expected</th><th>Actual</th><th>Diff</th></tr></thead>
        <tbody>
          <tr><td>Tax</td><td class="cell-mono">${formatMoney(h.ar_invoice_tax_expected)}</td><td class="cell-mono">${formatMoney(h.ar_invoice_tax_actual)}</td><td class="cell-mono">${formatMoney(h.ar_invoice_tax_diff)}</td></tr>
          <tr><td>Unit price</td><td class="cell-mono">${formatMoney(h.ar_invoice_unit_price_expected)}</td><td class="cell-mono">${formatMoney(h.ar_invoice_unit_price_actual)}</td><td class="cell-mono">${formatMoney(h.ar_invoice_unit_price_diff)}</td></tr>
        </tbody>
      </table>
    </div>

    <div class="order-detail-box" style="margin-top:1rem">
      <div class="order-detail-box-title">Lines</div>
      <table class="trace-table">
        <thead><tr><th>SKU</th><th>Qty</th><th>Unit price</th><th>Total</th><th>Batch</th><th>Status</th></tr></thead>
        <tbody>${linesRows || `<tr><td colspan="6" class="muted">No lines.</td></tr>`}</tbody>
      </table>
    </div>

    <div class="order-detail-box" style="margin-top:1rem">
      <div class="order-detail-box-title">Credit notes</div>
      <table class="trace-table">
        <thead><tr><th>Return ID</th><th>Type</th><th>Status</th><th>SAP DocEntry</th><th>Error</th></tr></thead>
        <tbody>${cnRows || `<tr><td colspan="5" class="muted">No returns for this order.</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

// ── Returns / Credit notes panel ─────────────────────────────────────────

async function loadReturns() {
  hideError();
  const status = document.getElementById("cn-status-filter")?.value || "";
  const query = status ? `?status=${encodeURIComponent(status)}&limit=100` : "?limit=100";
  const data = await apiGet(`/v1/unicommerce/returns${query}`);
  const body = document.getElementById("cn-body");
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="9" class="loading-cell">No returns/credit-note activity yet.</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (r, i) => `
    <tr>
      <td class="cell-center cell-muted">${i + 1}</td>
      <td class="cell-mono">${escapeHtml(r.uc_return_id)}</td>
      <td class="cell-mono">${escapeHtml(r.uc_order_id)}</td>
      <td>${escapeHtml(r.return_type || "—")}</td>
      <td>${formatDateOnly(r.return_date)}</td>
      <td>${stageBadge(r.status)}</td>
      <td class="cell-mono">${r.sap_cn_docentry ?? "—"}</td>
      <td>${escapeHtml(truncate(r.last_error, 70) || "—")}</td>
      <td>${formatDate(r.updated_at)}</td>
    </tr>`
    )
    .join("");
}

async function runRetryCn(btn) {
  if (!window.confirm("Retry all failed credit notes (up to 50)? This posts real SAP credit notes for any returns it finds.")) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Retrying…";
  try {
    await apiPost("/v1/unicommerce/retry-cn?limit=50");
    hideError();
    window.alert("Retry started -- check the Sync history panel below (mode: retry_cn) for progress and results.");
    await Promise.all([loadStats(), loadReturns()]);
  } catch (err) {
    showError(`Retry failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// ── Sync history panel ───────────────────────────────────────────────────

async function loadSyncLog() {
  hideError();
  const mode = document.getElementById("sync-mode-filter")?.value || "";
  const query = mode ? `?mode=${encodeURIComponent(mode)}&limit=50` : "?limit=50";
  const data = await apiGet(`/v1/unicommerce/sync-log${query}`);
  const body = document.getElementById("sync-body");
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="8" class="loading-cell">No sync runs yet.</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (r, i) => `
    <tr>
      <td class="cell-center cell-muted">${i + 1}</td>
      <td>${escapeHtml(r.mode)}</td>
      <td>${r.status === "success" ? '<span class="badge badge-success">success</span>' : `<span class="badge badge-failed">${escapeHtml(r.status)}</span>`}</td>
      <td>${formatDate(r.started_at)}</td>
      <td>${formatDate(r.finished_at)}</td>
      <td class="cell-mono">${escapeHtml(truncate(r.window_from, 20) || "—")} → ${escapeHtml(truncate(r.window_to, 20) || "—")}</td>
      <td class="cell-mono">${escapeHtml(truncate(JSON.stringify(r.counts || {}), 60))}</td>
      <td>${escapeHtml(truncate(r.error_message, 60) || "—")}</td>
    </tr>`
    )
    .join("");
}

// ── Recon / mismatches panel ─────────────────────────────────────────────

async function loadReconGaps() {
  hideError();
  const minDiff = document.getElementById("recon-min-diff")?.value || "1";
  const data = await apiGet(`/v1/unicommerce/recon-gaps?min_diff=${encodeURIComponent(minDiff)}&limit=100`);
  const body = document.getElementById("recon-body");
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="8" class="loading-cell">No mismatches at or above this threshold.</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (r, i) => `
    <tr>
      <td class="cell-center cell-muted">${i + 1}</td>
      <td class="cell-mono">${escapeHtml(r.uniware_id)}</td>
      <td>${escapeHtml(r.channel_name || "—")}</td>
      <td class="cell-mono">${r.sap_ar_invoice_docentry ?? "—"}</td>
      <td class="cell-mono">${formatMoney(r.ar_invoice_tax_diff)}</td>
      <td class="cell-mono">${formatMoney(r.ar_invoice_unit_price_diff)}</td>
      <td>${escapeHtml(truncate(r.ar_invoice_mismatch_reason, 90) || "—")}</td>
      <td>${formatDate(r.updated_at)}</td>
    </tr>`
    )
    .join("");
}

document.getElementById("recon-refresh-btn")?.addEventListener("click", () => {
  loadReconGaps().catch((e) => showError(e.message));
});
document.getElementById("cn-status-filter")?.addEventListener("change", () => {
  loadReturns().catch((e) => showError(e.message));
});
document.getElementById("sync-mode-filter")?.addEventListener("change", () => {
  loadSyncLog().catch((e) => showError(e.message));
});

// ── Bootstrap ────────────────────────────────────────────────────────────

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
    await loadStats();
    await setActivePanel("orders-panel");
  } catch (e) {
    showError(e.message);
  }
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginSubmit.disabled = true;
  loginError.classList.add("hidden");
  try {
    await loginWithPassword(loginEmail.value.trim(), loginPassword.value);
    showAppShell();
    await loadStats();
    await setActivePanel("orders-panel");
  } catch (err) {
    loginError.textContent = err.message || "Sign-in failed.";
    loginError.classList.remove("hidden");
  } finally {
    loginSubmit.disabled = false;
  }
});

logoutBtn.addEventListener("click", () => {
  clearAuthSession();
  showLoginScreen();
});

bootstrapApp();
