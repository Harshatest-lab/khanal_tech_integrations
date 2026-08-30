const API_BASE = "https://v3330mnmyg.execute-api.ap-south-1.amazonaws.com";
const COGNITO_REGION = "ap-south-1";
const COGNITO_CLIENT_ID = "2t05ft74ug23t0lsk9l176a28j";
const IDP = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`;
const AUTH_KEY = "b2b_so_auth";
const THEME_KEY = "b2b_so_theme";
const TEXT_KEY = "b2b_so_text";
const BG_COLOR_KEY = "b2b_so_bg_color";
const INK_COLOR_KEY = "b2b_so_ink_color";
const LINE_FIELDS = [
  ["item_code", "Item code"],
  ["product_description", "Description"],
  ["sap_code", "SAP code"],
  ["quantity", "Quantity"],
  ["discount", "Discount"],
  ["tax_rate", "Tax rate"],
  ["gst_tax", "GST tax"],
  ["igst_tax", "IGST tax"],
  ["basic_cost_price", "Basic cost price"],
  ["igst_percentage", "IGST %"],
  ["tax_amount", "Tax amount"],
  ["landing_rate", "Landing rate"],
  ["mrp", "MRP"],
  ["total_amount", "Total amount"],
  ["taxcode", "Tax code"],
];

const CHANNELS = [
  { id: "blinkit", label: "Blinkit", live: true },
  { id: "zepto", label: "Zepto", live: false },
  { id: "instamart", label: "Instamart", live: false },
  { id: "bigbasket", label: "Bigbasket", live: false },
  { id: "more", label: "More", live: false },
];

const TITLES = {
  home: ["Overview", "Pipeline health"],
  orders: ["Purchase orders", "Inbox to SAP"],
  maps: ["Item map", "Channel SKU → SAP"],
  addresses: ["Ship-to", "PDF text → SAP address"],
  inbox: ["Mailbox", "Alerts"],
  users: ["Team", "Roles, channels & SAP codes"],
  logs: ["Activity log", "Who created and edited"],
};

let auth = null;
let me = {};
let statusChart = null;
let currentPo = null;
let currentChannel = "blinkit";
let currentView = "home";
let teamUsers = [];
let skuRows = [];
let addrRows = [];
let skuEditKey = "";
let orderSort = { col: "created_at", dir: "desc" };

const $ = (id) => document.getElementById(id);
function setBusy(el, on) {
  if (!el) return;
  if (on) {
    if (el.classList.contains("busy")) return;
    if (!el.dataset.label) el.dataset.label = el.innerHTML;
    el.classList.add("busy");
    el.disabled = true;
    el.setAttribute("aria-busy", "true");
    el.innerHTML = '<span class="btn-dots" aria-hidden="true"><i></i><i></i><i></i></span>';
    return;
  }
  if (!el.classList.contains("busy")) return;
  el.classList.remove("busy");
  el.disabled = false;
  el.removeAttribute("aria-busy");
  if (el.dataset.label) el.innerHTML = el.dataset.label;
}
async function withBusy(el, fn) {
  if (!el) return fn();
  if (el.classList.contains("busy")) return;
  setBusy(el, true);
  try { return await fn(); }
  finally { setBusy(el, false); }
}
function loadAuth() {
  try { return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null"); } catch { return null; }
}
function saveAuth(s) { auth = s; sessionStorage.setItem(AUTH_KEY, JSON.stringify(s)); }
function clearAuth() { auth = null; sessionStorage.removeItem(AUTH_KEY); }
function jwtExp(t) {
  try { return JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))).exp * 1000; } catch { return 0; }
}
function valid(s) { return s?.id_token && Date.now() < jwtExp(s.id_token) - 60_000; }
function hdrs() {
  const h = { "Content-Type": "application/json" };
  if (auth?.id_token) h.Authorization = `Bearer ${auth.id_token}`;
  return h;
}
function money(n) {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(Number(n || 0));
}
function canPost() { return Boolean(me.can_post_sap); }
function canAdmin() { return Boolean(me.can_manage_users); }
const PERM_RANK = { none: 0, view: 1, edit: 2, delete: 3 };
const ROLE_PERM_DEFAULTS = {
  admin: { orders: "delete", sku_map: "delete", ship_to: "delete" },
  reviewer: { orders: "edit", sku_map: "edit", ship_to: "edit" },
  viewer: { orders: "view", sku_map: "view", ship_to: "view" },
};
function permLevel(resource) {
  if (canAdmin()) return "delete";
  return me.permissions?.[resource] || "none";
}
function hasPerm(resource, need) {
  return (PERM_RANK[permLevel(resource)] || 0) >= (PERM_RANK[need] || 0);
}
function canViewOrders() { return hasPerm("orders", "view"); }
function canEditOrders() { return hasPerm("orders", "edit"); }
function canDeleteOrders() { return hasPerm("orders", "delete"); }
function canViewSkuMap() { return hasPerm("sku_map", "view"); }
function canEditSkuMap() { return hasPerm("sku_map", "edit"); }
function canDeleteSkuMap() { return hasPerm("sku_map", "delete"); }
function canViewShipTo() { return hasPerm("ship_to", "view"); }
function canEditShipTo() { return hasPerm("ship_to", "edit"); }
function canDeleteShipTo() { return hasPerm("ship_to", "delete"); }
function canEdit() { return canEditOrders(); }
function permLabel(level) {
  const map = { none: "None", view: "View", edit: "Edit", delete: "Delete" };
  return map[level] || level || "Default";
}
function permSummary(u) {
  const p = u.permissions || {};
  return `PO: ${permLabel(p.orders)} · Map: ${permLabel(p.sku_map)} · Ship: ${permLabel(p.ship_to)}`;
}
function applyRolePermDefaults(role) {
  const d = ROLE_PERM_DEFAULTS[role] || ROLE_PERM_DEFAULTS.viewer;
  $("perm-orders").value = d.orders;
  $("perm-sku-map").value = d.sku_map;
  $("perm-ship-to").value = d.ship_to;
}
function myChannels() {
  if (canAdmin()) return CHANNELS.map((c) => c.id);
  const raw = me.channels || ["blinkit"];
  return CHANNELS.map((c) => c.id).filter((id) => raw.includes(id));
}
function q(path) {
  const url = new URL(path, "https://local.invalid");
  url.searchParams.set("channel", currentChannel);
  return `${url.pathname}?${url.searchParams}`;
}

function toast(msg, bad) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("bad", Boolean(bad));
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 4000);
}

async function cognito(target, body) {
  const res = await fetch(IDP, {
    method: "POST",
    headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}` },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(cognitoErrorMessage(data));
  return data;
}

function cognitoErrorMessage(data) {
  const type = String(data.__type || "").split("#").pop();
  const msg = String(data.message || "");
  const map = {
    CodeMismatchException: "Verification code is incorrect. Check the email and try again.",
    ExpiredCodeException: "Verification code expired. Request a new code.",
    InvalidPasswordException: "Password was rejected. Use 10+ chars with upper, lower, and a number.",
    NotAuthorizedException: msg.includes("Incorrect username or password") ? "Current password is incorrect." : (msg || "Not authorized"),
    LimitExceededException: "Too many attempts. Wait a few minutes and try again.",
    InvalidParameterException: msg || "Invalid input",
  };
  if (map[type]) return map[type];
  return msg || type || "Request failed";
}

function passwordPolicyOk(password) {
  if (password.length < 10) return "Password must be at least 10 characters";
  if (!/[A-Z]/.test(password)) return "Password needs an uppercase letter";
  if (!/[a-z]/.test(password)) return "Password needs a lowercase letter";
  if (!/[0-9]/.test(password)) return "Password needs a number";
  return "";
}

function showForgotPassword(prefillEmail = "") {
  $("login-form").classList.add("hidden");
  $("forgot-form").classList.remove("hidden");
  $("forgot-step-request").classList.remove("hidden");
  $("forgot-step-confirm").classList.add("hidden");
  $("forgot-lead").textContent = "Enter your email and we will send a verification code.";
  $("forgot-error").classList.add("hidden");
  $("forgot-confirm-error").classList.add("hidden");
  $("forgot-email").value = prefillEmail || $("login-email").value.trim();
  $("forgot-code").value = "";
  $("forgot-password").value = "";
  $("forgot-password2").value = "";
}

function showLoginForm() {
  $("forgot-form").classList.add("hidden");
  $("login-form").classList.remove("hidden");
  $("login-error").classList.add("hidden");
}

