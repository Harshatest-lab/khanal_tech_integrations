/* Amazon UK Orders dashboard — plain JS, no dependencies.
 *
 * Served as a static site from its own S3 website bucket (see
 * serverless.yml AmazonUkDashboardSiteBucket) — API_BASE below is this
 * account/stage's API Gateway URL and must be hand-edited (then re-synced,
 * see README) if the dashboard is ever promoted to a different stage or
 * account, same known limitation the Walmart dashboard has with its own
 * hardcoded API_BASE.
 *
 * Auth: Cognito User Pool JWT authorizer on the HTTP API (same pattern as
 * the Amazon US B2C dashboard) — this file calls Cognito's IdP endpoint
 * straight with USER_PASSWORD_AUTH and sends the id_token as a Bearer
 * header on every /api call. If COGNITO_CLIENT_ID is left blank (local dev
 * / open deployments), auth is skipped and every request goes out
 * unauthenticated.
 */
const API_BASE = "https://hy2spqrjz9.execute-api.ap-south-1.amazonaws.com";
const COGNITO_REGION = "ap-south-1";
const COGNITO_CLIENT_ID = "7a2bcpb1lkaofb6ik8dl0rlc49";
const COGNITO_IDP_URL = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`;
const AUTH_ENABLED = Boolean(COGNITO_CLIENT_ID);
const AUTH_STORAGE_KEY = "amazon_uk_b2c_dashboard_auth";

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
  if (AUTH_ENABLED && authSession?.id_token) {
    headers.Authorization = `Bearer ${authSession.id_token}`;
  }
  return headers;
}

async function apiFetch(path, extra) {
  const url = API_BASE + path;
  const res = await fetch(url, { ...(extra || {}), headers: authHeaders((extra || {}).headers) });
  if (res.status === 401 && AUTH_ENABLED) {
    // Session expired mid-use — try one refresh, then force re-login.
    if (await refreshAuthSession()) {
      return fetch(url, { ...(extra || {}), headers: authHeaders((extra || {}).headers) });
    }
    showLoginScreen("Session expired — sign in again.");
    throw new Error("session expired");
  }
  return res;
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

function setupAuth(onReady) {
  if (!AUTH_ENABLED) {
    // No Cognito configured — run open (local dev).
    logoutBtn?.classList.add("hidden");
    showAppShell();
    onReady();
    return;
  }
  authSession = loadAuthSession();
  if (!isSessionValid(authSession)) {
    clearAuthSession();
    showLoginScreen();
  } else {
    showAppShell();
    onReady();
  }

  loginForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginSubmit.disabled = true;
    loginSubmit.textContent = "Signing in…";
    try {
      await loginWithPassword(loginEmail.value.trim(), loginPassword.value);
      showAppShell();
      onReady();
    } catch (err) {
      showLoginScreen(err.message);
    } finally {
      loginSubmit.disabled = false;
      loginSubmit.textContent = "Sign in";
    }
  });

  logoutBtn?.addEventListener("click", () => {
    clearAuthSession();
    location.reload();
  });
}

const $ = (id) => document.getElementById(id);
const money = (n, currency) =>
  n == null ? "–" : (currency === "GBP" || !currency ? "£" : currency + " ") +
    Number(n).toLocaleString("en-GB", { maximumFractionDigits: 2 });

function escapeHtml(s) {
  return (s ?? "").toString().replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtDate(s) {
  if (!s) return "–";
  return new Date(s + "Z").toLocaleString("en-GB",
    { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
}

function pill(status) {
  const known = ["Shipped", "Pending", "Canceled"];
  const cls = known.includes(status) ? status : "other";
  return `<span class="pill ${cls}">${escapeHtml(status) || "–"}</span>`;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 3000);
}

function showError(e) {
  console.error(e);
  toast("⚠ " + e.message);
}

/* ---------- page routing ---------- */
const PAGES = ["orders", "refunds", "sync", "skumap"];

function switchPage(name) {
  PAGES.forEach(p => {
    $(`page-${p}`).classList.toggle("hidden", p !== name);
  });
  document.querySelectorAll(".nav-item").forEach(btn =>
    btn.classList.toggle("active", btn.dataset.page === name));

  if (name === "orders") refreshOrdersPage();
  else if (name === "refunds") { refundsPager.page = 1; loadRefunds(); }
  else if (name === "sync") { syncPager.page = 1; loadSyncLog(); }
  else if (name === "skumap") loadSkuMap();
}

document.querySelectorAll(".nav-item").forEach(btn =>
  btn.addEventListener("click", () => switchPage(btn.dataset.page)));

/* ========================================================================
   ORDERS REPORT PAGE
   ======================================================================== */

const pager = { page: 1, pageSize: 50, pages: 1, total: 0 };
const traceCache = {};       // orderId -> trace data (fetched once, reused on re-expand)
const traceOpen = new Set(); // orderIds currently expanded (cleared on every reload)

function currentFilters() {
  return {
    q: $("f-q").value.trim(),
    status: $("f-status").value,
    sku: $("f-sku").value.trim(),
    item: $("f-item").value.trim(),
    qty: $("f-qty").value.trim(),
    order_from: $("f-order-from").value,
    order_to: $("f-order-to").value,
    updated_from: $("f-updated-from").value,
    updated_to: $("f-updated-to").value,
    line_count: $("f-line-count").value,
    shipping: $("f-shipping").value,
    discount: $("f-discount").value,
    tax_method: $("f-tax-method").value,
    sap_status: $("f-sap-status").value,
    has_refund: $("f-has-refund").value,
  };
}

function buildOrdersParams(extra) {
  const params = new URLSearchParams();
  Object.entries(currentFilters()).forEach(([k, v]) => { if (v) params.set(k, v); });
  Object.entries(extra || {}).forEach(([k, v]) => { if (v != null && v !== "") params.set(k, v); });
  return params;
}

async function loadStats() {
  const res = await apiFetch("/api/stats");
  if (!res.ok) throw new Error(`stats failed (${res.status})`);
  const data = await res.json();
  const k = data.kpis;

  $("sum-total").textContent = `${Number(k.total_orders).toLocaleString("en-GB")} orders`;
  $("sum-lines").textContent = `${Number(k.total_lines).toLocaleString("en-GB")} lines`;
  $("sum-posted").textContent = `${Number(k.sap_posted).toLocaleString("en-GB")} SAP posted`;
  $("sum-failed").textContent = `${Number(k.sap_failed).toLocaleString("en-GB")} SAP failed`;
  $("sum-lastsync").textContent = "Last sync: " + (k.last_sync_at ? `${fmtDate(k.last_sync_at.replace(" ", "T").split(".")[0])} UTC` : "–");

  const sel = $("f-status");
  const current = sel.value;
  sel.innerHTML = '<option value="">All statuses</option>' +
    data.by_status.map(r => `<option>${escapeHtml(r.order_status)}</option>`).join("");
  sel.value = current;
}

/* ---------- notification bell (SAP posting failures) ----------
   Not live/push — refreshed on load and on the same 5-minute auto-refresh
   as the rest of the dashboard (see "boot" at the bottom of this file). */

const sapAlertWrap = $("sap-alert-wrap");
const sapAlertBell = $("sap-alert-bell");
const sapAlertCount = $("sap-alert-count");
const sapAlertPanel = $("sap-alert-panel");
const sapAlertPanelBody = $("sap-alert-panel-body");
const sapAlertPanelCount = $("sap-alert-panel-count");

function updateSapAlertBell(total) {
  sapAlertCount.textContent = String(total);
  sapAlertCount.classList.toggle("hidden", total < 1);
  sapAlertBell.classList.toggle("sap-alert-bell--active", total > 0);
  sapAlertBell.title = total > 0 ? `${total} SAP sync failure(s) — click to view` : "No SAP sync failures";
  sapAlertPanelCount.textContent = String(total);
}

function renderSapFailureItems(items) {
  if (!items.length) return '<p class="muted small">No SAP sync failures.</p>';
  return items.map(row => `
    <article class="sap-alert-item">
      <div class="sap-alert-item-head">
        <span class="pill other">${row.kind === "credit_note" ? "Credit note" : "Invoice"}</span>
        <span class="order-id">${escapeHtml(row.ref_id)}</span>
      </div>
      <div class="sap-alert-item-error">${escapeHtml(row.error)}</div>
    </article>`).join("");
}

async function loadSapFailures() {
  try {
    const res = await apiFetch("/api/sap-failures?limit=50");
    if (!res.ok) throw new Error(`sap-failures failed (${res.status})`);
    const data = await res.json();
    updateSapAlertBell(data.total ?? 0);
    sapAlertPanelBody.innerHTML = renderSapFailureItems(data.items || []);
  } catch (e) {
    console.error(e);
  }
}

sapAlertBell.addEventListener("click", () => {
  const willOpen = sapAlertPanel.classList.contains("hidden");
  sapAlertPanel.classList.toggle("hidden", !willOpen);
  sapAlertBell.setAttribute("aria-expanded", String(willOpen));
});

document.addEventListener("click", (e) => {
  if (!sapAlertWrap.contains(e.target)) sapAlertPanel.classList.add("hidden");
});

$("sap-alert-view-all").addEventListener("click", () => {
  sapAlertPanel.classList.add("hidden");
  switchPage("orders");
  $("f-sap-status").value = "failed";
  applyFilters();
});

function skuSummary(o) {
  if (!o.line_count || o.line_count <= 1) return o.skus || "–";
  const first = (o.skus || "").split(", ")[0] || "–";
  return `${first} +${o.line_count - 1} more`;
}

function sapCell(o) {
  if (o.sap_status === "posted") {
    return `<div class="sap-cell"><span class="pill Shipped">Posted #${escapeHtml(o.sap_doc_num)}</span></div>`;
  }
  if (o.sap_status) {
    return `<div class="sap-cell">
      <span class="pill Canceled">Failed</span>
      <span class="sap-error-text" title="${escapeHtml(o.sap_error)}">${escapeHtml(o.sap_error)}</span>
    </div>`;
  }
  return '<span class="muted">–</span>';
}

