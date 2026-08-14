/** Amazon US B2C dashboard — dev stack, ap-south-1.
 * Auth: Cognito User Pool JWT authorizer on the HTTP API directly (no
 * backend login proxy) — this file calls Cognito's IdP endpoint straight,
 * unlike the Walmart dashboard which proxies login through its own API.
 */
const API_BASE = "https://ejkzzvmudi.execute-api.ap-south-1.amazonaws.com";
const COGNITO_REGION = "ap-south-1";
const COGNITO_CLIENT_ID = "201ikfk1cte8vt5e94c4v22u49";
const COGNITO_IDP_URL = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`;

const AUTH_STORAGE_KEY = "amazon_us_b2c_dashboard_auth";

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
    "In Terminal run: cd services/amazon-us-b2c/dashboard && ./serve.sh " +
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
    return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(amount));
  } catch {
    return `$${Number(amount).toFixed(2)}`;
  }
}

const STAGE_BADGE = {
  fetched: "badge-created",
  ready: "badge-acknowledged",
  posting: "badge-running",
  posted: "badge-success",
  failed: "badge-failed",
};

function stageBadge(stage) {
  const cls = STAGE_BADGE[stage] || "badge-default";
  return `<span class="badge ${cls}">${escapeHtml(stage || "—")}</span>`;
}

function cnStatusBadge(status) {
  const map = { posting: "badge-running", posted: "badge-success", skipped: "badge-acknowledged", failed: "badge-failed", failed_permanent: "badge-failed" };
  const cls = map[status] || "badge-default";
  return `<span class="badge ${cls}">${escapeHtml(status || "—")}</span>`;
}

// ── Panel switching ─────────────────────────────────────────────────────

const PANELS = ["orders-panel", "failures-panel", "cn-panel", "sync-panel", "health-panel", "finance-panel"];
const PANEL_TITLES = {
  "orders-panel": "Orders",
  "failures-panel": "Invoice failures",
  "cn-panel": "Credit notes",
  "sync-panel": "Sync history",
  "health-panel": "Pipeline health",
  "finance-panel": "Finance reconciliation",
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
  if (panelId === "failures-panel") {
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
    btn.textContent = "Re-run CN posting (45d window)";
    btn.addEventListener("click", () => runRetryCn(btn));
    el.appendChild(btn);
  } else if (panelId === "health-panel") {
    const specs = [
      {
        label: "Sync orders now",
        build: ({ from, to }) => {
          const params = new URLSearchParams();
          if (from && to) {
            params.set("mode", "report");
            params.set("from_date", from);
            params.set("to_date", to);
          } else {
            params.set("mode", "incremental");
          }
          return {
            path: `/v1/amazon/sync-orders?${params}`,
            confirmMsg: from && to ? `Sync orders for ${from} → ${to} (report mode)?` : "Sync incremental orders now?",
          };
        },
      },
      {
        label: "Sync FBA ship dates",
        build: ({ from, to }) => {
          const params = new URLSearchParams();
          if (from) params.set("from_date", from);
          if (to) params.set("to_date", to);
          return {
            path: `/v1/amazon/sync-fba?${params}`,
            confirmMsg: from || to ? `Sync FBA ship dates for ${from || "…"} → ${to || "…"}?` : "Sync FBA ship dates now (last 21 days)?",
          };
        },
      },
      {
        label: "Post ready orders",
        build: ({ from, to }) => {
          const params = new URLSearchParams({ limit: "200" });
          if (from) params.set("from_date", from);
          if (to) params.set("to_date", to);
          return {
            path: `/v1/amazon/post-ready?${params}`,
            confirmMsg:
              from || to
                ? `Post up to 200 ready orders (ship date ${from || "cutoff"} → ${to || "no upper bound"})?`
                : "Post up to 200 ready orders to SAP now?",
          };
        },
      },
      {
        label: "Run invariants check",
        build: () => ({ path: "/v1/amazon/run-recon?mode=invariants", confirmMsg: "Run the daily invariants check now?" }),
      },
    ];
    specs.forEach(({ label, build }) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-outline btn-sm";
      btn.textContent = label;
      btn.addEventListener("click", () => {
        const { path, confirmMsg } = build(healthDateRange());
        runTrigger(btn, path, confirmMsg);
      });
      el.appendChild(btn);
    });
  }
}

function healthDateRange() {
  return {
    from: document.getElementById("health-date-from")?.value || "",
    to: document.getElementById("health-date-to")?.value || "",
  };
}

// ── Fire-and-forget triggers (sync/post/recon run async — results show up
// in Sync History, this just confirms the trigger was accepted) ──────────

async function runTrigger(btn, path, confirmMsg) {
  if (confirmMsg && !window.confirm(confirmMsg)) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Triggering…";
  try {
    await apiPost(path);
    hideError();
    window.alert("Triggered. Check Sync History in a minute or two for progress.");
  } catch (err) {
    showError(`Trigger failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function loadPanel(panelId) {
  if (panelId === "orders-panel") return loadOrders(true);
  if (panelId === "failures-panel") return loadFailures();
  if (panelId === "cn-panel") return loadCnLog();
  if (panelId === "sync-panel") return loadSyncLog();
  if (panelId === "health-panel") return loadHealth();
  if (panelId === "finance-panel") return loadFinanceRecon();
}