async function api(path, opts = {}) {
  if (!API_BASE) throw new Error("Set API_BASE in dashboard/app.js after deploy");
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers: { ...hdrs(), ...(opts.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) { clearAuth(); showLogin(); throw new Error("Session expired"); }
  if (!res.ok) throw new Error(body.error || body.message || `${res.status} ${res.statusText || "request failed"}`);
  return body;
}

function showLogin(msg) {
  $("app-shell").classList.add("hidden");
  $("login-screen").classList.remove("hidden");
  showLoginForm();
  if (msg) {
    $("login-error").className = "error";
    $("login-error").textContent = msg;
    $("login-error").classList.remove("hidden");
  }
}
function showApp() {
  $("login-screen").classList.add("hidden");
  $("app-shell").classList.remove("hidden");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function statusClass(s) { return `status s-${s || ""}`; }
function statusLabel(s) {
  return ({
    parsed: "Parsed",
    needs_review: "Needs review",
    ready: "Pending approval",
    approved: "Approved",
    posted: "Posted to SAP",
    failed: "Extract failed",
    rejected: "Rejected",
    cancelled: "Cancelled",
    pending_approval: "Pending approval",
    posted_open: "SAP open",
    posted_closed: "SAP closed",
  }[s] || s || "Unknown");
}
function displayPo(po) {
  const v = String(po || "");
  return v.startsWith("UNPARSED-") ? "Not extracted" : v;
}
function normalizePoDate(raw) {
  const s = String(raw || "").trim();
  if (!s || s === "Not Found" || s === "Invalid Date Format") return "";
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  const d = new Date(s);
  if (!Number.isNaN(d.getTime())) return d.toISOString().slice(0, 10);
  return s;
}
function docDueDate(h, sapLive) {
  return normalizePoDate(sapLive?.DocDueDate) || normalizePoDate(h?.po_expiry_date) || "";
}
function docDueLabel(h, sapLive) {
  const d = docDueDate(h, sapLive);
  return d ? `Doc due ${d}` : "";
}
function hasIssue(r) { return Boolean(r?.last_error) || r?.pipeline_status === "failed"; }
function sapResponse(r) {
  let raw = r?.sap_response;
  if (!raw) return null;
  if (typeof raw === "string") {
    try { raw = JSON.parse(raw); } catch { return { message: raw }; }
  }
  return raw;
}
function isSapPostError(text, r) {
  if (String(r?.sap_status || "").toUpperCase().includes("FAIL")) return true;
  return /SSM |sap_b1_url|companydb|Service Layer|Create Order|Update Order|salesperson code/i.test(String(text || ""));
}
function canApproveRow(r) {
  if (!canEditOrders() || !canPost()) return false;
  if (["posted", "failed", "rejected", "cancelled"].includes(r?.pipeline_status)) return false;
  if (r?.pipeline_status === "ready" && !hasIssue(r)) return true;
  if (isSapPostError(r?.last_error, r)) return true;
  return false;
}
function canRejectRow(r) {
  if (!canEditOrders()) return false;
  return !["posted", "failed", "rejected", "cancelled"].includes(r?.pipeline_status);
}
function canSubmitRow(r) {
  return canApproveRow(r);
}
function shownStatus(r) {
  if (r?.pipeline_status === "failed") return "failed";
  if (r?.last_error && (r.pipeline_status === "parsed" || r.pipeline_status === "ready") && !isSapPostError(r.last_error, r)) return "needs_review";
  return r?.pipeline_status || "";
}
function soStatusLabel(s) {
  if (!s) return "—";
  const map = { bost_Open: "Open", bost_Close: "Closed", bost_Paid: "Paid", cancelled: "Cancelled" };
  if (map[s]) return map[s];
  return String(s).replace(/^bost_/, "").replace(/_/g, " ");
}
function isSapOpen(r) {
  if (!r || r.pipeline_status === "cancelled") return false;
  if (r.pipeline_status !== "posted") return false;
  const st = String(r.sap_document_status || r.DocumentStatus || "").trim();
  if (!st) return true;
  const low = st.toLowerCase();
  if (["cancelled", "bost_close", "closed", "bost_paid", "paid"].includes(low)) return false;
  return low === "open" || low === "bost_open" || low.startsWith("bost_open");
}
function canUpdateSapRow(r) {
  return canEditOrders() && canPost() && isSapOpen(r);
}
function canCancelSapRow(r) {
  return canEditOrders() && canPost() && isSapOpen(r);
}
function sapPostResponse(raw) {
  if (!raw) return null;
  if (typeof raw === "string") {
    try { return JSON.parse(raw); } catch { return { message: raw }; }
  }
  return raw;
}
function formatSapLive(live) {
  if (!live) return "";
  return `<div class="sap-box">
    <p class="eyebrow">SAP live (HANA)</p>
    <p><b>DocNum</b> ${esc(live.DocNum ?? "—")} · <b>DocEntry</b> ${esc(live.DocEntry ?? "—")}</p>
    <p><b>Status</b> ${esc(soStatusLabel(live.DocumentStatus))} · <b>Doc due</b> ${esc(live.DocDueDate || "—")} · <b>Date</b> ${esc(live.DocDate || "—")} · <b>Value</b> ${esc(money(live.DocTotal))}</p>
    <pre>${esc(JSON.stringify(live, null, 2))}</pre>
  </div>`;
}
function errorBits(text) {
  return String(text || "").split(/(?<=\.)\s+/).map((s) => s.trim()).filter(Boolean);
}
function friendlyError(text) {
  return errorBits(text).map((b) => {
    if (/No sufficiently similar address/i.test(b)) return "Ship-to address from the PDF is not in the address map.";
    if (/No matching SAP address/i.test(b)) return "SAP has no ship-to that matches this address.";
    if (/No mapping found for item codes/i.test(b)) return b.replace(/No mapping found for item codes:\s*/i, "These item codes have no SAP mapping: ");
    if (/No mapping found for item codes/i.test(b)) return b.replace(/No mapping found for item codes:\s*/i, "These item codes have no SAP mapping: ");
    if (/No Excel/i.test(b)) return "No Excel file was attached, so line items could not be read.";
    if (/Excel attachment had no usable/i.test(b)) return "The Excel file had no usable line items.";
    if (/This email has no PDF/i.test(b)) return "This email has no PDF, so the purchase order could not be extracted.";
    if (/Could not read a PO number/i.test(b)) return "The PDF was found, but a PO number could not be read.";
    return b;
  });
}
function errorTitle(text) {
  const bits = errorBits(text);
  const t = bits[0] || text || "";
  if (bits.length > 1) return "This purchase order needs attention";
  if (/address/i.test(t)) return "Ship-to address is not mapped";
  if (/item code/i.test(t)) return "Item codes are not mapped to SAP";
  if (/GST/i.test(t)) return "GST does not match SAP";
  if (/SSM |sap_b1_url|companydb|credentials/i.test(t)) return "SAP is not configured — this order was not posted";
  if (/Create Order|Update Order|Service Layer/i.test(t)) return "SAP rejected this order";
  if (/extract|PDF|parse/i.test(t)) return "Could not extract this purchase order";
  if (/Excel|xlsx/i.test(t)) return "Line items could not be extracted";
  return "This purchase order needs attention";
}
function timeAgo(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return d.toLocaleDateString("en-IN");
}
function emptyRows(msg, cols = 8) {
  return `<tr class="empty-row"><td colspan="${cols}">${esc(msg)}</td></tr>`;
}
function lineCard(l, i, editable) {
  const dis = editable ? "" : "disabled";
  return `<div class="line-card">${LINE_FIELDS.map(([f, label]) => {
    const val = l[f] ?? "";
    return `<label>${label}<input data-f="${f}" data-i="${i}" value="${esc(val)}" ${dis} /></label>`;
  }).join("")}</div>`;
}
function showPreview(title, html) {
  $("preview-title").textContent = title;
  $("preview-body").innerHTML = html;
  $("preview-modal").classList.remove("hidden");
}

function parseAmendRecordData(entry) {
  const raw = entry?.record_data;
  if (!raw) return { header: {}, lines: [] };
  if (typeof raw === "string") {
    try { return JSON.parse(raw); } catch { return { header: {}, lines: [] }; }
  }
  return raw;
}

function formatAmendHeaderGrid(h) {
  const rows = [
    ["PO number", h.po_number],
    ["Company", h.company_name],
    ["Contact", h.contact_name],
    ["Delivered to", h.delivered_to],
    ["SAP ship-to", h.matched_address_name],
    ["State", h.matched_state],
    ["GST / GSTIN", h.gst_no],
    ["Bill to", h.bill_to],
    ["Doc due", h.po_expiry_date],
    ["Pipeline status", h.pipeline_status],
    ["SAP status", h.sap_status],
    ["DocNum", h.sap_docnum],
    ["DocEntry", h.sap_docentry],
    ["SAP document status", h.sap_document_status],
    ["Total qty", h.total_quantity],
    ["Total amount", h.total_amount],
    ["Items", h.total_items],
    ["Email subject", h.email_subject],
    ["Created", h.created_at],
    ["Updated", h.updated_at],
    ["Amend status", h.duplicate_records_status],
  ].filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== "");
  if (!rows.length) return "<p class='muted'>No header fields in archive.</p>";
  return `<dl class="amend-kv">${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>`;
}

function formatAmendLinesTable(lines) {
  if (!lines?.length) return "<p class='muted'>No line items in archive.</p>";
  const head = ["#", "Item code", "SAP code", "Description", "Qty", "Tax", "Total"];
  const body = lines.map((l, i) => `<tr>
    <td>${i + 1}</td>
    <td>${esc(l.item_code || "")}</td>
    <td>${esc(l.sap_code || "")}</td>
    <td>${esc(l.product_description || l.item_name || "")}</td>
    <td>${esc(l.quantity ?? "")}</td>
    <td>${esc(l.taxcode || l.tax_rate || "")}</td>
    <td>${esc(l.total_amount ?? "")}</td>
  </tr>`).join("");
  return `<div class="table-wrap"><table class="amend-lines"><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function formatAmendSnapshotSection(entry, versionNo, total) {
  const snap = parseAmendRecordData(entry);
  const h = snap.header || {};
  const meta = [
    entry.archived_at ? timeAgo(entry.archived_at) : "",
    entry.sap_docnum ? `DocNum ${entry.sap_docnum}` : "",
    entry.sap_docentry ? `DocEntry ${entry.sap_docentry}` : "",
    entry.pipeline_status ? `Was ${entry.pipeline_status}` : "",
  ].filter(Boolean).join(" · ");
  return `<section class="amend-snap" id="amend-snap-${esc(entry.id)}">
    <header class="amend-snap-head">
      <h3>Archive ${versionNo} of ${total}</h3>
      <p class="muted">${esc(meta)}</p>
      ${entry.email_subject ? `<p class="muted">${esc(entry.email_subject)}</p>` : ""}
    </header>
    <p class="section-h">Header (as stored before amend)</p>
    ${formatAmendHeaderGrid(h)}
    <p class="section-h">Line items (${(snap.lines || []).length})</p>
    ${formatAmendLinesTable(snap.lines || [])}
    <details class="amend-raw"><summary>Raw JSON snapshot</summary><pre>${esc(JSON.stringify(snap, null, 2))}</pre></details>
  </section>`;
}

async function fetchAmendHistory(po) {
  if (currentPo?.header?.po_number === po && (currentPo.amend_history || []).length) {
    const rows = currentPo.amend_history || [];
    if (rows[0]?.record_data) return rows;
  }
  const data = await api(`/v1/b2b/amend-history?po_number=${encodeURIComponent(po)}&channel=${encodeURIComponent(currentChannel)}`);
  return data.items || [];
}

async function showAmendHistoryForPo(po, amendId = null) {
  const rows = await fetchAmendHistory(po);
  if (!rows.length) {
    toast("No historical snapshots for this PO", true);
    return;
  }
  const total = rows.length;
  if (amendId) {
    const entry = rows.find((r) => String(r.id) === String(amendId));
    if (!entry) {
      toast("Historical snapshot not found", true);
      return;
    }
    const idx = rows.findIndex((r) => String(r.id) === String(amendId));
    showPreview(
      `Historical PO ${displayPo(po)} · archive ${total - idx}`,
      formatAmendSnapshotSection(entry, total - idx, total),
    );
    return;
  }
  const html = rows.map((entry, i) => formatAmendSnapshotSection(entry, total - i, total)).join("");
  showPreview(`Historical data · PO ${displayPo(po)} (${total} archive${total === 1 ? "" : "s"})`, html);
}

function bindAmendHistoryActions(po, amendRows) {
  $("po-history-link")?.addEventListener("click", (e) => {
    e.preventDefault();
    showAmendHistoryForPo(po).catch((err) => toast(err.message, true));
  });
  $("view-all-amend")?.addEventListener("click", (e) => {
    e.preventDefault();
    showAmendHistoryForPo(po).catch((err) => toast(err.message, true));
  });
  $("drawer-body").querySelectorAll("[data-amend-view]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      showAmendHistoryForPo(po, btn.dataset.amendView).catch((err) => toast(err.message, true));
    });
  });
  document.querySelector("#drawer .po-meta .amend-badge-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    showAmendHistoryForPo(po).catch((err) => toast(err.message, true));
  });
}
function writeButtons(extra = "") {
  if (!canEditOrders()) return "";
  return extra;
}
function channelLive() {
  return Boolean(CHANNELS.find((c) => c.id === currentChannel)?.live);
}

function renderNav() {
  const allowed = myChannels();
  $("channel-nav").innerHTML = CHANNELS.filter((c) => allowed.includes(c.id)).map((c) => `
    <div class="ch-block ${currentChannel === c.id ? "open" : ""}" data-ch="${c.id}">
      <button class="ch-head ${currentChannel === c.id ? "open" : ""}" type="button" data-ch-toggle="${c.id}">
        <span class="dot ${c.id}"></span>${c.label}
      </button>
      <div class="ch-sub">
        ${canViewOrders() ? `<button class="nav-btn" data-view="home" data-ch="${c.id}">Overview</button>
        <button class="nav-btn" data-view="orders" data-ch="${c.id}">Purchase orders</button>
        <button class="nav-btn" data-view="inbox" data-ch="${c.id}">Mailbox</button>` : ""}
        ${canViewSkuMap() ? `<button class="nav-btn" data-view="maps" data-ch="${c.id}">Item map</button>` : ""}
        ${canViewShipTo() ? `<button class="nav-btn" data-view="addresses" data-ch="${c.id}">Ship-to</button>` : ""}
      </div>
    </div>
  `).join("");
  document.querySelectorAll("[data-view]").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === currentView && (b.dataset.ch ? b.dataset.ch === currentChannel : true));
  });
}

function setChannel(id) {
  if (!myChannels().includes(id)) return;
  currentChannel = id;
  const meta = CHANNELS.find((c) => c.id === id);
  $("channel-pill").className = `chip ${id}`;
  $("channel-pill").textContent = meta?.label || id;
  $("channel-soon").classList.toggle("hidden", channelLive());
  $("fetch-btn").classList.toggle("hidden", !channelLive());
  renderNav();
}

function setView(name) {
  currentView = name;
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $(`view-${name}`).classList.remove("hidden");
  $("page-kicker").textContent = TITLES[name][0];
  $("page-title").textContent = TITLES[name][1];
  $("sidebar").classList.remove("open");
  renderNav();
}

async function refreshHome() {
  if (!channelLive()) {
    $("kpi-row").innerHTML = "";
    $("home-queue").innerHTML = emptyRows("This channel is not fetching mail yet.");
    return;
  }
  const stats = await api(q("/v1/b2b/stats"));
  const s = stats.summary || {};
  const by = stats.by_pipeline_status || {};
  const cards = [
    ["Total POs", stats.total_pos || 0, CHANNELS.find((c) => c.id === currentChannel)?.label || ""],
    ["Pending approval", s.pending_approval || by.pending_approval || 0, "Not yet approved"],
    ["Needs review", s.needs_review || by.needs_review || 0, "Fix map / address"],
    ["Rejected", s.rejected || by.rejected || 0, "Not sent to SAP"],
    ["SAP open", s.posted_open || by.posted_open || 0, "Posted · open"],
    ["SAP closed", s.posted_closed || by.posted_closed || 0, "Posted · closed"],
    ["Cancelled", s.cancelled || by.cancelled || 0, "Cancelled in SAP"],
    ["Extract failed", s.failed || by.failed || 0, "Could not read mail"],
  ];
  const icons = ["📦", "⏳", "✎", "✕", "☁", "☁", "🚫", "⚠"];
  $("kpi-row").innerHTML = cards.map(([t, v, s], i) => `<div class="kpi"><div class="kpi-ico">${icons[i]}</div><div><span>${t}</span><b>${v}</b><small>${s}</small></div></div>`).join("");
  const labels = Object.keys(by);
  const values = labels.map((k) => by[k]);
  const ctx = $("status-chart");
  if (window.Chart && ctx) {
    statusChart?.destroy();
    statusChart = new Chart(ctx, {
      type: "doughnut",
      data: { labels, datasets: [{ data: values, backgroundColor: ["#0075ff", "#2cd9ff", "#582cff", "#01b574", "#f53939", "#4fd1c5"], borderWidth: 0 }] },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        cutout: "72%",
      },
    });
  }
  const max = Math.max(1, ...values);
  $("value-bars").innerHTML = labels.map((k, i) => `<div class="bar-row"><span>${k}</span><div class="bar"><span style="width:${(values[i] / max) * 100}%"></span></div><b>${values[i]}</b></div>`).join("") || "<p class='muted'>No data yet</p>";
  const attn = await api(q("/v1/b2b/orders?status=needs_review"));
  const failed = await api(q("/v1/b2b/orders?status=failed"));
  const ready = await api(q("/v1/b2b/orders?status=ready"));
  const items = [...(failed.items || []), ...(attn.items || []), ...(ready.items || [])].slice(0, 8);
  $("home-queue").innerHTML = items.map(orderRow).join("") || emptyRows("Nothing waiting.");
  await refreshNotices();
}

function orderRow(r) {
  const err = r.last_error || "";
  const status = shownStatus(r);
  const title = displayPo(r.po_number);
  const due = docDueDate(r);
  const who = r.company_name || r.email_subject || "";
  const bits = friendlyError(err);
  const amend = String(r.duplicate_records_status || "").startsWith("Amended/") ? r.duplicate_records_status : "";
  return `<tr class="order-row${hasIssue(r) ? " has-error" : ""}" data-open="${esc(r.po_number)}">
    <td>
      ${due ? `<div class="cell-sub due-tag">${esc(`Doc due ${due}`)}</div>` : ""}
      ${amend ? `<div class="cell-sub"><button type="button" class="status s-needs_review amend-list-btn" data-act="amend-history" data-open="${esc(r.po_number)}">${esc(amend)} · History</button></div>` : ""}
      <strong class="po-no">${amend ? `<button type="button" class="link-btn po-amend-link" data-act="amend-history" data-open="${esc(r.po_number)}">` : ""}${esc(title)}${amend ? "</button>" : ""}</strong>
      ${who ? `<div class="cell-sub">${esc(who)}</div>` : ""}
      ${err ? `<div class="row-error"><b>${esc(errorTitle(err))}</b>${bits.map((b) => `<span>${esc(b)}</span>`).join("")}</div>` : ""}
    </td>
    <td><span class="${statusClass(status)}">${esc(statusLabel(status))}</span></td>
    <td>${esc(String(r.sap_status || "").toUpperCase().includes("FAIL") ? "Post failed" : soStatusLabel(r.sap_document_status))}</td>
    <td>${r.sap_docnum ? `${esc(r.sap_docnum)}${r.sap_docentry ? `<div class="cell-sub">Entry ${esc(r.sap_docentry)}</div>` : ""}` : "—"}</td>
    <td>${esc(money(r.total_amount))}</td>
    <td>${Number(r.total_items || 0)}</td>
    <td class="muted nowrap">${esc(timeAgo(r.created_at) || "—")}</td>
    <td class="row-actions">
      <button class="btn ghost" type="button" data-act="preview" data-open="${esc(r.po_number)}">Preview</button>
      ${canEditOrders() && r.pipeline_status !== "posted" ? `<button class="btn ghost" type="button" data-act="edit" data-open="${esc(r.po_number)}">Edit</button>` : ""}
      ${canEditOrders() && r.pipeline_status !== "posted" ? `<button class="btn primary" type="button" data-act="refetch" data-open="${esc(r.po_number)}">Refetch</button>` : ""}
      ${canApproveRow(r) ? `<button class="btn primary" type="button" data-act="approve" data-open="${esc(r.po_number)}">Approve</button>` : ""}
      ${canRejectRow(r) ? `<button class="btn ghost" type="button" data-act="reject" data-open="${esc(r.po_number)}">Reject</button>` : ""}
      ${canUpdateSapRow(r) ? `<button class="btn primary" type="button" data-act="update-sap" data-open="${esc(r.po_number)}">Update SAP</button>` : ""}
      ${canCancelSapRow(r) ? `<button class="btn danger" type="button" data-act="cancel-sap" data-open="${esc(r.po_number)}">Cancel SAP</button>` : ""}
      ${canDeleteOrders() && r.pipeline_status !== "posted" ? `<button class="btn danger" type="button" data-act="delete" data-open="${esc(r.po_number)}">Delete</button>` : ""}
    </td>
  </tr>`;
}

async function refreshOrders() {
  if (!channelLive()) { $("orders-list").innerHTML = emptyRows("This channel is not live yet."); return; }
  const params = new URLSearchParams({ channel: currentChannel });
  const status = $("status-filter").value;
  const po = $("po-search").value.trim();
  const sapDocnum = $("sap-docnum-filter").value.trim();
  const sapDocentry = $("sap-docentry-filter").value.trim();
  const createdFrom = $("created-from-filter").value;
  const createdTo = $("created-to-filter").value;
  if (status) params.set("status", status);
  if (po) params.set("po_number", po);
  if (sapDocnum) params.set("sap_docnum", sapDocnum);
  if (sapDocentry) params.set("sap_docentry", sapDocentry);
  if (createdFrom) params.set("created_from", createdFrom);
  if (createdTo) params.set("created_to", createdTo);
  params.set("sort_by", orderSort.col);
  params.set("sort_dir", orderSort.dir);
  const data = await api(`/v1/b2b/orders?${params}`);
  const items = data.items || [];
  $("orders-count").textContent = items.length ? `${items.length} of ${data.total || items.length}` : "";
  $("orders-list").innerHTML = items.map(orderRow).join("") || emptyRows("No purchase orders yet.");
  updateOrderSortIndicators();
}

function updateOrderSortIndicators() {
  document.querySelectorAll(".sort-ind").forEach((el) => {
    const col = el.dataset.sortInd;
    const active = col === orderSort.col;
    el.textContent = active ? (orderSort.dir === "asc" ? " ↑" : " ↓") : "";
    el.closest(".th-sort")?.classList.toggle("active", active);
  });
}

function clearOrderFilters() {
  $("status-filter").value = "";
  $("po-search").value = "";
  $("sap-docnum-filter").value = "";
  $("sap-docentry-filter").value = "";
  $("created-from-filter").value = "";
  $("created-to-filter").value = "";
}

async function openPo(po) {
  const data = await api(`/v1/b2b/order-detail?po_number=${encodeURIComponent(po)}&channel=${encodeURIComponent(currentChannel)}`);
  currentPo = data;
  const h = data.header || {};
  const sapLive = data.sap_live || null;
  const failed = h.pipeline_status === "failed";
  const posted = h.pipeline_status === "posted" || h.pipeline_status === "cancelled";
  const rejected = h.pipeline_status === "rejected";
  const sapRow = { ...h, sap_document_status: sapLive?.DocumentStatus || h.sap_document_status };
  const sapOpen = isSapOpen(sapRow);
  const editable = canEditOrders() && !failed && !rejected && (!posted || sapOpen);
  const lines = data.lines || [];
  const err = h.last_error || "";
  const bits = friendlyError(err);
  const status = shownStatus(h);
  const sapFail = isSapPostError(err, h);
  const postResp = sapPostResponse(data.sap_post_response) || sapResponse(h);
  const blocked = failed || (Boolean(err) && !sapFail && !rejected);
  const pdf = h.pdf_url || "";
  const xlsx = h.xlsx_url || h.excel_url || "";
  const pdfName = String(h.pdf_s3_key || "").split("/").pop() || "PO.pdf";
  const xlsxName = String(h.xlsx_s3_key || "").split("/").pop() || "PO.xlsx";
  const sapAmount = sapLive?.DocTotal ?? h.sap_doc_total ?? h.total_amount;
  const due = docDueDate(h, sapLive);
  const amendRows = data.amend_history || [];
  if (amendRows.length) {
    $("d-title").innerHTML = `${esc(displayPo(h.po_number))} <button type="button" class="link-btn" id="po-history-link">View historical data (${amendRows.length})</button>`;
  } else {
    $("d-title").textContent = displayPo(h.po_number);
  }
  $("d-sub").textContent = [due ? `Doc due ${due}` : "", h.company_name, h.email_subject].filter(Boolean).join(" · ");
  const amendLabel = String(h.duplicate_records_status || "").startsWith("Amended/") ? h.duplicate_records_status : "";
  $("drawer-body").innerHTML = `
    <div class="po-meta">
      ${due ? `<span class="due-tag">Doc due ${esc(due)}</span>` : ""}
      ${amendLabel ? `<button type="button" class="status s-needs_review amend-badge-btn">${esc(amendLabel)} · tap for history</button>` : ""}
      <span class="${statusClass(status)}">${esc(statusLabel(status))}</span>
      <span>${esc(money(sapAmount))}</span>
      <span>${Number(h.total_items || lines.length || 0)} items</span>
      ${h.sap_docnum ? `<span>DocNum ${esc(h.sap_docnum)}</span>` : ""}
      ${h.sap_docentry ? `<span>DocEntry ${esc(h.sap_docentry)}</span>` : ""}
      <span>${esc(String(h.sap_status || "").toUpperCase().includes("FAIL") ? "Post failed" : soStatusLabel(h.sap_document_status || sapLive?.DocumentStatus))}</span>
    </div>
    ${amendRows.length ? `<div class="amend-history-box">
      <p class="section-h">Historical data (archived before amend)</p>
      <p class="muted">Full PO header, line items, and SAP fields as they were before each amend.</p>
      ${amendRows.map((a, i) => {
        const n = amendRows.length - i;
        return `<div class="amend-card">
          <div class="amend-card-head">
            <strong>Archive ${n}</strong>
            <span class="muted">${esc(timeAgo(a.archived_at))}</span>
          </div>
          <p>DocNum ${esc(a.sap_docnum || "—")} · DocEntry ${esc(a.sap_docentry || "—")} · was ${esc(a.pipeline_status || "—")}</p>
          ${a.email_subject ? `<p class="muted">${esc(a.email_subject)}</p>` : ""}
          <button type="button" class="btn ghost" data-amend-view="${esc(a.id)}">View full snapshot</button>
        </div>`;
      }).join("")}
      <button type="button" class="btn primary" id="view-all-amend">View all historical data</button>
    </div>` : ""}
    ${err && !posted ? `<div class="issue-box">
      <p class="eyebrow">${failed ? "Extract failed — not sent to SAP" : rejected ? "Rejected — not sent to SAP" : sapFail ? "SAP post failed — fix and Approve again" : "Blocked — not sent to SAP"}</p>
      <strong>${esc(errorTitle(err))}</strong>
      <ul>${bits.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>
      ${canEditOrders() && !posted && !rejected && !sapFail ? `<p class="muted">After you update Item map or Ship-to, click Refetch to re-read this PO and apply the new maps and SAP tax.</p>
      <button class="btn primary" id="refetch-po" type="button">Refetch</button>` : ""}
    </div>` : ""}
    ${sapOpen && canEditOrders() ? `<p class="muted">SAP order is open — edit lines, click Save, then Update SAP to PATCH changes in SAP.</p>` : ""}
    ${sapLive ? formatSapLive(sapLive) : ""}
    ${postResp && (h.sap_docnum || h.sap_docentry) ? `<div class="sap-box${sapFail ? " bad" : ""}">
      <p class="eyebrow">SAP post response (at approval)</p>
      ${h.sap_docnum ? `<p><b>DocNum</b> ${esc(h.sap_docnum)}</p>` : ""}
      ${h.sap_docentry ? `<p><b>DocEntry</b> ${esc(h.sap_docentry)}</p>` : ""}
      ${h.sap_document_status ? `<p><b>Status</b> ${esc(soStatusLabel(h.sap_document_status))}</p>` : ""}
      <pre>${esc(JSON.stringify(postResp, null, 2))}</pre>
    </div>` : ""}
    <p class="section-h">Customer</p>
    <div class="form-grid">
      <label class="full-span">Doc due date (SAP DocDueDate)<input id="e-due" type="date" value="${esc(due)}" ${editable ? "" : "disabled"} /></label>
      <label>Delivered to<textarea id="e-delivered" ${editable ? "" : "disabled"}>${esc(h.delivered_to || h.delivered_to || "")}</textarea></label>
      <label>SAP ship-to<input id="e-ship" value="${esc(h.matched_address_name || "")}" ${editable ? "" : "disabled"} placeholder="SAP address name" /></label>
      <label>GST / GSTIN<input id="e-gst" value="${esc(h.gst_no || h.gst_no || "")}" ${editable ? "" : "disabled"} /></label>
      <label>Bill to<input id="e-bill" value="${esc(h.bill_to || "")}" ${editable ? "" : "disabled"} /></label>
    </div>
    <p class="section-h">Files</p>
    <div class="docs">
      ${pdf ? `<a class="doc-link" href="${esc(pdf)}" target="_blank" rel="noopener">PDF · ${esc(pdfName)}</a>` : `<span class="empty-box">No PDF on this mail</span>`}
      ${xlsx ? `<a class="doc-link" href="${esc(xlsx)}" target="_blank" rel="noopener">Excel · ${esc(xlsxName)}</a>` : `<span class="empty-box">No Excel on this mail — that is why line items may be empty</span>`}
    </div>
    <p class="section-h">Line items</p>
    <div id="po-lines">${(lines.length ? lines : [{}]).map((l, i) => lineCard(l, i, editable)).join("")}</div>
    <div class="actions">
      ${editable ? `<button class="btn ghost" id="add-line" type="button">Add</button>` : ""}
      ${editable ? `<button class="btn ghost" id="save-po" type="button">Edit / Save</button>` : ""}
      <button class="btn ghost" id="sap-payload" type="button">SAP payload</button>
      <button class="btn ghost" id="raw-payload" type="button">Raw extract</button>
      <button class="btn ghost" id="preview-po" type="button">Preview</button>
      ${canApproveRow(h) ? `<button class="btn primary" id="approve-po" type="button" title="Approve and post to SAP">Approve</button>` : ""}
      ${canRejectRow(h) ? `<button class="btn ghost" id="reject-po" type="button">Reject</button>` : ""}
      ${canUpdateSapRow(sapRow) ? `<button class="btn primary" id="update-sap-po" type="button" title="PATCH open SAP order with current lines">Update SAP</button>` : ""}
      ${canCancelSapRow(sapRow) ? `<button class="btn danger" id="cancel-sap-po" type="button" title="Cancel open SAP order">Cancel SAP</button>` : ""}
      ${canDeleteOrders() && !posted ? `<button class="btn danger" id="delete-po" type="button">Delete</button>` : ""}
    </div>
  `;
  bindAmendHistoryActions(h.po_number, amendRows);
  $("drawer").classList.remove("hidden");
  $("add-line")?.addEventListener("click", () => {
    const box = $("po-lines");
    const i = box.querySelectorAll(".line-card").length;
    box.insertAdjacentHTML("beforeend", lineCard({}, i, true));
  });
  $("save-po")?.addEventListener("click", () => withBusy($("save-po"), () => savePo(h.po_number, lines)).catch((e) => toast(e.message, true)));
  $("preview-po")?.addEventListener("click", () => withBusy($("preview-po"), () => previewPo(h.po_number, "sap")).catch((e) => toast(e.message, true)));
  $("sap-payload")?.addEventListener("click", () => withBusy($("sap-payload"), () => previewPo(h.po_number, "sap")).catch((e) => toast(e.message, true)));
  $("raw-payload")?.addEventListener("click", () => withBusy($("raw-payload"), () => previewPo(h.po_number, "raw")).catch((e) => toast(e.message, true)));
  $("refetch-po")?.addEventListener("click", () => withBusy($("refetch-po"), () => rematchPo(h.po_number)).catch((e) => toast(e.message, true)));
  $("approve-po")?.addEventListener("click", () => withBusy($("approve-po"), () => act("/v1/b2b/approve", h.po_number)).catch((e) => toast(e.message, true)));
  $("reject-po")?.addEventListener("click", () => withBusy($("reject-po"), () => rejectPo(h.po_number)).catch((e) => toast(e.message, true)));
  $("update-sap-po")?.addEventListener("click", () => withBusy($("update-sap-po"), () => updateSapPo(h.po_number)).catch((e) => toast(e.message, true)));
  $("cancel-sap-po")?.addEventListener("click", () => {
    if (!confirm(`Cancel SAP order for ${displayPo(h.po_number)}? This cannot be undone.`)) return;
    withBusy($("cancel-sap-po"), () => cancelSapPo(h.po_number)).catch((e) => toast(e.message, true));
  });
  $("delete-po")?.addEventListener("click", async () => {
    if (!confirm(`Delete purchase order ${displayPo(h.po_number)}? Posted orders cannot be removed.`)) return;
    await withBusy($("delete-po"), () => deletePo(h.po_number, true)).catch((e) => toast(e.message, true));
  });
}

async function savePo(po, lines) {
  const idxs = [...new Set([...document.querySelectorAll("#po-lines [data-i]")].map((el) => Number(el.dataset.i)))];
  const next = idxs.map((i) => {
    const row = { ...(lines[i] || {}) };
    document.querySelectorAll(`#po-lines [data-i="${i}"]`).forEach((inp) => { row[inp.dataset.f] = inp.value; });
    return row;
  }).filter((row) => String(row.item_code || "").trim());
  await api("/v1/b2b/order", {
    method: "PUT",
    body: JSON.stringify({
      po_number: po,
      header: {
        po_expiry_date: $("e-due").value,
        delivered_to: $("e-delivered").value,
        matched_address_name: $("e-ship").value,
        gst_no: $("e-gst").value,
        bill_to: $("e-bill").value,
      },
      lines: next,
    }),
  });
  toast("PO saved");
  await openPo(po);
  await refreshOrders();
  await refreshHome();
}