function orderRowHtml(o) {
  const isOpen = traceOpen.has(o.amazon_order_id);
  return `
    <tr class="order-row" data-order-id="${o.amazon_order_id}">
      <td><button class="trace-toggle ${isOpen ? "open" : ""}" data-order-id="${o.amazon_order_id}" title="View order trace">${isOpen ? "▾" : "▸"}</button></td>
      <td class="order-id">${escapeHtml(o.amazon_order_id)}${o.is_prime ? ' <span class="prime" title="Prime">✓</span>' : ""}${o.refund_count > 0 ? ` <span class="pill other" title="${o.refund_count} refund(s) on this order">↩ Refunded</span>` : ""}</td>
      <td>${fmtDate(o.purchase_date)}</td>
      <td>${pill(o.order_status)}</td>
      <td class="num" title="${escapeHtml(o.titles)}">${o.line_count ?? "–"}</td>
      <td class="order-id">${escapeHtml(skuSummary(o))}</td>
      <td class="num">${o.total_qty ?? "–"}</td>
      <td>${fmtDate(o.last_update_date)}</td>
      <td>${sapCell(o)}</td>
    </tr>
    <tr class="trace-row ${isOpen ? "" : "hidden"}" data-trace-for="${o.amazon_order_id}">
      <td colspan="9"></td>
    </tr>`;
}