document.querySelectorAll(".nav-item[data-panel]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    setActivePanel(a.dataset.panel);
  });
});

// ── Stats ────────────────────────────────────────────────────────────────

let lastStats = null;

async function loadStats() {
  const stats = await apiGet("/v1/amazon/stats");
  lastStats = stats;
  const chips = document.getElementById("stat-chips");
  const byStage = stats.by_stage || {};
  const failed = byStage.failed || 0;
  const posted = byStage.posted || 0;
  const ready = byStage.ready || 0;
  const fetched = byStage.fetched || 0;
  const cnPosted = stats.cn_by_status?.posted || 0;
  const cnFailed = (stats.cn_by_status?.failed || 0) + (stats.cn_by_status?.failed_permanent || 0);
  chips.innerHTML = `
    <span class="stat-chip">${escapeHtml(String(stats.total_orders ?? 0))} orders</span>
    <span class="stat-chip stat-chip--success">${escapeHtml(String(posted))} posted</span>
    <span class="stat-chip">${escapeHtml(String(ready))} ready</span>
    <span class="stat-chip">${escapeHtml(String(fetched))} fetched</span>
    <span class="stat-chip ${failed > 0 ? "stat-chip--danger" : "stat-chip--muted"}">${escapeHtml(String(failed))} failed</span>
    <span class="stat-chip stat-chip--success">${escapeHtml(String(cnPosted))} CN posted</span>
    <span class="stat-chip ${cnFailed > 0 ? "stat-chip--danger" : "stat-chip--muted"}">${escapeHtml(String(cnFailed))} CN failed</span>
  `;
}

// ── Pipeline health panel ────────────────────────────────────────────────

const MODE_LABELS = {
  incremental: "Incremental fetch",
  report: "Report fetch",
  fba: "FBA ship-date sync",
  post: "Invoice posting",
  cn: "Credit-note posting",
  recon: "Daily invariants",
  mtr_recon: "Monthly MTR recon",
};

async function loadHealth() {
  hideError();
  await loadStats();
  renderHealth();
}

function runStatusBadge(status) {
  if (status === "success") return '<span class="badge badge-success">success</span>';
  if (status === "running") return '<span class="badge badge-running">running</span>';
  return `<span class="badge badge-failed">${escapeHtml(status || "unknown")}</span>`;
}