async function rejectPo(po, reason = "") {
  const out = await api("/v1/b2b/reject", {
    method: "POST",
    body: JSON.stringify({ po_number: po, channel: currentChannel, reason: reason || "Rejected by reviewer" }),
  });
  toast(out.message || "PO rejected");
  try { await openPo(po); } catch (_) {}
  await refreshOrders();
  await refreshHome();
  await refreshInbox();
  await refreshNotices();
}

async function act(path, po) {
  const out = await api(path, { method: "POST", body: JSON.stringify({ po_number: po, channel: currentChannel }) });
  const failed = out.status === "error";
  toast(out.message || out.error || (failed ? "Failed" : "Done"), failed);
  try { await openPo(po); } catch (_) {}
  await refreshOrders();
  await refreshHome();
  await refreshInbox();
  await refreshNotices();
  if (failed) throw new Error(out.message || out.error || "Failed");
}

async function rematchPo(po) {
  const out = await api("/v1/b2b/order-rematch", { method: "POST", body: JSON.stringify({ po_number: po, channel: currentChannel }) });
  if (out.ok) toast("Refetched. Maps and SAP tax applied — you can Approve, then Submit to SAP.");
  else toast(out.message || out.last_error || "Still blocked", true);
  await openPo(po);
  await refreshOrders();
  await refreshHome();
}