async function loadOrders() {
  const params = buildOrdersParams({ page: pager.page, page_size: pager.pageSize });
  const res = await apiFetch(`/api/orders?${params}`);
  if (!res.ok) throw new Error(`orders failed (${res.status})`);
  const data = await res.json();
  pager.pages = data.pages;
  pager.total = data.total;
  pager.page = Math.min(data.page, data.pages);

  $("orders-count-sub").textContent = `Records (${data.orders.length} of ${data.total.toLocaleString("en-GB")})`;

  const { orders } = data;
  $("orders-body").innerHTML = orders.length
    ? orders.map(orderRowHtml).join("")
    : '<tr><td colspan="9" class="muted center">no orders match</td></tr>';

  $("orders-body").querySelectorAll(".trace-toggle").forEach(btn =>
    btn.addEventListener("click", () => toggleTrace(btn.dataset.orderId)));

  // re-render any trace panels that were already open (order reload keeps
  // the row present, so re-populate from cache instead of re-fetching)
  traceOpen.forEach(id => {
    if (traceCache[id]) renderTraceInto(id, traceCache[id]);
  });

  renderPager();
}

async function refreshOrdersPage() {
  try {
    await Promise.all([loadStats(), loadOrders()]);
  } catch (e) {
    showError(e);
  }
}

/* ---------- inline order trace ---------- */