function renderHealth() {
  const body = document.getElementById("health-body");
  const runs = lastStats?.last_run_by_mode || {};
  const recon = runs.recon;
  const breaches = recon?.counts?.breaches || [];
  const watermarkAge = recon?.counts?.fetch_watermark_age_hours;

  const breachesHtml = breaches.length
    ? `<div class="sap-error-inline">${breaches.map((b) => escapeHtml(b)).join("<br>")}</div>`
    : `<p class="muted" style="margin:0 0 1rem">No invariant breaches on the last run${recon ? ` (${formatDate(recon.finished_at)})` : " — recon hasn't run yet"}.</p>`;

  const watermarkNote =
    watermarkAge == null
      ? ""
      : `<p class="muted" style="margin:0 0 1rem">Incremental fetch watermark age: <strong>${escapeHtml(String(watermarkAge))}h</strong>${watermarkAge > 6 ? " — stale, incremental fetch may be dead" : ""}</p>`;

  const modeOrder = ["incremental", "report", "fba", "post", "cn", "recon", "mtr_recon"];
  const cards = modeOrder
    .filter((m) => runs[m])
    .map((m) => {
      const r = runs[m];
      const countsPreview = truncate(JSON.stringify(r.counts || {}), 60);
      return `
      <div class="trace-card" style="overflow-wrap:anywhere">
        <span class="trace-card-label">${escapeHtml(MODE_LABELS[m] || m)}</span>
        <span class="trace-card-value">${runStatusBadge(r.status)}</span>
        <div class="sap-sync-meta" style="margin-top:0.4rem;word-break:break-word">Finished: ${formatDate(r.finished_at)}</div>
        <div class="sap-sync-meta" style="word-break:break-word">${escapeHtml(countsPreview)}</div>
        ${r.error_message ? `<div class="sap-sync-meta" style="color:var(--red);word-break:break-word">${escapeHtml(truncate(r.error_message, 60))}</div>` : ""}
      </div>`;
    })
    .join("");

  body.innerHTML = `
    <div class="order-detail-box-title" style="border-radius:10px 10px 0 0">Last invariant check</div>
    <div style="padding:1rem;border:1px solid var(--border);border-top:none;border-radius:0 0 10px 10px;margin-bottom:1.25rem">
      ${breachesHtml}
      ${watermarkNote}
    </div>
    <div class="order-detail-box-title" style="border-radius:10px 10px 0 0">Last run per mode</div>
    <div class="trace-cards" style="padding:1rem;border:1px solid var(--border);border-top:none;border-radius:0 0 10px 10px;margin:0">
      ${cards || '<p class="muted">No sync runs recorded yet.</p>'}
    </div>
  `;
}

// ── Finance reconciliation panel ─────────────────────────────────────────

// recon.py's own documented categorization: these three are expected/no-action
// (timing lag, not a real discrepancy); everything else needs Finance's attention.
const RECON_EXPECTED_CATEGORIES = new Set([
  "sap_not_in_mtr_timing",
  "refunded_after_mtr_extract",
  "pre_cutoff_excluded",
]);
const RECON_CATEGORY_ORDER = [
  "matched",
  "missing_in_sap",
  "amount_mismatch",
  "refund_not_yet_posted",
  "sap_not_in_mtr_timing",
  "refunded_after_mtr_extract",
  "pre_cutoff_excluded",
];

function reconCategoryChip(name, count) {
  if (!count) return "";
  let cls = "stat-chip--danger";
  if (name === "matched") cls = "stat-chip--success";
  else if (RECON_EXPECTED_CATEGORIES.has(name)) cls = "stat-chip--muted";
  return `<span class="stat-chip ${cls}">${escapeHtml(String(count))} ${escapeHtml(name.replace(/_/g, " "))}</span>`;
}

async function loadFinanceRecon() {
  hideError();
  const data = await apiGet("/v1/amazon/sync-log?mode=mtr_recon&limit=24");
  const body = document.getElementById("finance-body");
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="6" class="loading-cell">No reconciliation runs yet.</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map((r, i) => {
      const c = r.counts || {};
      const period = `${formatDateOnly(r.window_from)} → ${formatDateOnly(r.window_to)}`;
      const matchedText = c.total_orders != null ? `${c.matched ?? 0} / ${c.total_orders} (${c.matched_pct ?? 0}%)` : "—";
      const chips = RECON_CATEGORY_ORDER.map((cat) => reconCategoryChip(cat, c[cat])).join(" ");
      const downloadBtn = c.result_key
        ? `<button type="button" class="btn-outline btn-sm" data-key="${escapeHtml(c.result_key)}">Download CSV</button>`
        : "—";
      return `
      <tr>
        <td class="cell-center cell-muted">${i + 1}</td>
        <td class="cell-mono">${escapeHtml(period)}</td>
        <td>${r.status === "success" ? '<span class="badge badge-success">success</span>' : `<span class="badge badge-failed">${escapeHtml(r.status)}</span>`}</td>
        <td class="cell-mono">${escapeHtml(matchedText)}</td>
        <td><div class="stat-chips" style="margin-top:0">${chips || "—"}</div></td>
        <td class="cell-center">${downloadBtn}</td>
      </tr>`;
    })
    .join("");

  body.querySelectorAll("button[data-key]").forEach((btn) => {
    btn.addEventListener("click", () => downloadReconCsv(btn));
  });
}