async function updateSapPo(po) {
  const out = await api("/v1/b2b/update-sap", { method: "POST", body: JSON.stringify({ po_number: po, channel: currentChannel }) });
  const failed = out.status === "error";
  toast(out.message || out.error || (failed ? "SAP update failed" : "SAP order updated"), failed);
  if (failed) throw new Error(out.message || out.error || "SAP update failed");
  try { await openPo(po); } catch (_) {}
  await refreshOrders();
  await refreshHome();
}

async function cancelSapPo(po) {
  const out = await api("/v1/b2b/cancel-sap", { method: "POST", body: JSON.stringify({ po_number: po, channel: currentChannel }) });
  const failed = out.status === "error";
  toast(out.message || out.error || (failed ? "SAP cancel failed" : "SAP order cancelled"), failed);
  if (failed) throw new Error(out.message || out.error || "SAP cancel failed");
  $("drawer").classList.add("hidden");
  await refreshOrders();
  await refreshHome();
}

async function previewPo(po, mode = "sap") {
  const data = await api(`/v1/b2b/order-preview?po_number=${encodeURIComponent(po)}&channel=${encodeURIComponent(currentChannel)}`);
  const sap = JSON.stringify(data.payload || {}, null, 2);
  const raw = JSON.stringify(data.extract || { header: data.header, lines: data.lines }, null, 2);
  const title = mode === "raw" ? "Raw extract" : "SAP payload";
  const json = mode === "raw" ? raw : sap;
  const note = mode === "raw" ? "What we extracted from the PDF / Excel" : "What Submit will send to SAP";
  showPreview(`PO ${displayPo(po)} · ${title}`, `
    <div class="payload-tabs">
      <button class="btn ${mode === "sap" ? "primary" : "ghost"}" type="button" id="tab-sap">SAP payload</button>
      <button class="btn ${mode === "raw" ? "primary" : "ghost"}" type="button" id="tab-raw">Raw extract</button>
    </div>
    <p class="muted">${note}</p>
    <pre>${esc(json)}</pre>
  `);
  $("tab-sap")?.addEventListener("click", () => previewPo(po, "sap").catch((e) => toast(e.message, true)));
  $("tab-raw")?.addEventListener("click", () => previewPo(po, "raw").catch((e) => toast(e.message, true)));
}
async function deletePo(po, confirmed = false) {
  if (!confirmed && !confirm(`Delete purchase order ${displayPo(po)}? Posted orders cannot be removed.`)) return;
  await api(`/v1/b2b/order?po_number=${encodeURIComponent(po)}&channel=${encodeURIComponent(currentChannel)}`, { method: "DELETE" });
  toast("Purchase order deleted");
  $("drawer").classList.add("hidden");
  await refreshOrders();
  await refreshHome();
}