async function toggleTrace(orderId) {
  const row = document.querySelector(`tr.trace-row[data-trace-for="${CSS.escape(orderId)}"]`);
  const toggleBtn = document.querySelector(`.trace-toggle[data-order-id="${CSS.escape(orderId)}"]`);
  if (!row) return;

  if (traceOpen.has(orderId)) {
    traceOpen.delete(orderId);
    row.classList.add("hidden");
    toggleBtn.classList.remove("open");
    toggleBtn.textContent = "▸";
    return;
  }

  traceOpen.add(orderId);
  row.classList.remove("hidden");
  toggleBtn.classList.add("open");
  toggleBtn.textContent = "▾";

  if (traceCache[orderId]) {
    renderTraceInto(orderId, traceCache[orderId]);
    return;
  }

  row.querySelector("td").innerHTML = '<div class="trace-loading">Loading order trace…</div>';
  try {
    const res = await apiFetch(`/api/orders/${encodeURIComponent(orderId)}/trace`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `failed (${res.status})`);
    traceCache[orderId] = data;
    renderTraceInto(orderId, data);
  } catch (e) {
    row.querySelector("td").innerHTML = `<div class="trace-loading">Error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderTraceInto(orderId, data) {
  const row = document.querySelector(`tr.trace-row[data-trace-for="${CSS.escape(orderId)}"]`);
  if (!row) return;
  row.querySelector("td").innerHTML = renderTrace(data);
  const postBtn = row.querySelector(".post-sap-btn");
  if (postBtn) postBtn.addEventListener("click", () => postOrderToSap(orderId, postBtn));
}

// Queue ONE order to SAP (POST /api/orders/{id}/post -> 202), then poll the
// trace until the posting row flips to posted/failed (or ~3 min pass).
async function postOrderToSap(orderId, btn) {
  if (!confirm(`Post order ${orderId} to SAP as an AR Invoice?`)) return;
  const status = btn.parentElement.querySelector(".post-sap-status");
  btn.disabled = true;
  status.textContent = "Queuing…";
  try {
    const res = await apiFetch(`/api/orders/${encodeURIComponent(orderId)}/post`, { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (res.status !== 202) throw new Error(body.error || `failed (${res.status})`);
    status.textContent = "Posting to SAP…";
    const before = traceCache[orderId]?.sap?.posted_at || null;
    for (let i = 0; i < 45; i++) {
      await new Promise(r => setTimeout(r, 4000));
      const tr = await apiFetch(`/api/orders/${encodeURIComponent(orderId)}/trace`);
      const data = await tr.json();
      if (!tr.ok) continue;
      traceCache[orderId] = data;
      const sap = data.sap;
      if (sap && (sap.status === "posted" || sap.posted_at !== before)) {
        renderTraceInto(orderId, data);
        loadOrders();
        return;
      }
    }
    status.textContent = "Still processing — refresh the trace in a minute.";
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
    btn.disabled = false;
  }
}

function kv(k, v) {
  return `<div class="trace-kv"><span class="k">${escapeHtml(k)}</span><span class="v">${v}</span></div>`;
}

function renderTraceLine(l) {
  return `
    <div class="trace-line">
      <div class="trace-line-head">
        <span class="trace-line-sku">${escapeHtml(l.seller_sku)}</span>
        ${pill("")}<span class="muted small">Qty ${l.quantity_ordered ?? "–"}</span>
        ${l.asin ? `<span class="muted small">ASIN ${escapeHtml(l.asin)}</span>` : ""}
      </div>
      <div class="trace-line-title">${escapeHtml(l.title)}</div>
      <div class="trace-line-grid">
        <div>
          <h3 style="margin-top:0">Charges &amp; tax</h3>
          ${kv("Item price", money(l.item_price))}
          ${kv("Item tax", money(l.item_tax))}
          ${kv("Item discount", money(l.item_discount))}
          ${kv("Shipping price", money(l.shipping_price))}
          ${kv("Shipping tax", money(l.shipping_tax))}
          ${kv("Shipping discount", money(l.shipping_discount))}
          ${l.gift_wrap_price ? kv("Gift wrap price", money(l.gift_wrap_price)) : ""}
        </div>
        <div>
          <h3 style="margin-top:0">Shipment</h3>
          ${kv("Tracking", escapeHtml(l.tracking_number) || "–")}
          ${kv("Carrier", escapeHtml(l.carrier) || "–")}
          ${kv("Ship date", l.shipment_date ? fmtDate(l.shipment_date) + " UTC" : "–")}
        </div>
      </div>
      ${l.raw_report_row ? `
      <details class="trace-json">
        <summary>Shipment report raw row (JSON)</summary>
        <pre>${escapeHtml(JSON.stringify(l.raw_report_row, null, 2))}</pre>
      </details>` : `<div class="trace-loading small">No shipment report row yet — order not shipped/reported</div>`}
    </div>`;
}

function renderTrace(d) {
  const sap = d.sap;
  const cards = [
    ["Customer order", escapeHtml(d.amazon_order_id)],
    ["Order date (UTC)", fmtDate(d.purchase_date)],
    ["Updated (UTC)", fmtDate(d.last_update_date)],
    ["Ship to", [d.ship_city, d.ship_postal_code, d.ship_country].filter(Boolean).map(escapeHtml).join(", ") || "–"],
    ["Fulfilment", d.fulfillment_channel === "AFN" ? "FBA (AFN)" : "Seller (MFN)"],
    ["Line items", d.lines.length],
  ];

  const sapStatusHtml = !sap
    ? '<span class="muted">Not synced</span>'
    : sap.status === "posted"
      ? `<span class="pill Shipped">Posted — DocNum ${escapeHtml(sap.sap_doc_num)}</span>`
      : `<span class="pill Canceled">Failed</span> <span class="small">${escapeHtml(sap.error)}</span>`;

  const reqPayload = d.sap_request && d.sap_request.payload;
  // Same eligibility the server enforces: Shipped, not yet posted, and a
  // buildable payload (otherwise the post would just record a failure).
  const canPostToSap = d.order_status === "Shipped" && !(sap && sap.status === "posted") && !!reqPayload;

  return `
    <div class="trace-panel">
      <h3>Order trace</h3>
      <div class="trace-cards">
        ${cards.map(([label, value]) => `
          <div class="trace-card">
            <div class="trace-card-label">${escapeHtml(label)}</div>
            <div class="trace-card-value">${value}</div>
          </div>`).join("")}
      </div>

      <h3>Order lines</h3>
      ${d.lines.map(renderTraceLine).join("")}

      <h3>Order totals</h3>
      <div class="trace-card" style="max-width:260px">
        ${kv("Grand total", money(d.order_total_amount, d.order_total_currency))}
      </div>

      <h3>SAP sync</h3>
      <div class="trace-card" style="max-width:420px">${sapStatusHtml}</div>
      ${canPostToSap ? `
      <div style="margin-top:8px">
        <button class="btn primary small post-sap-btn">Post to SAP</button>
        <span class="small muted post-sap-status"></span>
      </div>` : ""}

      <h3>Refunds</h3>
      ${d.refunds && d.refunds.length ? `
      <div class="trace-cards">
        ${d.refunds.map(r => `
          <div class="trace-card">
            <div class="trace-card-label">${fmtDate(r.posted_date)} UTC — ${escapeHtml(r.transaction_type)}</div>
            <div class="trace-card-value">
              ${money(r.total_amount, r.currency)}
              ${r.sap_status === "posted"
                ? ` <span class="pill Shipped">Credit note #${escapeHtml(r.sap_doc_num)}</span>`
                : r.sap_status === "failed"
                  ? ' <span class="pill Canceled">Credit note failed</span>'
                  : ' <span class="muted small">Not yet posted</span>'}
            </div>
          </div>`).join("")}
      </div>` : '<div class="trace-card" style="max-width:420px"><span class="muted">No refunds on this order</span></div>'}

      <details class="trace-json">
        <summary>SAP request payload (JSON)${reqPayload ? "" : " — not postable"}</summary>
        <pre>${escapeHtml(JSON.stringify(reqPayload || d.sap_request, null, 2))}</pre>
      </details>
      ${sap && sap.sap_response ? `
      <details class="trace-json">
        <summary>SAP response (JSON)</summary>
        <pre>${escapeHtml(JSON.stringify(sap.sap_response, null, 2))}</pre>
      </details>` : ""}
      <details class="trace-json">
        <summary>Raw Amazon order payload (JSON)</summary>
        <pre>${escapeHtml(JSON.stringify(d.raw_order, null, 2))}</pre>
      </details>
    </div>`;
}

function toCsv(rows, columns) {
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const header = columns.map(c => esc(c.label)).join(",");
  const body = rows.map(r => columns.map(c => esc(c.get(r))).join(",")).join("\n");
  return header + "\n" + body;
}

function downloadCsv(csv, filename) {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const EXPORT_COLUMNS = [
  { label: "Order #", get: r => r.amazon_order_id },
  { label: "Order Date (UTC)", get: r => r.purchase_date },
  { label: "Status", get: r => r.order_status },
  { label: "SKU(s)", get: r => r.skus },
  { label: "Item(s)", get: r => r.titles },
  { label: "Total Qty", get: r => r.total_qty },
  { label: "Line Count", get: r => r.line_count },
  { label: "Updated (UTC)", get: r => r.last_update_date },
  { label: "Order Total", get: r => r.order_total_amount },
  { label: "Currency", get: r => r.order_total_currency },
  { label: "SAP Status", get: r => r.sap_status || "not synced" },
  { label: "SAP DocNum", get: r => r.sap_doc_num },
  { label: "SAP Error", get: r => r.sap_error },
];

async function exportOrders(extraParams, filename) {
  try {
    const params = buildOrdersParams({ page: 1, page_size: 5000, ...extraParams });
    const res = await apiFetch(`/api/orders?${params}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `export failed (${res.status})`);
    if (!data.orders.length) return toast("Nothing to export for the current filters");
    downloadCsv(toCsv(data.orders, EXPORT_COLUMNS), filename);
    toast(`✓ Exported ${data.orders.length} rows`);
  } catch (e) {
    showError(e);
  }
}
$("export-filtered-btn").addEventListener("click", () => exportOrders({}, "amazon-uk-orders-filtered.csv"));
$("export-failed-btn").addEventListener("click", () => exportOrders({ sap_status: "failed" }, "amazon-uk-orders-failed.csv"));