async function downloadReconCsv(btn) {
  const key = btn.dataset.key;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Preparing…";
  try {
    const result = await apiGet(`/v1/amazon/recon-download?key=${encodeURIComponent(key)}`);
    window.open(result.url, "_blank");
  } catch (err) {
    showError(`Download failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

const reconPeriodFrom = document.getElementById("recon-period-from");
const reconPeriodTo = document.getElementById("recon-period-to");
const reconS3Key = document.getElementById("recon-s3-key");
const runMtrReconBtn = document.getElementById("run-mtr-recon-btn");
const runExportReconBtn = document.getElementById("run-export-recon-btn");

runMtrReconBtn.addEventListener("click", async () => {
  const from = reconPeriodFrom.value;
  const to = reconPeriodTo.value;
  const key = reconS3Key.value.trim();
  if (!from || !to || !key) {
    showError("MTR recon needs period from, period to, and the S3 key of an already-uploaded MTR CSV.");
    return;
  }
  const params = new URLSearchParams({ mode: "mtr", s3_key: key, period_from: from, period_to: to });
  await runTrigger(
    runMtrReconBtn,
    `/v1/amazon/run-recon?${params}`,
    `Run MTR reconciliation for ${from} → ${to} against ${key}?`
  );
});

runExportReconBtn.addEventListener("click", async () => {
  const from = reconPeriodFrom.value;
  const to = reconPeriodTo.value;
  if (!from || !to) {
    showError("Order-level export needs both period from and period to.");
    return;
  }
  const params = new URLSearchParams({ period_from: from, period_to: to });
  await runTrigger(
    runExportReconBtn,
    `/v1/amazon/export-recon?${params}`,
    `Export order-level invoice + credit-note CSVs for ${from} → ${to}?`
  );
});

const runExportFinanceRevenueBtn = document.getElementById("run-export-finance-revenue-btn");

runExportFinanceRevenueBtn.addEventListener("click", async () => {
  const from = reconPeriodFrom.value;
  const to = reconPeriodTo.value;
  if (!from || !to) {
    showError("Finance revenue report export needs both period from and period to.");
    return;
  }
  const params = new URLSearchParams({ period_from: from, period_to: to });
  await runTrigger(
    runExportFinanceRevenueBtn,
    `/v1/amazon/export-finance-revenue?${params}`,
    `Run Finance's own Revenue Recognition SQL (scoped to Amazon USA) for ${from} → ${to}?`
  );
});

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
  add("amazon_order_id", orderFilter.value);
  add("stage", stageFilter.value);
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
  const page = await apiGet(`/v1/amazon/orders?${query}`);
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
    <tr class="data-row" data-oid="${escapeHtml(row.amazon_order_id)}">
      <td class="cell-center cell-muted">${offset + i + 1}</td>
      <td class="cell-mono">${escapeHtml(row.amazon_order_id)}</td>
      <td>${escapeHtml(row.order_status || "—")}</td>
      <td>${stageBadge(row.stage)}</td>
      <td>${formatDateOnly(row.purchase_date)}</td>
      <td>${formatDateOnly(row.actual_ship_date)}</td>
      <td>${escapeHtml(row.fulfillment_channel || "—")}</td>
      <td class="cell-mono">${formatMoney(row.order_total_amount)}</td>
      <td class="cell-mono">${row.sap_doc_entry != null ? escapeHtml(String(row.sap_doc_entry)) : "—"}</td>
      <td>${formatDate(row.updated_at)}</td>
      <td class="cell-center"><button type="button" class="btn-view" data-oid="${escapeHtml(row.amazon_order_id)}">View</button></td>
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