async function refreshMaps() {
  if (!channelLive()) { $("sku-body").innerHTML = ""; return; }
  const data = await api(q("/v1/b2b/sku-map"));
  skuRows = data.items || [];
  $("sku-form").classList.toggle("hidden", !canEditSkuMap());
  $("sku-body").innerHTML = skuRows.map((r) => `<tr>
    <td>${esc(r.item_code)}</td><td>${esc(r.sap_code)}</td>
    <td class="row-actions">${canEditSkuMap() ? `<button class="btn ghost" type="button" data-map-act="edit" data-item="${esc(r.item_code)}">Edit</button>` : ""}${canDeleteSkuMap() ? `<button class="btn danger" type="button" data-map-act="delete" data-item="${esc(r.item_code)}">Delete</button>` : ""}</td>
  </tr>`).join("") || emptyRows("No item mappings yet. Submit a mapping above.", 3);
}
async function refreshAddresses() {
  if (!channelLive()) { $("addr-body").innerHTML = ""; return; }
  const data = await api(q("/v1/b2b/ship-to"));
  addrRows = data.items || [];
  $("addr-form").classList.toggle("hidden", !canEditShipTo());
  $("addr-body").innerHTML = addrRows.map((r) => `<tr>
    <td>${esc(r.address_id)}</td><td>${esc(r.address_entry)}</td>
    <td class="row-actions">${canEditShipTo() ? `<button class="btn ghost" type="button" data-addr-act="edit" data-id="${esc(r.address_id)}">Edit</button>` : ""}${canDeleteShipTo() ? `<button class="btn danger" type="button" data-addr-act="delete" data-id="${esc(r.address_id)}">Delete</button>` : ""}</td>
  </tr>`).join("") || emptyRows("No ship-to aliases yet. Submit an alias above.", 3);
}
async function refreshInbox() {
  if (!channelLive()) { $("inbox-list").innerHTML = "<p class='muted'>Mailbox for this channel is not live yet.</p>"; return; }
  const data = await api(q("/v1/b2b/notifications"));
  $("inbox-list").innerHTML = (data.items || []).map((n) => {
    const read = Boolean(n.is_read);
    const cls = `inbox-item${n.kind === "error" ? " error" : ""}${read ? " read" : " unread"}`;
    return `<div class="${cls}" data-po="${esc(n.po_number || "")}" data-nid="${esc(n.id || "")}">
    <strong>${esc(n.title || "")}</strong>
    <div>${esc(n.body || "")}</div>
    <small class="muted">${esc(n.po_number ? displayPo(n.po_number) + " · " : "")}${read ? "Read · " : ""}${esc(timeAgo(n.created_at))}</small>
    <div class="row-actions">
      ${!read ? `<button class="btn ghost" type="button" data-act="read">Mark read</button>` : ""}
      ${n.po_number ? `<button class="btn ghost" type="button" data-act="preview">Preview</button>` : ""}
      ${n.po_number && canEditOrders() && canPost() && !read ? `<button class="btn primary" type="button" data-act="approve">Approve</button>` : ""}
      ${n.po_number && canEditOrders() && !read ? `<button class="btn ghost" type="button" data-act="reject">Reject</button>` : ""}
      ${canEditOrders() ? `<button class="btn danger" type="button" data-act="delete">Delete</button>` : ""}
    </div>
  </div>`;
  }).join("") || "<p class='muted'>Inbox is clear.</p>";
}