/* ---------- filters wiring ---------- */

function applyFilters() {
  pager.page = 1;
  refreshOrdersPage();
}
$("apply-filters-btn").addEventListener("click", applyFilters);

function clearFilters() {
  ["f-q", "f-sku", "f-item", "f-qty", "f-order-from", "f-order-to", "f-updated-from", "f-updated-to"]
    .forEach(id => { $(id).value = ""; });
  ["f-status", "f-line-count", "f-shipping", "f-discount", "f-tax-method", "f-sap-status", "f-has-refund"]
    .forEach(id => { $(id).value = ""; });
  applyFilters();
}
$("clear-filters-btn").addEventListener("click", clearFilters);

// Auto-apply: selects/dates fire immediately on change; free-text search
// boxes debounce on input so filtering doesn't fire on every keystroke.
// Refresh button and Enter-key both still work too, just no longer required.
let filterDebounce;
document.querySelectorAll("#page-orders .filters-grid select, #page-orders .filters-grid input[type=date]")
  .forEach(el => el.addEventListener("change", applyFilters));

document.querySelectorAll("#page-orders .filters-grid input[type=search], #page-orders .filters-grid input[type=number]")
  .forEach(el => el.addEventListener("input", () => {
    clearTimeout(filterDebounce);
    filterDebounce = setTimeout(applyFilters, 400);
  }));

document.querySelectorAll(".filters-grid input").forEach(el =>
  el.addEventListener("keydown", e => { if (e.key === "Enter") { clearTimeout(filterDebounce); applyFilters(); } }));

/* ---------- pagination ---------- */

function renderPager() {
  const from = pager.total === 0 ? 0 : (pager.page - 1) * pager.pageSize + 1;
  const to = Math.min(pager.page * pager.pageSize, pager.total);
  $("page-info").textContent = `Showing ${from}–${to} of ${pager.total.toLocaleString("en-GB")} orders`;

  $("prev-btn").disabled = pager.page <= 1;
  $("next-btn").disabled = pager.page >= pager.pages;

  const nums = new Set([1, pager.pages, pager.page, pager.page - 1, pager.page + 1]);
  const sorted = [...nums].filter(n => n >= 1 && n <= pager.pages).sort((a, b) => a - b);
  let html = "";
  let prev = 0;
  for (const n of sorted) {
    if (n - prev > 1) html += '<span class="dots">…</span>';
    html += `<button class="btn page-btn pg-num ${n === pager.page ? "active" : ""}" data-page="${n}">${n}</button>`;
    prev = n;
  }
  $("page-numbers").innerHTML = html;
  $("page-numbers").querySelectorAll(".pg-num").forEach(btn =>
    btn.addEventListener("click", () => goToPage(Number(btn.dataset.page))));
}