async function openOrderDetail(oid) {
  orderDetailTitle.textContent = `Order ${oid}`;
  orderDetailBody.innerHTML = `<p class="muted">Loading…</p>`;
  orderDetailModal.classList.remove("hidden");
  orderDetailModal.setAttribute("aria-hidden", "false");
  try {
    const detail = await apiGet(`/v1/amazon/order-detail?amazon_order_id=${encodeURIComponent(oid)}`);
    orderDetailBody.innerHTML = renderOrderDetail(detail);
  } catch (err) {
    orderDetailBody.innerHTML = `<p class="trace-error">${escapeHtml(err.message)}</p>`;
  }
}

function renderOrderDetail(detail) {
  const h = detail.header || {};
  const lines = detail.lines || [];
  const invoiceLog = detail.invoice_log || [];
  const cnLog = detail.cn_log || [];

  const linesRows = lines
    .map(
      (l) => `
      <tr>
        <td class="cell-mono">${escapeHtml(l.seller_sku)}</td>
        <td>${escapeHtml(truncate(l.title, 40))}</td>
        <td class="cell-center">${escapeHtml(String(l.quantity))}</td>
        <td class="cell-mono">${formatMoney(l.unit_price)}</td>
        <td class="cell-mono">${formatMoney(l.item_price_amount)}</td>
        <td class="cell-mono">${formatMoney(l.item_tax_amount)}</td>
        <td class="cell-mono">${formatMoney(l.shipping_price_amount)}</td>
      </tr>`
    )
    .join("");

  const invoiceRows = invoiceLog
    .map(
      (r) => `
      <tr>
        <td>${escapeHtml(r.status)}</td>
        <td class="cell-mono">${r.sap_doc_entry ?? "—"}</td>
        <td class="cell-mono">${r.sap_doc_num ?? "—"}</td>
        <td>${escapeHtml(truncate(r.error_message, 60) || "—")}</td>
        <td>${formatDate(r.created_at)}</td>
      </tr>`
    )
    .join("");

  const cnRows = cnLog
    .map(
      (r) => `
      <tr>
        <td class="cell-mono">${escapeHtml(r.refund_id)}</td>
        <td>${cnStatusBadge(r.status)}</td>
        <td class="cell-mono">${r.sap_doc_entry ?? "—"}</td>
        <td>${escapeHtml(r.skip_reason || r.error_message || "—")}</td>
        <td>${formatDate(r.updated_at)}</td>
      </tr>`
    )
    .join("");

  return `
    <div class="trace-cards">
      <div class="trace-card"><span class="trace-card-label">Stage</span><span class="trace-card-value">${stageBadge(h.stage)}</span></div>
      <div class="trace-card"><span class="trace-card-label">Status</span><span class="trace-card-value">${escapeHtml(h.order_status || "—")}</span></div>
      <div class="trace-card"><span class="trace-card-label">Channel</span><span class="trace-card-value">${escapeHtml(h.fulfillment_channel || "—")}</span></div>
      <div class="trace-card"><span class="trace-card-label">Ship date</span><span class="trace-card-value">${formatDateOnly(h.actual_ship_date)}</span></div>
      <div class="trace-card"><span class="trace-card-label">SAP DocEntry</span><span class="trace-card-value">${h.sap_doc_entry ?? "—"}</span></div>
      <div class="trace-card"><span class="trace-card-label">Attempts</span><span class="trace-card-value">${h.attempt_count ?? 0}</span></div>
    </div>
    ${h.last_error ? `<p class="sap-error-inline">${escapeHtml(h.last_error)}</p>` : ""}

    <div class="order-detail-box">
      <div class="order-detail-box-title">Lines</div>
      <table class="trace-table">
        <thead><tr><th>SKU</th><th>Title</th><th>Qty</th><th>Unit price</th><th>Item price</th><th>Tax</th><th>Shipping</th></tr></thead>
        <tbody>${linesRows || `<tr><td colspan="7" class="muted">No lines.</td></tr>`}</tbody>
      </table>
    </div>

    <div class="order-detail-box" style="margin-top:1rem">
      <div class="order-detail-box-title">Invoice log</div>
      <table class="trace-table">
        <thead><tr><th>Status</th><th>DocEntry</th><th>DocNum</th><th>Error</th><th>At</th></tr></thead>
        <tbody>${invoiceRows || `<tr><td colspan="5" class="muted">No invoice attempts yet.</td></tr>`}</tbody>
      </table>
    </div>

    <div class="order-detail-box" style="margin-top:1rem">
      <div class="order-detail-box-title">Credit note log</div>
      <table class="trace-table">
        <thead><tr><th>Refund ID</th><th>Status</th><th>DocEntry</th><th>Reason</th><th>At</th></tr></thead>
        <tbody>${cnRows || `<tr><td colspan="5" class="muted">No CN activity.</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

// ── Invoice failures panel ───────────────────────────────────────────────

async function loadFailures() {
  hideError();
  const data = await apiGet("/v1/amazon/invoice-failures?limit=100");
  const body = document.getElementById("failures-body");
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="7" class="loading-cell">No failed invoices.</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (r, i) => `
    <tr>
      <td class="cell-center cell-muted">${i + 1}</td>
      <td class="cell-mono">${escapeHtml(r.amazon_order_id)}</td>
      <td>${escapeHtml(r.order_status || "—")}</td>
      <td>${formatDateOnly(r.actual_ship_date)}</td>
      <td class="cell-center">${escapeHtml(String(r.attempt_count ?? 0))}</td>
      <td>${escapeHtml(truncate(r.last_error, 90) || "—")}</td>
      <td>${formatDate(r.updated_at)}</td>
    </tr>`
    )
    .join("");
}