async function refreshNotices() {
  if (!channelLive()) {
    $("notice-stack").innerHTML = "";
    $("bell-count").classList.add("hidden");
    $("bell-panel").innerHTML = "<p class='muted'>No alerts</p>";
    return;
  }
  const data = await api(`${q("/v1/b2b/notifications")}&unread=true`);
  const items = (data.items || []).filter((n) => n.kind === "error").slice(0, 8);
  $("nav-alerts").textContent = items.length ? `${items.length} error${items.length === 1 ? "" : "s"}` : "";
  $("bell-count").textContent = items.length ? String(items.length) : "";
  $("bell-count").classList.toggle("hidden", !items.length);
  $("bell-panel").innerHTML = items.length ? items.map((n) => `<div class="bell-item" data-po="${esc(n.po_number || "")}" data-nid="${esc(n.id || "")}">
    <strong>${esc(n.title || "Could not process mail")}</strong>
    <p class="muted">${esc(n.body || "")}</p>
  </div>`).join("") : "<p class='muted'>No new errors</p>";
  $("notice-stack").innerHTML = "";
}

async function openAlert(po, nid) {
  if (nid) {
    await api("/v1/b2b/notifications/read", { method: "POST", body: JSON.stringify({ id: nid, channel: currentChannel }) });
    await refreshNotices();
    await refreshInbox();
  }
  if (!po) return;
  setView("orders");
  await openPo(po);
}

function pickedChannels() {
  return [...document.querySelectorAll("#user-channel-picks input:checked")].map((el) => el.value);
}

async function refreshUsers() {
  const data = await api("/v1/b2b/users");
  const self = (me.email || "").toLowerCase();
  teamUsers = data.items || [];
  $("user-channel-picks").innerHTML = CHANNELS.map((c) => `<label><input type="checkbox" value="${c.id}" ${c.id === "blinkit" ? "checked" : ""} /> ${c.label}</label>`).join("");
  $("user-body").innerHTML = teamUsers.map((u) => {
    const mine = String(u.email || "").toLowerCase() === self;
    const on = Boolean(u.is_active);
    const ch = (u.channels || []).join(", ");
    const edit = `<button class="btn ghost" type="button" data-user-act="edit" data-email="${esc(u.email)}">Edit</button>`;
    const toggle = mine ? "" : `<button class="btn ghost" type="button" data-user-act="status" data-email="${esc(u.email)}" data-active="${on ? "0" : "1"}">${on ? "Deactivate" : "Activate"}</button>`;
    const del = mine ? "" : `<button class="btn danger" type="button" data-user-act="delete" data-email="${esc(u.email)}">Delete</button>`;
    return `<tr><td>${esc(u.email)}</td><td>${esc(u.role)}</td><td class="muted small">${esc(permSummary(u))}</td><td>${esc(ch)}</td><td>${esc(u.sap_salesperson_code)}</td><td>${on}</td><td class="filters">${edit}${toggle}${del}</td></tr>`;
  }).join("");
}

function fillUserForm(u) {
  $("user-email").value = u.email || "";
  $("user-name").value = u.full_name || "";
  $("user-role").value = u.role || "viewer";
  $("user-slp").value = u.sap_salesperson_code || "";
  $("user-password").value = "";
  $("user-password").placeholder = "leave blank to keep password";
  $("perm-orders").value = u.perm_orders || "";
  $("perm-sku-map").value = u.perm_sku_map || "";
  $("perm-ship-to").value = u.perm_ship_to || "";
  const set = new Set(u.channels || []);
  document.querySelectorAll("#user-channel-picks input").forEach((el) => { el.checked = set.has(el.value); });
}

async function refreshLogs() {
  const data = await api("/v1/b2b/logs");
  $("logs-body").innerHTML = (data.items || []).map((r) => `<tr>
    <td>${esc(timeAgo(r.created_at))}</td>
    <td>${esc(r.actor)}</td>
    <td>${esc(r.action)}</td>
    <td>${esc(r.channel)}</td>
    <td>${esc(r.po_number || "—")}</td>
    <td>${esc(r.detail)}</td>
  </tr>`).join("") || `<tr><td colspan="6" class="muted">No activity yet.</td></tr>`;
}

async function loadView(name) {
  if (name === "home" && !canViewOrders()) { toast("No access to purchase orders", true); name = canViewSkuMap() ? "maps" : canViewShipTo() ? "addresses" : "home"; }
  if (name === "orders" && !canViewOrders()) { toast("No access to purchase orders", true); return loadView(canViewSkuMap() ? "maps" : "home"); }
  if (name === "maps" && !canViewSkuMap()) { toast("No access to item map", true); return; }
  if (name === "addresses" && !canViewShipTo()) { toast("No access to ship-to", true); return; }
  if (name === "inbox" && !canViewOrders()) { toast("No access to mailbox", true); return; }
  setView(name);
  if (name === "home") await refreshHome();
  if (name === "orders") await refreshOrders();
  if (name === "maps") await refreshMaps();
  if (name === "addresses") await refreshAddresses();
  if (name === "inbox") await refreshInbox();
  if (name === "users") await refreshUsers();
  if (name === "logs") await refreshLogs();
}

async function boot() {
  me = await api("/v1/b2b/me");
  $("who").innerHTML = `<strong>${esc(me.full_name || me.email)}</strong><small>${esc(me.role || "viewer")}${me.sap_salesperson_code ? " · " + esc(me.sap_salesperson_code) : ""}</small>`;
  document.querySelectorAll("[data-admin]").forEach((el) => el.classList.toggle("hidden", !canAdmin()));
  const allowed = myChannels();
  currentChannel = allowed.includes("blinkit") ? "blinkit" : (allowed[0] || "blinkit");
  setChannel(currentChannel);
  const firstView = canViewOrders() ? "home" : canViewSkuMap() ? "maps" : canViewShipTo() ? "addresses" : "home";
  await loadView(firstView);
}