function goToPage(p) {
  pager.page = Math.min(Math.max(p, 1), pager.pages);
  traceOpen.clear();
  refreshOrdersPage();
  window.scrollTo({ top: 0, behavior: "smooth" });
}
$("prev-btn").addEventListener("click", () => goToPage(pager.page - 1));
$("next-btn").addEventListener("click", () => goToPage(pager.page + 1));
$("page-size").addEventListener("change", (e) => {
  pager.pageSize = Number(e.target.value);
  goToPage(1);
});

/* ========================================================================
   REFUNDS PAGE (read-only — no fetch/post actions; fetch and SAP posting
   run only from amazonUkFetchRefunds / amazonUkPostCreditNotes's own
   EventBridge schedules)
   ======================================================================== */

const refundsPager = { page: 1, pageSize: 50, pages: 1, total: 0 };
let refundsPageData = [];                 // current page's refund rows
const refundTraceOpen = new Set();        // row keys currently expanded

function refundRowKey(r) {
  return `${r.transaction_id}|${r.item_index}`;
}

function renderRawJson(label, obj) {
  if (!obj || (Array.isArray(obj) && !obj.length)) return "";
  const text = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  return `
      <details class="trace-json">
        <summary>${escapeHtml(label)}</summary>
        <pre>${escapeHtml(text)}</pre>
      </details>`;
}

function renderRefundTraceInto(r, td) {
  td.innerHTML = `
    <div class="trace-line">
      <div class="trace-line-head">
        <span class="trace-line-sku">${escapeHtml(r.seller_sku) || "–"}</span>
        <span class="pill other">Refund${r.description ? ` — ${escapeHtml(r.description)}` : ""}</span>
        <span class="pill other">${money(r.net_refund, r.currency)} net</span>
      </div>
      <div class="trace-line-grid">
        <div class="trace-kv"><span class="k">Order</span><span class="v order-id">${escapeHtml(r.amazon_order_id) || "–"}</span></div>
        <div class="trace-kv"><span class="k">Order date</span><span class="v">${r.purchase_date ? fmtDate(r.purchase_date) : "–"} UTC</span></div>
        <div class="trace-kv"><span class="k">Posted</span><span class="v">${fmtDate(r.posted_date)} UTC</span></div>
        <div class="trace-kv"><span class="k">Refunded</span><span class="v">${money(r.refunded_amount, r.currency)}</span></div>
        <div class="trace-kv"><span class="k">Fees</span><span class="v">${money(r.fees_total, r.currency)}</span></div>
        <div class="trace-kv"><span class="k">Tax</span><span class="v">${money(r.tax_withheld_total, r.currency)}</span></div>
        <div class="trace-kv"><span class="k">Promotions</span><span class="v">${money(r.promotions_total, r.currency)}</span></div>
        <div class="trace-kv"><span class="k">Other</span><span class="v">${money(r.other_total, r.currency)}</span></div>
        <div class="trace-kv"><span class="k">Net</span><span class="v">${money(r.net_refund, r.currency)}</span></div>
      </div>
      <div data-sap-trace-for="${escapeHtml(refundRowKey(r))}" class="trace-loading">Loading SAP info…</div>
      ${renderRawJson("Item breakdown (item.breakdowns from Finances API)", r.breakdowns)}
      ${renderRawJson("Raw transaction (full JSON from Finances API)", r.raw_transaction)}
    </div>`;
}

async function loadRefundSapTrace(r, key) {
  const container = document.querySelector(`[data-sap-trace-for="${CSS.escape(key)}"]`);
  if (!container) return;
  try {
    const res = await apiFetch(`/api/refunds/${encodeURIComponent(r.transaction_id)}/sap-trace`);
    if (!res.ok) throw new Error(`sap-trace failed (${res.status})`);
    const data = await res.json();
    const sap = data.sap;
    const reqPayload = data.sap_request && data.sap_request.payload;
    const reqError = data.sap_request && data.sap_request.error;
    const reqSkipped = data.sap_request && data.sap_request.skipped;

    const statusHtml = !sap
      ? '<span class="muted">Not posted yet</span>'
      : sap.status === "posted"
        ? `<span class="pill Shipped">Posted — DocNum ${escapeHtml(sap.sap_doc_num)}</span>`
        : `<span class="pill Canceled">Failed</span> <span class="small">${escapeHtml(sap.error)}</span>`;

    container.innerHTML = `
      <div class="trace-kv" style="margin-bottom:8px"><span class="k">SAP credit note</span><span class="v">${statusHtml}</span></div>
      ${reqError ? `<div class="small muted" style="margin-bottom:8px">${escapeHtml(reqError)}</div>` : ""}
      ${reqSkipped && reqSkipped.length ? renderRawJson(`Skipped lines (${reqSkipped.length})`, reqSkipped) : ""}
      <details class="trace-json">
        <summary>SAP Credit Memo request payload (JSON)${reqPayload ? "" : " — not postable"}</summary>
        <pre>${escapeHtml(JSON.stringify(reqPayload || data.sap_request, null, 2))}</pre>
      </details>
      ${sap && sap.sap_response ? `
      <details class="trace-json">
        <summary>SAP response (JSON)</summary>
        <pre>${escapeHtml(JSON.stringify(sap.sap_response, null, 2))}</pre>
      </details>` : ""}`;
  } catch (e) {
    container.innerHTML = `<div class="small muted">Failed to load SAP info: ${escapeHtml(e.message)}</div>`;
  }
}