async function runRetryInvoices(btn) {
  if (!window.confirm("Retry all failed invoices (up to 50, attempt_count < 3)?")) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Retrying…";
  try {
    const result = await apiPost("/v1/amazon/retry-invoices?limit=50");
    hideError();
    window.alert(`Retry finished.\n\n${JSON.stringify(result, null, 2)}`);
    await Promise.all([loadStats(), loadFailures()]);
  } catch (err) {
    showError(`Retry failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// ── Credit notes panel ───────────────────────────────────────────────────

async function loadCnLog() {
  hideError();
  const data = await apiGet("/v1/amazon/cn-log?limit=100");
  const body = document.getElementById("cn-body");
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="8" class="loading-cell">No credit-note activity yet.</td></tr>`;
    return;
  }
  body.innerHTML = items
    .map(
      (r, i) => `
    <tr>
      <td class="cell-center cell-muted">${i + 1}</td>
      <td class="cell-mono">${escapeHtml(r.refund_id)}</td>
      <td class="cell-mono">${escapeHtml(r.amazon_order_id)}</td>
      <td>${formatDateOnly(r.posted_date)}</td>
      <td>${cnStatusBadge(r.status)}</td>
      <td class="cell-mono">${r.sap_doc_entry ?? "—"}</td>
      <td>${escapeHtml(truncate(r.skip_reason || r.error_message, 70) || "—")}</td>
      <td>${formatDate(r.updated_at)}</td>
    </tr>`
    )
    .join("");
}

async function runRetryCn(btn) {
  if (!window.confirm("Re-run credit-note posting for the last 45 days?\n\nDuplicate guards make this safe to re-run.")) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Running…";
  try {
    const postedAfter = new Date(Date.now() - 45 * 24 * 60 * 60 * 1000).toISOString();
    const result = await apiPost(`/v1/amazon/retry-cn?posted_after=${encodeURIComponent(postedAfter)}`);
    hideError();
    window.alert(`CN run finished.\n\n${JSON.stringify(result, null, 2)}`);
    await Promise.all([loadStats(), loadCnLog()]);
  } catch (err) {
    showError(`CN run failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// ── Sync history panel ───────────────────────────────────────────────────

async function loadSyncLog() {
  hideError();
  const data = await apiGet("/v1/amazon/sync-log?limit=50");
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