$("pw-toggle")?.addEventListener("click", () => {
  const inp = $("login-password");
  const show = inp.type === "password";
  inp.type = show ? "text" : "password";
  $("pw-toggle").textContent = show ? "Hide" : "Show";
});
$("forgot-pw-toggle")?.addEventListener("click", () => {
  const inp = $("forgot-password");
  const show = inp.type === "password";
  inp.type = show ? "text" : "password";
  $("forgot-pw-toggle").textContent = show ? "Hide" : "Show";
});
$("forgot-open")?.addEventListener("click", () => showForgotPassword());
$("forgot-back")?.addEventListener("click", () => showLoginForm());
async function sendForgotCode(email, btn) {
  const addr = email.trim().toLowerCase();
  if (!addr || !addr.includes("@")) throw new Error("Enter a valid email");
  await cognito("ForgotPassword", { ClientId: COGNITO_CLIENT_ID, Username: addr });
  $("forgot-step-request").classList.add("hidden");
  $("forgot-step-confirm").classList.remove("hidden");
  $("forgot-lead").textContent = `Enter the code sent to ${addr} and choose a new password.`;
  $("forgot-confirm-error").classList.add("hidden");
}
$("forgot-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const onConfirm = !$("forgot-step-confirm").classList.contains("hidden");
  if (!onConfirm) {
    $("forgot-error").classList.add("hidden");
    try {
      await withBusy($("forgot-send"), () => sendForgotCode($("forgot-email").value, $("forgot-send")));
    } catch (err) {
      $("forgot-error").textContent = err.message;
      $("forgot-error").classList.remove("hidden");
    }
    return;
  }
  $("forgot-confirm-error").classList.add("hidden");
  const email = $("forgot-email").value.trim().toLowerCase();
  const code = $("forgot-code").value.trim();
  const password = $("forgot-password").value;
  const password2 = $("forgot-password2").value;
  const policy = passwordPolicyOk(password);
  if (!code) {
    $("forgot-confirm-error").textContent = "Enter the verification code from your email";
    $("forgot-confirm-error").classList.remove("hidden");
    return;
  }
  if (policy) {
    $("forgot-confirm-error").textContent = policy;
    $("forgot-confirm-error").classList.remove("hidden");
    return;
  }
  if (password !== password2) {
    $("forgot-confirm-error").textContent = "New passwords do not match";
    $("forgot-confirm-error").classList.remove("hidden");
    return;
  }
  try {
    await withBusy($("forgot-reset"), async () => {
      await cognito("ConfirmForgotPassword", {
        ClientId: COGNITO_CLIENT_ID,
        Username: email,
        ConfirmationCode: code,
        Password: password,
      });
      showLoginForm();
      $("login-email").value = email;
      $("login-password").value = "";
      $("login-error").textContent = "Password updated. Sign in with your new password.";
      $("login-error").className = "ok";
      $("login-error").classList.remove("hidden");
    });
  } catch (err) {
    $("forgot-confirm-error").textContent = err.message;
    $("forgot-confirm-error").classList.remove("hidden");
  }
});
$("forgot-resend")?.addEventListener("click", async () => {
  $("forgot-confirm-error").classList.add("hidden");
  try {
    await withBusy($("forgot-resend"), () => sendForgotCode($("forgot-email").value, $("forgot-resend")));
    toast("New code sent — check your email");
  } catch (err) {
    $("forgot-confirm-error").textContent = err.message;
    $("forgot-confirm-error").classList.remove("hidden");
  }
});
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").className = "error";
  $("login-error").classList.add("hidden");
  const btn = e.target.querySelector("button[type=submit]");
  try {
    await withBusy(btn, async () => {
      const data = await cognito("InitiateAuth", {
        AuthFlow: "USER_PASSWORD_AUTH",
        ClientId: COGNITO_CLIENT_ID,
        AuthParameters: { USERNAME: $("login-email").value.trim(), PASSWORD: $("login-password").value },
      });
      if (data.ChallengeName) throw new Error(data.ChallengeName);
      const r = data.AuthenticationResult;
      saveAuth({ id_token: r.IdToken, access_token: r.AccessToken, refresh_token: r.RefreshToken });
      showApp();
      await boot();
    });
  } catch (err) { showLogin(err.message); }
});