function toggleRefundTrace(key) {
  const row = document.querySelector(`tr.trace-row[data-refund-trace-for="${CSS.escape(key)}"]`);
  const btn = document.querySelector(`.trace-toggle[data-refund-key="${CSS.escape(key)}"]`);
  if (!row) return;
  if (refundTraceOpen.has(key)) {
    refundTraceOpen.delete(key);
    row.classList.add("hidden");
    if (btn) { btn.classList.remove("open"); btn.textContent = "▸"; }
    return;
  }
  refundTraceOpen.add(key);
  row.classList.remove("hidden");
  if (btn) { btn.classList.add("open"); btn.textContent = "▾"; }
  const r = refundsPageData.find(x => refundRowKey(x) === key);
  if (r) {
    renderRefundTraceInto(r, row.cells[1]);
    loadRefundSapTrace(r, key);
  } else {
    row.cells[1].innerHTML = '<div class="trace-loading">Row not found</div>';
  }
}

function refundsParams(extra) {
  const params = new URLSearchParams({ page: refundsPager.page, page_size: refundsPager.pageSize });
  ["r-q", "r-sku"].forEach(id => {
    const v = $(id).value.trim();
    if (v) params.set(id === "r-q" ? "q" : "sku", v);
  });
  [["r-posted-from", "posted_from"], ["r-posted-to", "posted_to"],
   ["r-order-from", "order_from"], ["r-order-to", "order_to"]].forEach(([id, key]) => {
    if ($(id).value) params.set(key, $(id).value);
  });
  Object.entries(extra || {}).forEach(([k, v]) => { if (v) params.set(k, v); });
  return params;
}

async function loadRefunds() {
  try {
    const res = await apiFetch(`/api/refunds?${refundsParams()}`);
    if (!res.ok) throw new Error(`refunds failed (${res.status})`);
    const data = await res.json();
    const s = data.stats;

    $("sum-refund-events").textContent =
      `${Number(s.refund_events).toLocaleString("en-GB")} refund events`;
    $("sum-refund-amount").textContent = `£${Number(s.total_refunded).toLocaleString("en-GB", { minimumFractionDigits: 2 })} refunded`;
    $("sum-refund-fees").textContent = `£${Number(s.total_fees).toLocaleString("en-GB", { minimumFractionDigits: 2 })} fees`;
    $("sum-refund-fetched").textContent = `Last fetch: ${s.last_fetched ? fmtDate(s.last_fetched) : "–"}`;
    $("refunds-count-sub").textContent = `Records (${data.total.toLocaleString("en-GB")} refund lines)`;

    $("refunds-body").innerHTML = data.refunds.length
      ? data.refunds.map(r => {
          const key = refundRowKey(r);
          return `
          <tr>
            <td><button class="trace-toggle" data-refund-key="${escapeHtml(key)}" title="View refund detail & raw JSON">▸</button></td>
            <td class="order-id">${escapeHtml(r.amazon_order_id) || "–"}</td>
            <td>${r.purchase_date ? fmtDate(r.purchase_date) : "–"}</td>
            <td>${fmtDate(r.posted_date)}</td>
            <td class="order-id">${escapeHtml(r.seller_sku) || "–"}</td>
            <td class="num">${r.quantity_refunded ?? "–"}</td>
            <td class="num bad">${money(r.refunded_amount, r.currency)}</td>
            <td class="num muted">${money(r.fees_total, r.currency)}</td>
            <td class="num muted">${money(r.tax_withheld_total, r.currency)}</td>
            <td class="num bad">${money(r.net_refund, r.currency)}</td>
            <td>${sapCell({ sap_status: r.sap_status, sap_doc_num: r.sap_doc_num, sap_error: r.sap_error })}</td>
          </tr>
          <tr class="trace-row hidden" data-refund-trace-for="${escapeHtml(key)}">
            <td></td><td colspan="10">loading…</td>
          </tr>`;
        }).join("")
      : '<tr><td colspan="11" class="muted center">no refunds stored yet</td></tr>';

    $("refunds-body").querySelectorAll(".trace-toggle").forEach(btn =>
      btn.addEventListener("click", () => toggleRefundTrace(btn.dataset.refundKey)));

    // re-render any trace panels that were already open
    refundsPageData = data.refunds;
    refundTraceOpen.forEach(key => {
      const row = document.querySelector(`tr.trace-row[data-refund-trace-for="${CSS.escape(key)}"]`);
      if (row) {
        row.classList.remove("hidden");
        row.querySelector("button.trace-toggle")?.classList.add("open");
        row.querySelector("button.trace-toggle").textContent = "▾";
        const r = refundsPageData.find(x => refundRowKey(x) === key);
        if (r) {
          renderRefundTraceInto(r, row.cells[1]);
          loadRefundSapTrace(r, key);
        }
      }
    });

    const from = data.total === 0 ? 0 : (data.page - 1) * data.page_size + 1;
    const to = Math.min(data.page * data.page_size, data.total);
    $("refunds-page-info").textContent = `Showing ${from}–${to} of ${data.total.toLocaleString("en-GB")}`;
    $("refunds-prev-btn").disabled = data.page <= 1;
    $("refunds-next-btn").disabled = data.page >= data.pages;
    refundsPager.pages = data.pages;

    const nums = new Set([1, data.pages, data.page, data.page - 1, data.page + 1]);
    const sorted = [...nums].filter(n => n >= 1 && n <= data.pages).sort((a, b) => a - b);
    let html = "";
    let prev = 0;
    for (const n of sorted) {
      if (n - prev > 1) html += '<span class="dots">…</span>';
      html += `<button class="btn page-btn pg-num ${n === data.page ? "active" : ""}" data-page="${n}">${n}</button>`;
      prev = n;
    }
    $("refunds-page-numbers").innerHTML = html;
    $("refunds-page-numbers").querySelectorAll(".pg-num").forEach(btn =>
      btn.addEventListener("click", () => { refundsPager.page = Number(btn.dataset.page); loadRefunds(); }));
  } catch (e) { showError(e); }
}