$("logout-btn").addEventListener("click", () => { clearAuth(); showLogin(); });
function bindPwToggle(toggleId, inputId) {
  $(toggleId)?.addEventListener("click", () => {
    const inp = $(inputId);
    if (!inp) return;
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    $(toggleId).textContent = show ? "Hide" : "Show";
  });
}
bindPwToggle("pw-current-toggle", "pw-current");
bindPwToggle("pw-new-toggle", "pw-new");
$("change-password-btn")?.addEventListener("click", () => {
  $("password-form-error").classList.add("hidden");
  $("pw-current").value = "";
  $("pw-new").value = "";
  $("pw-new2").value = "";
  $("pw-current").type = "password";
  $("pw-new").type = "password";
  $("pw-current-toggle").textContent = "Show";
  $("pw-new-toggle").textContent = "Show";
  $("password-modal").classList.remove("hidden");
});
$("password-close")?.addEventListener("click", () => $("password-modal").classList.add("hidden"));
$("password-modal")?.addEventListener("click", (e) => { if (e.target === $("password-modal")) $("password-modal").classList.add("hidden"); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("password-modal").classList.contains("hidden")) $("password-modal").classList.add("hidden");
});
$("password-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  $("password-form-error").classList.add("hidden");
  const current = $("pw-current").value;
  const next = $("pw-new").value;
  const next2 = $("pw-new2").value;
  const policy = passwordPolicyOk(next);
  if (policy) {
    $("password-form-error").textContent = policy;
    $("password-form-error").classList.remove("hidden");
    return;
  }
  if (next !== next2) {
    $("password-form-error").textContent = "New passwords do not match";
    $("password-form-error").classList.remove("hidden");
    return;
  }
  if (!auth?.access_token) {
    $("password-form-error").textContent = "Session expired — sign in again";
    $("password-form-error").classList.remove("hidden");
    return;
  }
  const btn = e.target.querySelector("button[type=submit]");
  try {
    await withBusy(btn, async () => {
      await cognito("ChangePassword", {
        AccessToken: auth.access_token,
        PreviousPassword: current,
        ProposedPassword: next,
      });
      $("password-modal").classList.add("hidden");
      toast("Password updated");
    });
  } catch (err) {
    $("password-form-error").textContent = err.message;
    $("password-form-error").classList.remove("hidden");
  }
});
$("menu-btn").addEventListener("click", () => $("sidebar").classList.toggle("open"));
$("channel-nav").addEventListener("click", async (e) => {
  const toggle = e.target.closest("[data-ch-toggle]");
  if (toggle) {
    setChannel(toggle.dataset.chToggle);
    await loadView(currentView === "users" || currentView === "logs" ? "home" : currentView).catch((err) => toast(err.message, true));
    return;
  }
  const btn = e.target.closest("[data-view]");
  if (!btn || !btn.dataset.ch) return;
  setChannel(btn.dataset.ch);
  try { await loadView(btn.dataset.view); } catch (err) { toast(err.message, true); }
});
document.querySelectorAll("[data-admin][data-view]").forEach((btn) => {
  btn.addEventListener("click", () => loadView(btn.dataset.view).catch((err) => toast(err.message, true)));
});
$("status-filter").addEventListener("change", () => refreshOrders().catch((e) => toast(e.message, true)));
$("po-search").addEventListener("change", () => refreshOrders().catch((e) => toast(e.message, true)));
$("sap-docnum-filter")?.addEventListener("change", () => refreshOrders().catch((e) => toast(e.message, true)));
$("sap-docentry-filter")?.addEventListener("change", () => refreshOrders().catch((e) => toast(e.message, true)));
$("created-from-filter")?.addEventListener("change", () => refreshOrders().catch((e) => toast(e.message, true)));
$("created-to-filter")?.addEventListener("change", () => refreshOrders().catch((e) => toast(e.message, true)));
$("orders-clear-filters")?.addEventListener("click", () => {
  clearOrderFilters();
  refreshOrders().catch((e) => toast(e.message, true));
});
document.querySelector("#view-orders thead")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".th-sort");
  if (!btn?.dataset.sort) return;
  e.preventDefault();
  const col = btn.dataset.sort;
  if (orderSort.col === col) orderSort.dir = orderSort.dir === "asc" ? "desc" : "asc";
  else { orderSort.col = col; orderSort.dir = col === "created_at" ? "desc" : "asc"; }
  refreshOrders().catch((err) => toast(err.message, true));
});
async function handlePoAction(e) {
  const btn = e.target.closest("[data-act]");
  const po = (btn && btn.dataset.open) || e.target.closest("[data-open]")?.dataset.open;
  if (!po) return;
  e.preventDefault();
  const kind = btn?.dataset.act || "edit";
  if (kind === "delete" && !confirm(`Delete purchase order ${displayPo(po)}? Posted orders cannot be removed.`)) return;
  const work = async () => {
    if (kind === "preview") await previewPo(po);
    else if (kind === "refetch" || kind === "fix") await rematchPo(po);
    else if (kind === "approve") await act("/v1/b2b/approve", po);
    else if (kind === "reject") await rejectPo(po);
    else if (kind === "update-sap") await updateSapPo(po);
    else if (kind === "cancel-sap") {
      if (!confirm(`Cancel SAP order for ${displayPo(po)}? This cannot be undone.`)) return;
      await cancelSapPo(po);
    }
    else if (kind === "submit") await act("/v1/b2b/approve", po);
    else if (kind === "delete") await deletePo(po, true);
    else if (kind === "amend-history") await showAmendHistoryForPo(po);
    else await openPo(po);
  };
  try {
    if (btn) await withBusy(btn, work);
    else await work();
  } catch (err) { toast(err.message, true); }
}
$("orders-list").addEventListener("click", handlePoAction);
$("home-queue").addEventListener("click", handlePoAction);
$("drawer-close").addEventListener("click", () => $("drawer").classList.add("hidden"));
$("drawer-wide")?.addEventListener("click", () => {
  const panel = document.querySelector("#drawer .drawer-panel");
  panel?.classList.toggle("wide");
  $("drawer-wide").textContent = panel?.classList.contains("wide") ? "Narrow" : "Widen";
});
$("drawer").addEventListener("click", (e) => { if (e.target === $("drawer")) $("drawer").classList.add("hidden"); });
$("inbox-list").addEventListener("click", (e) => {
  const item = e.target.closest("[data-nid]");
  if (!item) return;
  const btn = e.target.closest("[data-act]");
  const kind = btn?.dataset.act;
  const nid = item.dataset.nid;
  const po = item.dataset.po;
  const work = async () => {
    if (kind === "preview") return previewPo(po);
    if (kind === "approve") return act("/v1/b2b/approve", po);
    if (kind === "reject") return rejectPo(po);
    if (kind === "submit") return act("/v1/b2b/approve", po);
    if (kind === "read") {
      await api("/v1/b2b/notifications/read", { method: "POST", body: JSON.stringify({ id: Number(nid), channel: currentChannel }) });
      await refreshInbox();
      await refreshNotices();
      toast("Marked as read");
      return;
    }
    if (kind === "delete") {
      if (!confirm("Delete this mailbox alert?")) return;
      await api("/v1/b2b/notifications", { method: "DELETE", body: JSON.stringify({ id: Number(nid), channel: currentChannel }) });
      await refreshInbox();
      await refreshNotices();
      toast("Alert deleted");
      return;
    }
    return openAlert(po, nid);
  };
  (btn ? withBusy(btn, work) : work()).catch((err) => toast(err.message, true));
});
$("notice-stack").addEventListener("click", (e) => {
  const x = e.target.closest(".notice-x");
  if (x) {
    e.stopPropagation();
    openAlert("", x.dataset.nid).catch((err) => toast(err.message, true));
    return;
  }
  const notice = e.target.closest("[data-po]");
  if (notice) openAlert(notice.dataset.po, notice.dataset.nid).catch((err) => toast(err.message, true));
});
$("bell-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  $("theme-panel")?.classList.add("hidden");
  $("bell-panel").classList.toggle("hidden");
});
$("bell-panel").addEventListener("click", (e) => {
  const item = e.target.closest("[data-po]");
  if (!item) return;
  $("bell-panel").classList.add("hidden");
  openAlert(item.dataset.po, item.dataset.nid).catch((err) => toast(err.message, true));
});
$("theme-btn")?.addEventListener("click", (e) => {
  e.stopPropagation();
  $("bell-panel")?.classList.add("hidden");
  $("theme-panel")?.classList.toggle("hidden");
});
$("theme-panel")?.addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => {
  $("bell-panel")?.classList.add("hidden");
  $("theme-panel")?.classList.add("hidden");
});
$("fetch-btn").addEventListener("click", async () => {
  try {
    await withBusy($("fetch-btn"), async () => {
      await api("/v1/b2b/fetch", { method: "POST", body: "{}" });
      toast("Mailbox fetch started — refreshing in a few seconds");
      setTimeout(async () => {
        try {
          await refreshHome();
          await refreshOrders();
          await refreshInbox();
        } catch (e) { toast(e.message, true); }
      }, 15000);
    });
  } catch (e) { toast(e.message, true); }
});
async function submitSku() {
  const item_code = $("sku-item").value.trim();
  const sap_code = $("sku-sap").value.trim();
  if (!item_code || !sap_code) throw new Error("Channel item and SAP item are required");
  if (skuEditKey && skuEditKey !== item_code) {
    await api("/v1/b2b/sku-map", { method: "DELETE", body: JSON.stringify({ item_code: skuEditKey, channel: currentChannel }) });
  }
  await api("/v1/b2b/sku-map", { method: "PUT", body: JSON.stringify({ item_code, sap_code, channel: currentChannel }) });
  skuEditKey = "";
  $("sku-item").value = "";
  $("sku-sap").value = "";
  toast("Item map submitted");
  await refreshMaps();
}
$("sku-save").addEventListener("click", () => withBusy($("sku-save"), submitSku).catch((e) => toast(e.message, true)));
$("sku-add")?.addEventListener("click", () => withBusy($("sku-add"), submitSku).catch((e) => toast(e.message, true)));
$("sku-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-map-act]");
  if (!btn) return;
  const item = btn.dataset.item;
  try {
    if (btn.dataset.mapAct === "edit") {
      const row = skuRows.find((x) => x.item_code === item);
      if (!row) return;
      skuEditKey = row.item_code;
      $("sku-item").value = row.item_code;
      $("sku-sap").value = row.sap_code;
      return;
    }
    if (!confirm(`Delete mapping ${item}?`)) return;
    await withBusy(btn, async () => {
      await api("/v1/b2b/sku-map", { method: "DELETE", body: JSON.stringify({ item_code: item, channel: currentChannel }) });
      toast("Item mapping deleted");
      await refreshMaps();
    });
  } catch (err) { toast(err.message, true); }
});
async function submitAddr() {
  const address_id = $("addr-id").value.trim();
  const address_entry = $("addr-entry").value.trim();
  if (!address_id || !address_entry) throw new Error("SAP name and PO text are required");
  if (addrEditKey && addrEditKey !== address_id) {
    await api("/v1/b2b/ship-to", { method: "DELETE", body: JSON.stringify({ address_id: addrEditKey, channel: currentChannel }) });
  }
  await api("/v1/b2b/ship-to", { method: "PUT", body: JSON.stringify({ address_id, address_entry, channel: currentChannel }) });
  addrEditKey = "";
  $("addr-id").value = "";
  $("addr-entry").value = "";
  toast("Ship-to alias submitted");
  await refreshAddresses();
}
$("addr-save").addEventListener("click", () => withBusy($("addr-save"), submitAddr).catch((e) => toast(e.message, true)));
$("addr-add")?.addEventListener("click", () => withBusy($("addr-add"), submitAddr).catch((e) => toast(e.message, true)));
$("addr-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-addr-act]");
  if (!btn) return;
  const id = btn.dataset.id;
  try {
    if (btn.dataset.addrAct === "edit") {
      const row = addrRows.find((x) => x.address_id === id);
      if (!row) return;
      addrEditKey = row.address_id;
      $("addr-id").value = row.address_id;
      $("addr-entry").value = row.address_entry;
      return;
    }
    if (!confirm(`Delete ship-to ${id}?`)) return;
    await withBusy(btn, async () => {
      await api("/v1/b2b/ship-to", { method: "DELETE", body: JSON.stringify({ address_id: id, channel: currentChannel }) });
      toast("Ship-to alias deleted");
      await refreshAddresses();
    });
  } catch (err) { toast(err.message, true); }
});
$("preview-close")?.addEventListener("click", () => $("preview-modal").classList.add("hidden"));
$("preview-modal")?.addEventListener("click", (e) => { if (e.target === $("preview-modal")) $("preview-modal").classList.add("hidden"); });
$("mark-all-read").addEventListener("click", async () => {
  await withBusy($("mark-all-read"), async () => {
    await api("/v1/b2b/notifications/read", { method: "POST", body: JSON.stringify({ channel: currentChannel }) });
    await refreshInbox();
    await refreshNotices();
    toast("All marked as read");
  }).catch((e) => toast(e.message, true));
});
$("user-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-user-act]");
  if (!btn) return;
  const email = btn.dataset.email;
  try {
    if (btn.dataset.userAct === "edit") {
      const u = teamUsers.find((x) => String(x.email || "").toLowerCase() === String(email).toLowerCase());
      if (u) fillUserForm(u);
      return;
    }
    if (btn.dataset.userAct === "delete" && !confirm(`Delete ${email}? They will not be able to sign in.`)) return;
    await withBusy(btn, async () => {
      if (btn.dataset.userAct === "status") {
        const is_active = btn.dataset.active === "1";
        await api("/v1/b2b/users/status", { method: "POST", body: JSON.stringify({ email, is_active }) });
        toast(is_active ? "User activated" : "User deactivated");
      }
      if (btn.dataset.userAct === "delete") {
        await api(`/v1/b2b/users?email=${encodeURIComponent(email)}`, { method: "DELETE" });
        toast("User deleted");
      }
      await refreshUsers();
    });
  } catch (err) { toast(err.message, true); }
});
$("user-role")?.addEventListener("change", (e) => applyRolePermDefaults(e.target.value));
$("user-save").addEventListener("click", async () => {
  try {
    await withBusy($("user-save"), async () => {
      const password = $("user-password").value;
      await api("/v1/b2b/users", { method: "PUT", body: JSON.stringify({
        email: $("user-email").value.trim(), full_name: $("user-name").value.trim(),
        role: $("user-role").value, sap_salesperson_code: $("user-slp").value.trim(),
        is_active: true, password, channels: pickedChannels(),
        perm_orders: $("perm-orders").value, perm_sku_map: $("perm-sku-map").value, perm_ship_to: $("perm-ship-to").value,
      }) });
      $("user-password").value = "";
      toast("User saved — they can sign in with that email and password");
      await refreshUsers();
    });
  } catch (e) { toast(e.message, true); }
});

function applyLook() {
  const root = document.documentElement;
  const theme = localStorage.getItem(THEME_KEY) || "navy";
  let text = localStorage.getItem(TEXT_KEY) || "default";
  if (theme === "light" && (text === "white" || text === "soft")) text = "default";
  root.dataset.theme = theme;
  if (text === "default") root.removeAttribute("data-text");
  else root.dataset.text = text;
  const bg = localStorage.getItem(BG_COLOR_KEY);
  const ink = localStorage.getItem(INK_COLOR_KEY);
  if (bg) root.style.setProperty("--page-bg", bg);
  else root.style.removeProperty("--page-bg");
  if (ink && text === "custom") {
    root.style.setProperty("--ink", ink);
    root.dataset.text = "custom";
  } else if (text !== "custom") {
    root.style.removeProperty("--ink");
  }
  const themeEl = $("theme-select");
  const textEl = $("text-select");
  const bgEl = $("bg-color");
  const inkEl = $("ink-color");
  if (themeEl) themeEl.value = theme;
  if (textEl) textEl.value = text;
  if (bgEl && bg) bgEl.value = bg;
  if (inkEl) inkEl.value = ink || (theme === "light" ? "#1b1840" : "#e2e8f0");
}
$("theme-select")?.addEventListener("change", (e) => {
  localStorage.setItem(THEME_KEY, e.target.value);
  if (e.target.value === "light") localStorage.setItem(TEXT_KEY, "default");
  applyLook();
});
$("text-select")?.addEventListener("change", (e) => {
  localStorage.setItem(TEXT_KEY, e.target.value);
  applyLook();
});
$("bg-color")?.addEventListener("input", (e) => {
  localStorage.setItem(BG_COLOR_KEY, e.target.value);
  applyLook();
});
$("ink-color")?.addEventListener("input", (e) => {
  localStorage.setItem(INK_COLOR_KEY, e.target.value);
  localStorage.setItem(TEXT_KEY, "custom");
  applyLook();
});
applyLook();

(async function start() {
  auth = loadAuth();
  if (!valid(auth)) { showLogin(); return; }
  showApp();
  try { await boot(); } catch (e) { toast(e.message, true); }
})();