let refundFilterDebounce;
document.querySelectorAll("#page-refunds .filters-grid select, #page-refunds .filters-grid input[type=date]")
  .forEach(el => el.addEventListener("change", () => { refundsPager.page = 1; loadRefunds(); }));
document.querySelectorAll("#page-refunds .filters-grid input[type=search]")
  .forEach(el => el.addEventListener("input", () => {
    clearTimeout(refundFilterDebounce);
    refundFilterDebounce = setTimeout(() => { refundsPager.page = 1; loadRefunds(); }, 400);
  }));
$("refunds-prev-btn").addEventListener("click", () => { refundsPager.page = Math.max(1, refundsPager.page - 1); loadRefunds(); });
$("refunds-next-btn").addEventListener("click", () => { refundsPager.page = Math.min(refundsPager.pages, refundsPager.page + 1); loadRefunds(); });

/* ========================================================================
   SYNC HISTORY PAGE
   ======================================================================== */

const syncPager = { page: 1, pageSize: 50, pages: 1, total: 0 };

async function loadSyncLog() {
  const params = new URLSearchParams({ page: syncPager.page, page_size: syncPager.pageSize });
  const level = $("sync-level-filter").value;
  if (level) params.set("level", level);

  const res = await apiFetch(`/api/sync-log?${params}`);
  if (!res.ok) return showError(new Error(`sync-log failed (${res.status})`));
  const data = await res.json();
  syncPager.pages = data.pages;
  syncPager.total = data.total;
  syncPager.page = Math.min(data.page, data.pages);

  $("sync-log-body").innerHTML = data.entries.length
    ? data.entries.map(e => `
        <tr>
          <td>${fmtDate(e.created_at.replace(" ", "T").split(".")[0])} UTC</td>
          <td><span class="pill ${e.level === "ERROR" ? "Canceled" : "Shipped"}">${escapeHtml(e.level)}</span></td>
          <td>${escapeHtml(e.message)}</td>
        </tr>`).join("")
    : '<tr><td colspan="3" class="muted center">no log entries</td></tr>';

  const from = syncPager.total === 0 ? 0 : (syncPager.page - 1) * syncPager.pageSize + 1;
  const to = Math.min(syncPager.page * syncPager.pageSize, syncPager.total);
  $("sync-page-info").textContent = `Showing ${from}–${to} of ${syncPager.total.toLocaleString("en-GB")}`;
  $("sync-prev-btn").disabled = syncPager.page <= 1;
  $("sync-next-btn").disabled = syncPager.page >= syncPager.pages;
}
$("sync-level-filter").addEventListener("change", () => { syncPager.page = 1; loadSyncLog(); });
$("sync-prev-btn").addEventListener("click", () => { syncPager.page = Math.max(1, syncPager.page - 1); loadSyncLog(); });
$("sync-next-btn").addEventListener("click", () => { syncPager.page = Math.min(syncPager.pages, syncPager.page + 1); loadSyncLog(); });

/* ========================================================================
   SAP FG ITEMS PAGE
   ======================================================================== */

async function loadSkuMap() {
  const q = $("skumap-search").value.trim();
  const params = q ? `?q=${encodeURIComponent(q)}` : "";
  const res = await apiFetch(`/api/sku-map${params}`);
  if (!res.ok) return showError(new Error(`sku-map failed (${res.status})`));
  const data = await res.json();

  $("skumap-body").innerHTML = data.items.length
    ? data.items.map(i => `
        <tr>
          <td class="order-id">${escapeHtml(i.seller_sku)}</td>
          <td class="order-id">${escapeHtml(i.sap_item_code)}</td>
          <td>${escapeHtml(i.item_description) || "–"}</td>
          <td>${i.is_active ? '<span class="pill Shipped">Active</span>' : '<span class="pill Canceled">Inactive</span>'}</td>
        </tr>`).join("")
    : '<tr><td colspan="4" class="muted center">no matches</td></tr>';
}
let skuDebounce;
$("skumap-search").addEventListener("input", () => {
  clearTimeout(skuDebounce);
  skuDebounce = setTimeout(loadSkuMap, 250);
});

/* ---------- boot ---------- */
setupAuth(() => {
  refreshOrdersPage();
  loadSapFailures();
  setInterval(() => {
    if (!$("page-orders").classList.contains("hidden")) refreshOrdersPage();
    else if (!$("page-refunds").classList.contains("hidden")) loadRefunds();
    loadSapFailures();
  }, 5 * 60 * 1000);
});
