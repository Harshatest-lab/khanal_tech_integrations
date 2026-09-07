const API_BASE = "https://mil2cadq50.execute-api.ap-south-1.amazonaws.com";
const COGNITO_REGION = "ap-south-1";
const COGNITO_CLIENT_ID = "3geb2fiikuf1v44h4vcbh88t4j";
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
  { id: "zepto", label: "Zepto", live: true },
  { id: "instamart", label: "Instamart", live: true },
  { id: "bigbasket", label: "Bigbasket", live: true },
  { id: "more", label: "More", live: false },
];

const TITLES = {
  home: ["Overview", "Pipeline health"],
  orders: ["Purchase orders", "Inbox to SAP"],
  maps: ["Item map", "Channel SKU → SAP"],
  addresses: ["Ship-to", "PO address → SAP address"],
  inbox: ["Mailbox", "Alerts"],
  users: ["Team", "Roles, channels & SAP codes"],
  logs: ["Activity log", "Who created and edited"],
  "logistics-home": ["Logistics", "Delivery overview"],
  "logistics-delivery": ["Delivery", "Find SAP invoice and link LR"],
  "logistics-tracker": ["Tracker report", "Delhivery LR status · Admin view"],
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
let addrEditKey = "";
let orderSort = { col: "created_at", dir: "desc" };
let navCounts = {};
let orderPage = 0;
let orderPageSize = 10;
let orderTotal = 0;
let poDrawerBaseline = null;

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
  admin: { orders: "delete", sku_map: "delete", ship_to: "delete", delivery: "delete" },
  reviewer: { orders: "edit", sku_map: "edit", ship_to: "edit", delivery: "edit" },
  viewer: { orders: "view", sku_map: "view", ship_to: "view", delivery: "none" },
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
function hasLogisticsModule() {
  if (canAdmin()) return true;
  return (me.channels || []).map((c) => String(c).toLowerCase()).includes("logistics");
}
function canViewDelivery() { return hasPerm("delivery", "view") && hasLogisticsModule(); }
function canEditDelivery() { return hasPerm("delivery", "edit") && hasLogisticsModule(); }
function canEdit() { return canEditOrders(); }

/** Sidebar Logistics links (keep in app.js so nav works even if delivery.js fails to load). */
function renderLogisticsNav() {
  const nav = $("logistics-nav");
  const label = $("logistics-nav-label");
  if (!nav) return;
  if (!canViewDelivery()) {
    nav.classList.add("hidden");
    label?.classList.add("hidden");
    return;
  }
  nav.classList.remove("hidden");
  label?.classList.remove("hidden");
  nav.innerHTML = `
    <button class="nav-btn" data-view="logistics-home" type="button">Overview</button>
    <button class="nav-btn" data-view="logistics-delivery" type="button">Delivery</button>
    <button class="nav-btn" data-view="logistics-tracker" type="button">Tracker report</button>
  `;
}
function permLabel(level) {
  const map = { none: "None", view: "View", edit: "Edit", delete: "Delete" };
  return map[level] || level || "Default";
}
function permSummary(u) {
  const p = u.permissions || {};
  return `PO: ${permLabel(p.orders)} · Map: ${permLabel(p.sku_map)} · Ship: ${permLabel(p.ship_to)} · Logistics: ${permLabel(p.delivery)}`;
}
function applyRolePermDefaults(role) {
  const d = ROLE_PERM_DEFAULTS[role] || ROLE_PERM_DEFAULTS.viewer;
  $("perm-orders").value = d.orders;
  $("perm-sku-map").value = d.sku_map;
  $("perm-ship-to").value = d.ship_to;
  if ($("perm-delivery")) $("perm-delivery").value = d.delivery;
}
function myChannels() {
  const raw = (me.channels || ["blinkit"]).map((c) => String(c).toLowerCase());
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
  clearTimeout(toast._hide);
  toast._hide = setTimeout(() => el.classList.add("hidden"), bad ? 8000 : 4500);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isSapPosting(r) {
  return String(r?.sap_status || "").toUpperCase() === "POSTING";
}

async function waitForSapPost(po, { action = "approve" } = {}) {
  const started = Date.now();
  const hardMs = 5 * 60 * 1000;
  const verb = action === "cancel" ? "Cancelling" : action === "update" ? "Updating" : "Posting";
  toast(`${verb} in SAP — please wait…`);
  while (Date.now() - started < hardMs) {
    await sleep(2500);
    let data;
    try {
      data = await api(`/v1/b2b/order-detail?po_number=${encodeURIComponent(po)}&channel=${encodeURIComponent(currentChannel)}`);
    } catch (_) {
      continue;
    }
    const h = data.header || {};
    const sap = String(h.sap_status || "").toUpperCase();
    if (action === "cancel" && h.pipeline_status === "cancelled") {
      toast(h.message || "SAP order cancelled");
      try { await openPo(po); } catch (_) {}
      await refreshOrders();
      await refreshHome();
      return h;
    }
    if (action !== "cancel" && h.pipeline_status === "posted" && sap !== "POSTING") {
      const msg = h.sap_docnum
        ? `Posted to SAP · DocNum ${h.sap_docnum}`
        : (h.message || "Posted to SAP");
      toast(action === "update" ? (h.message || "SAP order updated") : msg);
      try { await openPo(po); } catch (_) {}
      await refreshOrders();
      await refreshHome();
      await refreshNotices();
      return h;
    }
    if (sap.includes("FAIL") || (h.last_error && sap !== "POSTING")) {
      const err = h.last_error || "SAP operation failed";
      toast(err, true);
      try { await openPo(po); } catch (_) {}
      await refreshOrders();
      throw new Error(err);
    }
  }
  const timed = "SAP is still working — refresh this PO in a minute. If it stays on Posting, click Approve again.";
  toast(timed, true);
  try { await openPo(po); } catch (_) {}
  throw new Error(timed);
}


let confirmResolve = null;

function closeConfirmDialog(result) {
  $("confirm-modal")?.classList.add("hidden");
  if (confirmResolve) {
    confirmResolve(result);
    confirmResolve = null;
  }
}

function confirmDialog({
  title = "Are you sure?",
  message = "This action cannot be undone.",
  confirmText = "Delete",
  cancelText = "Cancel",
  danger = true,
} = {}) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $("confirm-title").textContent = title;
    $("confirm-message").textContent = message;
    $("confirm-ok").textContent = confirmText;
    $("confirm-cancel").textContent = cancelText;
    const okBtn = $("confirm-ok");
    okBtn.classList.toggle("danger", danger);
    okBtn.classList.toggle("primary", !danger);
    $("confirm-modal").classList.remove("hidden");
    $("confirm-cancel").focus();
  });
}

let fetchPollTimer = null;
let fetchHardCapTimer = null;
let fetchDone = false;
const FETCH_HARD_CAP_MS = 120000;
const FETCH_POLL_MS = 3000;
const FETCH_STABLE_POLLS = 2;
const FETCH_MIN_ELAPSED_MS = 12000;

function initSimpleRain(container, total = 16) {
  if (!container || container.dataset.rainReady) return;
  container.dataset.rainReady = "1";
  const burstCount = 4;
  const passCount = total - burstCount;

  for (let i = 0; i < passCount; i++) {
    const drop = document.createElement("span");
    drop.className = "rain-drop";
    const slot = (i + Math.random() * 0.9) / passCount;
    drop.style.left = `${Math.max(2, Math.min(98, slot * 100))}%`;
    drop.style.setProperty("--size", `${3 + Math.random() * 2.5}px`);
    drop.style.setProperty("--spd", `${6 + Math.random() * 5}s`);
    drop.style.setProperty("--delay", `${Math.random() * 11}s`);
    drop.style.setProperty("--op", `${0.42 + Math.random() * 0.3}`);
    drop.style.setProperty("--fall-from", `${-8 - Math.random() * 92}vh`);
    drop.innerHTML = '<i class="rain-drop-body"></i>';
    container.appendChild(drop);
  }

  for (let i = 0; i < burstCount; i++) {
    const drop = document.createElement("span");
    drop.className = "rain-drop rain-drop-burst";
    drop.style.left = `${44 + Math.random() * 12}%`;
    drop.style.setProperty("--size", `${3.5 + Math.random() * 2}px`);
    drop.style.setProperty("--spd", `${4.5 + Math.random() * 2.5}s`);
    drop.style.setProperty("--delay", `${i * 2.2 + Math.random() * 1.5}s`);
    drop.style.setProperty("--op", `${0.62 + Math.random() * 0.28}`);
    drop.style.setProperty("--land-y", `${47 + Math.random() * 6}vh`);
    drop.innerHTML = '<i class="rain-drop-body"></i><i class="rain-splash"></i><i class="rain-burst-ring"></i>';
    container.appendChild(drop);
  }
}

async function fetchChannelSnapshot() {
  if (!channelLive()) {
    return { orderTotal: 0, latestNoteId: 0, notificationCount: 0, noteSignature: "", latestPoAt: "" };
  }
  const [orders, notes] = await Promise.all([
    api(`/v1/b2b/orders?channel=${encodeURIComponent(currentChannel)}&limit=1&sort_by=created_at&sort_dir=desc`),
    api(q("/v1/b2b/notifications")),
  ]);
  const noteItems = notes.items || [];
  const orderItems = orders.items || [];
  return {
    orderTotal: Number(orders.total || 0),
    latestPoAt: String(orderItems[0]?.created_at || ""),
    latestNoteId: noteItems.reduce((m, n) => Math.max(m, Number(n.id) || 0), 0),
    notificationCount: noteItems.length,
    noteSignature: noteItems.map((n) => `${n.id}:${n.created_at || ""}:${n.po_number || ""}:${n.title || ""}`).join("|"),
  };
}

function fetchSnapshotChanged(before, after) {
  if (!before || !after) return false;
  if (after.orderTotal > before.orderTotal) return true;
  if (after.latestNoteId > before.latestNoteId) return true;
  if (after.notificationCount > before.notificationCount) return true;
  if (after.noteSignature !== before.noteSignature) return true;
  if (after.latestPoAt && after.latestPoAt > (before.latestPoAt || "")) return true;
  return false;
}

function snapshotsEqual(a, b) {
  if (!a || !b) return false;
  return a.orderTotal === b.orderTotal
    && a.latestNoteId === b.latestNoteId
    && a.notificationCount === b.notificationCount
    && a.noteSignature === b.noteSignature
    && a.latestPoAt === b.latestPoAt;
}

function stopFetchWait(message = "") {
  const el = $("fetch-wait");
  el?.classList.remove("done");
  el?.classList.add("hidden");
  document.querySelector(".topbar")?.classList.remove("fetch-active");
  $("fetch-btn")?.removeAttribute("disabled");
  if (fetchPollTimer) {
    clearTimeout(fetchPollTimer);
    fetchPollTimer = null;
  }
  if (fetchHardCapTimer) {
    clearTimeout(fetchHardCapTimer);
    fetchHardCapTimer = null;
  }
  if (message) toast(message);
}

function startFetchWait(label) {
  stopFetchWait();
  fetchDone = false;
  $("fetch-wait-title").textContent = `Checking ${label} mailbox…`;
  $("fetch-wait-msg").textContent = "Please wait — do not click Fetch again.";
  const bar = document.querySelector(".fetch-progress-bar");
  if (bar) {
    bar.style.animation = "none";
    bar.style.marginLeft = "";
    bar.style.width = "";
    void bar.offsetWidth;
    bar.style.animation = "";
  }
  $("fetch-wait")?.classList.remove("hidden", "done");
  document.querySelector(".topbar")?.classList.add("fetch-active");
  $("fetch-btn")?.setAttribute("disabled", "disabled");
}

function pollFetchUntilDone(baseline, label) {
  const started = Date.now();
  let stablePolls = 0;
  let lastSnap = null;

  const finish = (message, showToast = true) => {
    if (fetchDone) return;
    fetchDone = true;
    if (fetchPollTimer) {
      clearTimeout(fetchPollTimer);
      fetchPollTimer = null;
    }
    if (fetchHardCapTimer) {
      clearTimeout(fetchHardCapTimer);
      fetchHardCapTimer = null;
    }
    const el = $("fetch-wait");
    if (el && !el.classList.contains("hidden")) {
      el.classList.add("done");
      setTimeout(() => stopFetchWait(showToast ? message : ""), 350);
    } else {
      stopFetchWait(showToast ? message : "");
    }
  };

  const poll = async () => {
    if ($("fetch-wait")?.classList.contains("hidden")) return;
    try {
      await refreshHome();
      await refreshOrders();
      await refreshInbox();
      await refreshNotices();
      const snap = await fetchChannelSnapshot();
      if (fetchSnapshotChanged(baseline, snap)) {
        finish(`${label} mailbox updated.`);
        return;
      }
      if (lastSnap && snapshotsEqual(lastSnap, snap)) stablePolls += 1;
      else stablePolls = 0;
      lastSnap = snap;
      const elapsed = Date.now() - started;
      if (stablePolls >= FETCH_STABLE_POLLS && elapsed >= FETCH_MIN_ELAPSED_MS) {
        finish("", false);
        return;
      }
      if (elapsed >= FETCH_HARD_CAP_MS) {
        finish("", false);
        return;
      }
      fetchPollTimer = setTimeout(poll, FETCH_POLL_MS);
    } catch (e) {
      finish(e.message || "Fetch refresh failed");
    }
  };

  fetchPollTimer = setTimeout(poll, 4000);
  fetchHardCapTimer = setTimeout(() => {
    finish("", false);
  }, FETCH_HARD_CAP_MS);
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
  if (!res.ok) {
    const raw = body.error || body.message || `${res.status} ${res.statusText || "request failed"}`;
    const msg = /internal server error/i.test(String(raw))
      ? "Server error while talking to SAP (often a timeout). Try again — if it keeps failing, check CloudWatch logs."
      : raw;
    throw new Error(msg);
  }
  return body;
}

function showLogin(msg) {
  $("app-shell").classList.add("hidden");
  $("login-screen").classList.remove("hidden");
  initSimpleRain(document.querySelector(".login-rain"), 16);
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
    posting: "Posting to SAP…",
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
function isValidationBlockError(text) {
  return /GST mismatch|Missing tax code|No mapping found|No sufficiently similar address|Ship-to|item master|PO expiry date|Doc due date|delivery address could not be read|GST number could not be read|No line items|PO number mismatch|quantities are zero/i.test(String(text || ""));
}
function sapResponse(r) {
  let raw = r?.sap_response;
  if (!raw) return null;
  if (typeof raw === "string") {
    try { raw = JSON.parse(raw); } catch { return { message: raw }; }
  }
  return raw;
}
function isSapPostError(text, r) {
  if (isValidationBlockError(text)) return false;
  if (String(r?.sap_status || "").toUpperCase().includes("FAIL")) return true;
  return /SSM |sap_b1_url|companydb|Service Layer|Create Order|Update Order|salesperson code/i.test(String(text || ""));
}
function shownStatus(r) {
  if (isSapPosting(r)) return "posting";
  if (r?.pipeline_status === "failed") return "failed";
  if (r?.last_error && (r.pipeline_status === "parsed" || r.pipeline_status === "ready") && !isSapPostError(r.last_error, r)) return "needs_review";
  return r?.pipeline_status || "";
}
function canApproveRow(r) {
  if (!canEditOrders() || !canPost()) return false;
  if (isSapPosting(r)) return false;
  if (["posted", "failed", "rejected", "cancelled"].includes(r?.pipeline_status)) return false;
  if (["needs_review", "parsed"].includes(r?.pipeline_status)) return false;
  if (isValidationBlockError(r?.last_error)) return false;
  if (hasIssue(r) && !isSapPostError(r?.last_error, r)) return false;
  if (r?.pipeline_status === "ready" && !hasIssue(r)) return true;
  if (isSapPostError(r?.last_error, r)) return true;
  return false;
}
function canRejectRow(r) {
  if (!canEditOrders()) return false;
  if (isSapPosting(r)) return false;
  return !["posted", "failed", "rejected", "cancelled"].includes(r?.pipeline_status);
}
function canSubmitRow(r) {
  return canApproveRow(r);
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
  return canEditOrders() && canPost() && isSapOpen(r) && !isSapPosting(r);
}
function canCancelSapRow(r) {
  return canEditOrders() && canPost() && isSapOpen(r) && !isSapPosting(r);
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
    <p class="eyebrow">SAP</p>
    <p><b>DocNum</b> ${esc(live.DocNum ?? "—")} · <b>DocEntry</b> ${esc(live.DocEntry ?? "—")}</p>
    <p><b>Status</b> ${esc(soStatusLabel(live.DocumentStatus))} · <b>Doc due</b> ${esc(live.DocDueDate || "—")} · <b>Date</b> ${esc(live.DocDate || "—")} · <b>Value</b> ${esc(money(live.DocTotal))}</p>
    <pre>${esc(JSON.stringify(live, null, 2))}</pre>
  </div>`;
}
function errorBits(text) {
  return String(text || "").split(/(?<=\.)\s+/).map((s) => s.trim()).filter(Boolean);
}
function poSourceLabel(ch = currentChannel) {
  if (ch === "bigbasket") return "the Excel file";
  if (ch === "zepto") return "the CSV file";
  return "the PDF";
}
function friendlyError(text) {
  const fromDoc = poSourceLabel();
  return errorBits(text).map((b) => {
    if (/No sufficiently similar address/i.test(b)) return `Ship-to address from ${fromDoc} is not in the address map.`;
    if (/delivery address could not be read/i.test(b)) return `Ship-to / delivery address could not be read from ${fromDoc}. Add it in the Ship-to map or fix the source file.`;
    if (/PO expiry date|Doc due date/i.test(b)) return `PO expiry date (SAP Doc due date) could not be read from ${fromDoc}.`;
    if (/GST number could not be read/i.test(b)) return `GST number could not be read from ${fromDoc}.`;
    if (/PO number mismatch/i.test(b)) return b;
    if (/No line items/i.test(b)) return b;
    if (/quantities are zero/i.test(b)) return b;
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
function lineCard(l, i, editable, deletable = false) {
  const dis = editable ? "" : "disabled";
  const delBtn = deletable
    ? `<button class="btn danger line-del" type="button" title="Remove this line">Delete line</button>`
    : "";
  const head = `<div class="line-card-head"><span class="muted">Line ${i + 1}</span>${delBtn}</div>`;
  return `<div class="line-card">${head}${LINE_FIELDS.map(([f, label]) => {
    const val = l[f] ?? "";
    return `<label>${label}<input data-f="${f}" data-i="${i}" value="${esc(val)}" ${dis} /></label>`;
  }).join("")}</div>`;
}

function snapshotPoHeaderFromDom() {
  return {
    po_expiry_date: String($("e-due")?.value ?? "").trim(),
    delivered_to: String($("e-delivered")?.value ?? "").trim(),
    matched_address_name: String($("e-ship")?.value ?? "").trim(),
    gst_no: String($("e-gst")?.value ?? "").trim(),
    bill_to: String($("e-bill")?.value ?? "").trim(),
  };
}

function snapshotPoLineRow(l) {
  const row = {};
  LINE_FIELDS.forEach(([f]) => { row[f] = String(l?.[f] ?? "").trim(); });
  return row;
}

function snapshotPoLinesFromDom() {
  return [...document.querySelectorAll("#po-lines .line-card")].map((card) => {
    const row = {};
    LINE_FIELDS.forEach(([f]) => {
      row[f] = String(card.querySelector(`[data-f="${f}"]`)?.value ?? "").trim();
    });
    return row;
  });
}

function setPoDrawerBaseline(po, h, lines, due) {
  const shownLines = lines.length ? lines : [{}];
  poDrawerBaseline = {
    po,
    header: {
      po_expiry_date: String(due ?? h.po_expiry_date ?? "").trim().slice(0, 10),
      delivered_to: String(h.delivered_to ?? "").trim(),
      matched_address_name: String(h.matched_address_name ?? "").trim(),
      gst_no: String(h.gst_no ?? "").trim(),
      bill_to: String(h.bill_to ?? "").trim(),
    },
    lines: shownLines.map((l) => snapshotPoLineRow(l)),
  };
}

function isPoDrawerDirty() {
  if (!poDrawerBaseline || $("drawer")?.classList.contains("hidden")) return false;
  const headerSame = JSON.stringify(snapshotPoHeaderFromDom()) === JSON.stringify(poDrawerBaseline.header);
  const linesSame = JSON.stringify(snapshotPoLinesFromDom()) === JSON.stringify(poDrawerBaseline.lines);
  return !(headerSame && linesSame);
}

function requirePoSavedForPo(po, actionPhrase) {
  if (!poDrawerBaseline || poDrawerBaseline.po !== po || $("drawer")?.classList.contains("hidden")) return true;
  if (!isPoDrawerDirty()) return true;
  toast(`You have unsaved changes. Click Edit / Save before you ${actionPhrase} this order.`, true);
  return false;
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

const PO_AUDIT_LABELS = {
  po_created: "PO received from mail",
  edit_po: "Edited & saved",
  rematch_po: "Refetched",
  approve_post: "Approved & posted to SAP",
  sap_create: "Created in SAP",
  sap_update: "Updated in SAP",
  sap_link: "Linked to existing SAP order",
  sap_cancel: "Cancelled in SAP",
  sap_post_failed: "SAP post failed",
  sap_update_failed: "SAP update failed",
  sap_cancel_failed: "SAP cancel failed",
  reject: "Rejected",
  delete_po: "Deleted",
  amend_archive: "Previous version archived (amend)",
};

function auditActionLabel(action) {
  return PO_AUDIT_LABELS[action] || String(action || "").replace(/_/g, " ");
}

function buildPoActivityLog(header, rows) {
  const events = [...(rows || [])];
  if (header?.created_at) {
    const detail = [header.email_subject, header.email_uid ? `uid ${header.email_uid}` : ""].filter(Boolean).join(" · ");
    events.push({
      created_at: header.created_at,
      actor: "mailbox",
      action: "po_created",
      detail: detail || "Fetched from channel inbox",
    });
  }
  events.sort((a, b) => {
    const ta = a.created_at ? new Date(a.created_at).getTime() : 0;
    const tb = b.created_at ? new Date(b.created_at).getTime() : 0;
    return (Number.isNaN(tb) ? 0 : tb) - (Number.isNaN(ta) ? 0 : ta);
  });
  if (!events.length) return "<p class='muted'>No activity recorded for this PO yet.</p>";
  return `<ul class="po-activity-list">${events.map((r) => {
    const when = r.created_at ? new Date(r.created_at).toLocaleString() : "";
    const who = esc(r.actor || "—");
    const what = esc(auditActionLabel(r.action));
    const detail = esc(r.detail || "");
    return `<li class="po-activity-item">
      <div class="po-activity-when">${esc(when)}</div>
      <div class="po-activity-body"><strong>${what}</strong> · ${who}${detail ? `<span class="po-activity-detail">${detail}</span>` : ""}</div>
    </li>`;
  }).join("")}</ul>`;
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
function channelCanFetch() {
  const configured = me.fetch_channels || [currentChannel === "blinkit" ? "blinkit" : ""].filter(Boolean);
  return configured.includes(currentChannel);
}
function updateFetchButton() {
  const meta = CHANNELS.find((c) => c.id === currentChannel);
  const btn = $("fetch-btn");
  if (!btn) return;
  const show = Boolean(currentChannel) && channelCanFetch();
  btn.classList.toggle("hidden", !show);
  btn.textContent = meta ? `↻ Fetch ${meta.label}` : "↻ Fetch";
  btn.title = meta ? `Fetch ${meta.label} mailbox` : "Fetch mailbox";
}

function navCountHtml(ch, key) {
  const n = navCounts[ch]?.[key];
  if (n == null) return `<span class="nav-count" data-nav-count="${ch}:${key}"></span>`;
  return `<span class="nav-count" data-nav-count="${ch}:${key}">${n}</span>`;
}

function renderNav() {
  const allowed = myChannels();
  const channelsLabel = $("channels-nav-label");
  if (channelsLabel) channelsLabel.classList.toggle("hidden", !allowed.length);
  $("channel-nav").innerHTML = CHANNELS.filter((c) => allowed.includes(c.id)).map((c) => `
    <div class="ch-block ${currentChannel === c.id ? "open" : ""}" data-ch="${c.id}">
      <button class="ch-head ${currentChannel === c.id ? "open" : ""}" type="button" data-ch-toggle="${c.id}">
        <span class="dot ${c.id}"></span>${c.label}
      </button>
      <div class="ch-sub">
        ${canViewOrders() ? `<button class="nav-btn" data-view="home" data-ch="${c.id}">Overview</button>
        <button class="nav-btn" data-view="orders" data-ch="${c.id}"><span>Purchase orders</span>${navCountHtml(c.id, "orders")}</button>
        <button class="nav-btn" data-view="inbox" data-ch="${c.id}"><span>Mailbox</span>${navCountHtml(c.id, "inbox")}</button>` : ""}
        ${canViewSkuMap() ? `<button class="nav-btn" data-view="maps" data-ch="${c.id}"><span>Item map</span>${navCountHtml(c.id, "maps")}</button>` : ""}
        ${canViewShipTo() ? `<button class="nav-btn" data-view="addresses" data-ch="${c.id}"><span>Ship-to</span>${navCountHtml(c.id, "addresses")}</button>` : ""}
      </div>
    </div>
  `).join("");
  if (typeof renderLogisticsNav === "function") renderLogisticsNav();
  document.querySelectorAll("[data-view]").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === currentView && (b.dataset.ch ? b.dataset.ch === currentChannel : true));
  });
}

function applyNavCounts() {
  document.querySelectorAll("[data-nav-count]").forEach((el) => {
    const [ch, key] = String(el.dataset.navCount || "").split(":");
    const n = navCounts[ch]?.[key];
    el.textContent = n == null ? "" : String(n);
  });
}

async function refreshNavCounts(channel = currentChannel) {
  if (!CHANNELS.find((c) => c.id === channel)?.live) return;
  try {
    const stats = await api(`/v1/b2b/stats?channel=${encodeURIComponent(channel)}`);
    navCounts[channel] = {
      orders: stats.total_pos || 0,
      maps: stats.sku_map_count || 0,
      addresses: stats.ship_to_count || 0,
      inbox: stats.unread_notifications || 0,
    };
    applyNavCounts();
    if (channel === currentChannel) {
      if ($("sku-count")) $("sku-count").textContent = `${navCounts[channel].maps} mapping${navCounts[channel].maps === 1 ? "" : "s"}`;
      if ($("addr-count")) $("addr-count").textContent = `${navCounts[channel].addresses} alias${navCounts[channel].addresses === 1 ? "" : "es"}`;
    }
  } catch (_) { /* leave last known counts */ }
}

function setChannel(id) {
  if (!myChannels().includes(id)) return;
  stopFetchWait();
  currentChannel = id;
  orderPage = 0;
  const meta = CHANNELS.find((c) => c.id === id);
  $("channel-pill").className = `chip ${id}`;
  $("channel-pill").textContent = meta?.label || id;
  $("channel-soon").classList.toggle("hidden", channelLive());
  updateFetchButton();
  renderNav();
  refreshNavCounts(id);
}

function setView(name) {
  currentView = name;
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  const el = $(`view-${name}`);
  if (el) el.classList.remove("hidden");
  const titles = TITLES[name] || ["Khanal", ""];
  $("page-kicker").textContent = titles[0];
  $("page-title").textContent = titles[1];
  $("sidebar").classList.remove("open");
  renderNav();
}

async function refreshHome() {
  if (!channelLive()) {
    $("kpi-row").innerHTML = "";
    $("value-bars").innerHTML = "";
    statusChart?.destroy();
    statusChart = null;
    $("home-queue").innerHTML = `<p class="muted orders-empty">This channel is not live yet.</p>`;
    return;
  }
  const stats = await api(q("/v1/b2b/stats"));
  navCounts[currentChannel] = {
    orders: stats.total_pos || 0,
    maps: stats.sku_map_count || 0,
    addresses: stats.ship_to_count || 0,
    inbox: stats.unread_notifications || 0,
  };
  applyNavCounts();
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
  $("home-queue").innerHTML = items.map((r, i) => orderCard(r, i)).join("") || `<p class="muted orders-empty">Nothing waiting.</p>`;
  await refreshNotices();
}

function orderTone(r) {
  const status = shownStatus(r);
  if (status === "needs_review" || status === "failed" || hasIssue(r)) return "warn";
  if (status === "cancelled" || status === "rejected") return "muted";
  if (status === "posted" || status === "posted_open" || status === "ready" || status === "approved") return "ok";
  return "info";
}

function statusBadge(r) {
  const status = shownStatus(r);
  if (status === "posted") {
    const open = isSapOpen(r);
    const key = open ? "posted_open" : "posted_closed";
    const label = soStatusLabel(r.sap_document_status) || (open ? "Open" : "Closed");
    return `<span class="${statusClass(key)}">${esc(label === "—" ? (open ? "Open" : "Closed") : label)}</span>`;
  }
  return `<span class="${statusClass(status)}">${esc(statusLabel(status))}</span>`;
}

function orderCard(r, index = 0) {
  const err = r.last_error || "";
  const status = shownStatus(r);
  const title = displayPo(r.po_number);
  const due = docDueDate(r);
  const who = r.company_name || r.email_subject || "";
  const bits = friendlyError(err);
  const amend = String(r.duplicate_records_status || "").startsWith("Amended/") ? r.duplicate_records_status : "";
  const soLabel = String(r.sap_status || "").toUpperCase().includes("FAIL")
    ? "Post failed"
    : (status === "cancelled" ? "Cancelled" : soStatusLabel(r.sap_document_status));
  const tone = orderTone(r);
  return `<article class="po-card tone-${tone}${hasIssue(r) ? " has-error" : ""}" data-open="${esc(r.po_number)}">
    <div class="po-card-grid">
      <div class="col-num">${index + 1}</div>
      <div class="col-po">
        <div class="po-tags">
          ${due ? `<span class="chip-soft">Doc due ${esc(due)}</span>` : ""}
          ${amend ? `<button type="button" class="status s-needs_review amend-list-btn" data-act="amend-history" data-open="${esc(r.po_number)}">${esc(amend)} · History</button>` : ""}
          ${status === "posted" ? `<span class="${statusClass("posted")}">${esc(statusLabel("posted"))}</span>` : ""}
        </div>
        <strong class="po-no">${amend ? `<button type="button" class="link-btn po-amend-link" data-act="amend-history" data-open="${esc(r.po_number)}">` : ""}${esc(title)}${amend ? "</button>" : ""}</strong>
        ${who ? `<div class="po-company">${esc(who)}</div>` : ""}
        ${err ? `<button type="button" class="row-error" data-act="edit" data-open="${esc(r.po_number)}"><b>${esc(errorTitle(err))}</b>${bits.slice(0, 1).map((b) => `<span>${esc(b)}</span>`).join("")}</button>` : ""}
      </div>
      <div class="col-status">${statusBadge(r)}</div>
      <div class="col-so">${status === "cancelled" ? `<span class="${statusClass("cancelled")}">Cancelled</span>` : esc(soLabel)}</div>
      <div class="col-docnum">${r.sap_docnum ? `<span>${esc(r.sap_docnum)}</span>${r.sap_docentry ? `<span class="cell-sub">Entry ${esc(r.sap_docentry)}</span>` : ""}` : "—"}</div>
      <div class="col-amt">${esc(money(r.total_amount))}</div>
      <div class="col-items">${Number(r.total_items || 0)}</div>
      <div class="col-when">${esc(timeAgo(r.created_at) || "—")}</div>
    </div>
    <div class="po-card-actions" role="group" aria-label="Row actions">
      <div class="row-actions">
        <button class="btn ghost" type="button" data-act="preview" data-open="${esc(r.po_number)}">Preview</button>
        ${canEditOrders() && r.pipeline_status !== "posted" ? `<button class="btn ghost" type="button" data-act="edit" data-open="${esc(r.po_number)}">Edit</button>` : ""}
        ${canEditOrders() && r.pipeline_status !== "posted" ? `<button class="btn primary" type="button" data-act="refetch" data-open="${esc(r.po_number)}">Refetch</button>` : ""}
        ${canApproveRow(r) ? `<button class="btn primary" type="button" data-act="approve" data-open="${esc(r.po_number)}">Approve</button>` : ""}
        ${canRejectRow(r) ? `<button class="btn ghost danger-outline" type="button" data-act="reject" data-open="${esc(r.po_number)}">Reject</button>` : ""}
        ${canUpdateSapRow(r) ? `<button class="btn primary" type="button" data-act="update-sap" data-open="${esc(r.po_number)}">Update SAP</button>` : ""}
        ${canCancelSapRow(r) ? `<button class="btn danger" type="button" data-act="cancel-sap" data-open="${esc(r.po_number)}">Cancel SAP</button>` : ""}
        ${canDeleteOrders() && r.pipeline_status !== "posted" ? `<button class="btn danger" type="button" data-act="delete" data-open="${esc(r.po_number)}">Delete</button>` : ""}
      </div>
    </div>
  </article>`;
}

function renderOrdersPager() {
  const el = $("orders-pages");
  if (!el) return;
  const pages = Math.max(1, Math.ceil((orderTotal || 0) / orderPageSize));
  if (orderPage >= pages) orderPage = Math.max(0, pages - 1);
  if (orderTotal <= orderPageSize) {
    el.innerHTML = "";
    return;
  }
  const buttons = [];
  buttons.push(`<button type="button" class="page-btn" data-page="prev" ${orderPage <= 0 ? "disabled" : ""}>‹</button>`);
  const start = Math.max(0, Math.min(orderPage - 2, pages - 5));
  const end = Math.min(pages, start + 5);
  for (let i = start; i < end; i += 1) {
    buttons.push(`<button type="button" class="page-btn${i === orderPage ? " active" : ""}" data-page="${i}">${i + 1}</button>`);
  }
  buttons.push(`<button type="button" class="page-btn" data-page="next" ${orderPage >= pages - 1 ? "disabled" : ""}>›</button>`);
  el.innerHTML = buttons.join("");
}

async function refreshOrdersIssueBanner() {
  const banner = $("orders-issue-banner");
  const text = $("orders-issue-text");
  if (!banner || !text) return;
  if (!channelLive()) {
    banner.classList.add("hidden");
    return;
  }
  try {
    const [failed, review] = await Promise.all([
      api(`/v1/b2b/orders?channel=${encodeURIComponent(currentChannel)}&status=failed&limit=1`),
      api(`/v1/b2b/orders?channel=${encodeURIComponent(currentChannel)}&status=needs_review&limit=1`),
    ]);
    const failedN = Number(failed.total || 0);
    const reviewN = Number(review.total || 0);
    const total = failedN + reviewN;
    if (!total) {
      banner.classList.add("hidden");
      banner.dataset.filter = "";
      return;
    }
    const parts = [];
    if (failedN) parts.push(`${failedN} extract failed`);
    if (reviewN) parts.push(`${reviewN} need${reviewN === 1 ? "s" : ""} review`);
    text.textContent = `${parts.join(" · ")}. These stay on this list and are not sent to SAP until fixed.`;
    banner.dataset.filter = reviewN ? "needs_review" : "failed";
    const link = $("orders-show-issues");
    if (link) link.textContent = reviewN && failedN ? "Show needs review" : "Show them";
    banner.classList.remove("hidden");
  } catch (_) {
    banner.classList.add("hidden");
  }
}

async function refreshOrders() {
  if (!channelLive()) {
    $("orders-list").innerHTML = `<p class="muted orders-empty">This channel is not live yet.</p>`;
    orderTotal = 0;
    $("orders-count").textContent = "";
    $("orders-issue-banner")?.classList.add("hidden");
    renderOrdersPager();
    return;
  }
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
  params.set("limit", String(orderPageSize));
  params.set("offset", String(orderPage * orderPageSize));
  const [data] = await Promise.all([
    api(`/v1/b2b/orders?${params}`),
    refreshOrdersIssueBanner(),
  ]);
  const items = data.items || [];
  orderTotal = Number(data.total || items.length);
  $("orders-count").textContent = `${orderTotal} purchase order${orderTotal === 1 ? "" : "s"}`;
  const startNum = orderPage * orderPageSize;
  $("orders-list").innerHTML = items.map((r, i) => orderCard(r, startNum + i)).join("") || `<p class="muted orders-empty">No purchase orders yet.</p>`;
  if (!navCounts[currentChannel]) navCounts[currentChannel] = {};
  if (!$("status-filter").value && !$("po-search").value.trim()) {
    navCounts[currentChannel].orders = orderTotal;
    applyNavCounts();
  }
  updateOrderSortIndicators();
  renderOrdersPager();
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
  orderPage = 0;
}

function resetOrdersPage() {
  orderPage = 0;
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
  const deletable = canDeleteOrders() && !failed && !rejected && (!posted || sapOpen);
  const lines = data.lines || [];
  const err = h.last_error || "";
  const bits = friendlyError(err);
  const status = shownStatus(h);
  const sapFail = isSapPostError(err, h);
  const validationBlock = isValidationBlockError(err);
  const postResp = sapPostResponse(data.sap_post_response) || sapResponse(h);
  const blocked = failed || rejected || validationBlock || (Boolean(err) && !sapFail);
  const pdf = h.pdf_url || "";
  const xlsx = h.xlsx_url || h.excel_url || "";
  const pdfName = String(h.pdf_s3_key || "").split("/").pop() || "PO.pdf";
  const xlsxName = String(h.xlsx_s3_key || "").split("/").pop() || "PO.xlsx";
  const sapAmount = sapLive?.DocTotal ?? h.sap_doc_total ?? h.total_amount;
  const due = docDueDate(h, sapLive);
  const amendRows = data.amend_history || [];
  const activityLog = data.activity_log || [];
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
      ${canEditOrders() && !posted && !rejected && (validationBlock || !sapFail) ? `<p class="muted">After you update Item map or Ship-to, click Refetch to re-read this PO and apply the new maps and SAP tax.</p>
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
    <div id="po-lines">${(lines.length ? lines : [{}]).map((l, i) => lineCard(l, i, editable, deletable)).join("")}</div>
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
      ${canDeleteOrders() && h.pipeline_status !== "posted" ? `<button class="btn danger" id="delete-po" type="button">Delete</button>` : ""}
    </div>
    <div class="po-activity">
      <p class="section-h">Activity on this PO</p>
      <p class="muted">Who received, edited, refetched, approved, or posted this order.</p>
      ${buildPoActivityLog(h, activityLog)}
    </div>
  `;
  bindAmendHistoryActions(h.po_number, amendRows);
  $("drawer").classList.remove("hidden");
  const panel = document.querySelector("#drawer .drawer-panel");
  if ($("drawer-wide") && panel) {
    $("drawer-wide").textContent = panel.classList.contains("wide") ? "Narrow" : "Widen";
  }
  $("add-line")?.addEventListener("click", () => {
    const box = $("po-lines");
    const i = box.querySelectorAll(".line-card").length;
    box.insertAdjacentHTML("beforeend", lineCard({}, i, true, canDeleteOrders()));
  });
  $("save-po")?.addEventListener("click", () => withBusy($("save-po"), () => savePo(h.po_number, lines)).catch((e) => toast(e.message, true)));
  $("preview-po")?.addEventListener("click", () => withBusy($("preview-po"), () => previewPo(h.po_number, "sap")).catch((e) => toast(e.message, true)));
  $("sap-payload")?.addEventListener("click", () => withBusy($("sap-payload"), () => previewPo(h.po_number, "sap")).catch((e) => toast(e.message, true)));
  $("raw-payload")?.addEventListener("click", () => withBusy($("raw-payload"), () => previewPo(h.po_number, "raw")).catch((e) => toast(e.message, true)));
  $("refetch-po")?.addEventListener("click", () => withBusy($("refetch-po"), () => rematchPo(h.po_number)).catch((e) => toast(e.message, true)));
  $("approve-po")?.addEventListener("click", () => {
    if (!requirePoSavedForPo(h.po_number, "approve")) return;
    withBusy($("approve-po"), () => act("/v1/b2b/approve", h.po_number)).catch((e) => toast(e.message, true));
  });
  $("reject-po")?.addEventListener("click", () => withBusy($("reject-po"), () => rejectPo(h.po_number)).catch((e) => toast(e.message, true)));
  $("update-sap-po")?.addEventListener("click", () => {
    if (!requirePoSavedForPo(h.po_number, "update SAP")) return;
    withBusy($("update-sap-po"), () => updateSapPo(h.po_number)).catch((e) => toast(e.message, true));
  });
  $("cancel-sap-po")?.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Cancel SAP order",
      message: `Cancel SAP order for ${displayPo(h.po_number)}? This cannot be undone.`,
      confirmText: "Cancel SAP",
    });
    if (!ok) return;
    withBusy($("cancel-sap-po"), () => cancelSapPo(h.po_number)).catch((e) => toast(e.message, true));
  });
  $("delete-po")?.addEventListener("click", () => {
    withBusy($("delete-po"), () => deletePo(h.po_number)).catch((e) => toast(e.message, true));
  });
  setPoDrawerBaseline(h.po_number, h, lines, due);
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
      channel: currentChannel,
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

async function savePo(po, lines) {
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
  const isApprove = String(path || "").includes("approve");
  if (isApprove && !requirePoSavedForPo(po, "approve")) return;
  const out = await api(path, { method: "POST", body: JSON.stringify({ po_number: po, channel: currentChannel }) });
  if (out.accepted || out.status === "posting") {
    toast(out.message || "Posting to SAP — please wait…");
    await waitForSapPost(po, { action: "approve" });
    return out;
  }
  const failed = out.status === "error";
  toast(out.message || out.error || (failed ? "Failed" : "Done"), failed);
  try { await openPo(po); } catch (_) {}
  await refreshOrders();
  await refreshHome();
  await refreshInbox();
  await refreshNotices();
  if (failed) throw new Error(out.message || out.error || "Failed");
  return out;
}

async function rematchPo(po, { openDrawer = true } = {}) {
  const out = await api("/v1/b2b/order-rematch", { method: "POST", body: JSON.stringify({ po_number: po, channel: currentChannel }) });
  if (out.ok) toast(out.mail_moved ? "Refetched. PO is ready — mail moved to Processed." : "Refetched from stored PDF/Excel. Maps and SAP checks applied.");
  else toast(out.message || out.last_error || "Still blocked", true);
  const tasks = [refreshOrders(), refreshHome()];
  if (openDrawer) tasks.push(openPo(po));
  await Promise.all(tasks);
}

async function updateSapPo(po) {
  if (!requirePoSavedForPo(po, "update SAP")) return;
  const out = await api("/v1/b2b/update-sap", { method: "POST", body: JSON.stringify({ po_number: po, channel: currentChannel }) });
  if (out.accepted || out.status === "posting") {
    toast(out.message || "Updating SAP — please wait…");
    await waitForSapPost(po, { action: "update" });
    return out;
  }
  const failed = out.status === "error";
  toast(out.message || out.error || (failed ? "SAP update failed" : "SAP order updated"), failed);
  if (failed) throw new Error(out.message || out.error || "SAP update failed");
  try { await openPo(po); } catch (_) {}
  await refreshOrders();
  await refreshHome();
}

async function cancelSapPo(po) {
  const out = await api("/v1/b2b/cancel-sap", { method: "POST", body: JSON.stringify({ po_number: po, channel: currentChannel }) });
  if (out.accepted || out.status === "posting") {
    toast(out.message || "Cancelling SAP — please wait…");
    await waitForSapPost(po, { action: "cancel" });
    $("drawer").classList.add("hidden");
    await refreshOrders();
    await refreshHome();
    return out;
  }
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
async function deletePo(po) {
  const ok = await confirmDialog({
    title: "Delete purchase order",
    message: `Delete ${displayPo(po)}? This cannot be undone. Posted orders cannot be removed.`,
    confirmText: "Delete PO",
  });
  if (!ok) return;
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
  $("sku-import")?.classList.toggle("hidden", !canEditSkuMap());
  renderSkuTable();
  if (!navCounts[currentChannel]) navCounts[currentChannel] = {};
  navCounts[currentChannel].maps = skuRows.length;
  applyNavCounts();
}

function mapRowMatch(val, q) {
  if (!q) return true;
  return String(val || "").toLowerCase().includes(q.toLowerCase());
}

function mapCountLabel(n, unit, filtered, total, qActive) {
  const word = n === 1 ? unit : `${unit}s`;
  if (qActive && filtered !== total) return `${filtered} of ${total} ${word}`;
  return `${total} ${word}`;
}

function renderSkuTable() {
  const qItem = $("sku-search-item")?.value.trim() || "";
  const qSap = $("sku-search-sap")?.value.trim() || "";
  const filtered = skuRows.filter((r) => mapRowMatch(r.item_code, qItem) && mapRowMatch(r.sap_code, qSap));
  const total = skuRows.length;
  const qActive = Boolean(qItem || qSap);
  const emptyMsg = qActive ? "No mappings match your search." : "No item mappings yet. Add one above.";
  $("sku-body").innerHTML = filtered.map((r, i) => `<tr>
    <td class="col-num">${i + 1}</td><td>${esc(r.item_code)}</td><td>${esc(r.sap_code)}</td>
    <td class="row-actions">${canEditSkuMap() ? `<button class="btn ghost" type="button" data-map-act="edit" data-item="${esc(r.item_code)}">Edit</button>` : ""}${canDeleteSkuMap() ? `<button class="btn danger" type="button" data-map-act="delete" data-item="${esc(r.item_code)}">Delete</button>` : ""}</td>
  </tr>`).join("") || `<tr class="empty-row"><td colspan="4">${esc(emptyMsg)}</td></tr>`;
  if ($("sku-count")) $("sku-count").textContent = mapCountLabel(total, "mapping", filtered.length, total, qActive);
}

async function refreshAddresses() {
  if (!channelLive()) { $("addr-body").innerHTML = ""; return; }
  const data = await api(q("/v1/b2b/ship-to"));
  addrRows = data.items || [];
  $("addr-form").classList.toggle("hidden", !canEditShipTo());
  $("addr-import")?.classList.toggle("hidden", !canEditShipTo());
  renderAddrTable();
  if (!navCounts[currentChannel]) navCounts[currentChannel] = {};
  navCounts[currentChannel].addresses = addrRows.length;
  applyNavCounts();
}

function renderAddrTable() {
  const qId = $("addr-search-id")?.value.trim() || "";
  const qEntry = $("addr-search-entry")?.value.trim() || "";
  const filtered = addrRows.filter((r) => mapRowMatch(r.address_id, qId) && mapRowMatch(r.address_entry, qEntry));
  const total = addrRows.length;
  const qActive = Boolean(qId || qEntry);
  const emptyMsg = qActive ? "No aliases match your search." : "No ship-to aliases yet. Add one above.";
  $("addr-body").innerHTML = filtered.map((r, i) => `<tr>
    <td class="col-num">${i + 1}</td><td>${esc(r.address_id)}</td><td>${esc(r.address_entry)}</td>
    <td class="row-actions">${canEditShipTo() ? `<button class="btn ghost" type="button" data-addr-act="edit" data-id="${esc(r.address_id)}">Edit</button>` : ""}${canDeleteShipTo() ? `<button class="btn danger" type="button" data-addr-act="delete" data-id="${esc(r.address_id)}">Delete</button>` : ""}</td>
  </tr>`).join("") || `<tr class="empty-row"><td colspan="4">${esc(emptyMsg)}</td></tr>`;
  if ($("addr-count")) $("addr-count").textContent = mapCountLabel(total, "alias", filtered.length, total, qActive);
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
      ${n.po_number && canEditOrders() && !read ? `<button class="btn ghost" type="button" data-act="reject">Reject</button>` : ""}
      ${canEditOrders() ? `<button class="btn danger" type="button" data-act="delete">Delete</button>` : ""}
    </div>
  </div>`;
  }).join("") || "<p class='muted'>Inbox is clear.</p>";
  const unread = (data.items || []).filter((n) => !n.is_read).length;
  if (!navCounts[currentChannel]) navCounts[currentChannel] = {};
  navCounts[currentChannel].inbox = unread;
  applyNavCounts();
}

async function refreshNotices() {
  const marketplace = myChannels().filter((id) => CHANNELS.find((c) => c.id === id)?.live);
  const showB2b = canViewOrders() && marketplace.length > 0;
  const showDelivery = typeof canViewDelivery === "function" && canViewDelivery();

  let b2bItems = [];
  if (showB2b) {
    try {
      const qs = new URLSearchParams({ unread: "true", channels: marketplace.join(",") });
      const data = await api(`/v1/b2b/notifications?${qs}`);
      b2bItems = (data.items || []).filter((n) => n.kind === "error").slice(0, 12);
    } catch (_) {
      b2bItems = [];
    }
  }

  let deliveryItems = [];
  if (showDelivery && typeof deliveryApi === "function") {
    try {
      const alerts = await deliveryApi("/v1/delivery/alerts?limit=8");
      deliveryItems = alerts.alerts || [];
    } catch (_) {
      deliveryItems = [];
    }
  }

  const total = b2bItems.length + deliveryItems.length;
  $("nav-alerts").textContent = total ? `${total} alert${total === 1 ? "" : "s"}` : "";
  $("bell-count").textContent = total ? String(total) : "";
  $("bell-count").classList.toggle("hidden", !total);

  const parts = [];
  if (showB2b) {
    const label = marketplace.length === 1
      ? (CHANNELS.find((c) => c.id === marketplace[0])?.label || marketplace[0])
      : "Channels";
    parts.push(`<p class="bell-section-label">${esc(label)}</p>`);
    if (b2bItems.length) {
      parts.push(
        b2bItems
          .map((n) => {
            const ch = esc(n.channel || "");
            const chLabel = CHANNELS.find((c) => c.id === n.channel)?.label || n.channel || "";
            return `<div class="bell-item is-error" data-bell="b2b" data-po="${esc(n.po_number || "")}" data-nid="${esc(n.id || "")}" data-channel="${ch}">
    <strong>${esc(n.title || "Could not process mail")}</strong>
    <p class="bell-meta">${esc(chLabel)}${n.po_number ? ` · ${esc(n.po_number)}` : ""}</p>
    <p class="bell-body">${esc(n.body || "")}</p>
  </div>`;
          })
          .join("")
      );
    } else {
      parts.push('<p class="bell-empty">No channel errors</p>');
    }
  }

  if (showDelivery) {
    parts.push('<p class="bell-section-label">Logistics</p>');
    if (deliveryItems.length) {
      parts.push(
        deliveryItems
          .map(
            (a) => `<div class="bell-item is-error" data-bell="delivery" data-lr="${esc(a.lrnum || "")}">
    <strong>${esc(a.title || "Logistics")}</strong>
    <p class="bell-meta">LR ${esc(a.lrnum || "—")}</p>
    <p class="bell-body">${esc(a.message || "")}</p>
  </div>`
          )
          .join("")
      );
    } else {
      parts.push('<p class="bell-empty">No logistics issues</p>');
    }
  }

  if (!showB2b && !showDelivery) {
    parts.push('<p class="bell-empty">No alerts</p>');
  }

  $("bell-panel").innerHTML = parts.join("");
  $("notice-stack").innerHTML = "";
}

async function openAlert(po, nid, channel) {
  const ch = channel || currentChannel;
  if (nid) {
    await api("/v1/b2b/notifications/read", { method: "POST", body: JSON.stringify({ id: nid, channel: ch }) });
    await refreshNotices();
    if (typeof refreshInbox === "function" && ch === currentChannel) await refreshInbox();
  }
  if (!po) return;
  if (ch && myChannels().includes(ch) && ch !== currentChannel) setChannel(ch);
  setView("orders");
  await openPo(po);
}

async function openDeliveryBellAlert(lr) {
  if (!lr) return;
  if (typeof setView === "function") setView("logistics-tracker");
  if ($("ld-f-lrnum")) $("ld-f-lrnum").value = lr;
  if (typeof loadLogisticsView === "function") await loadLogisticsView("logistics-tracker");
  else if (typeof refreshLogisticsTracker === "function") await refreshLogisticsTracker(1);
}

function pickedChannels() {
  return [...document.querySelectorAll("#user-channel-picks input:checked")].map((el) => el.value);
}

async function refreshUsers() {
  const data = await api("/v1/b2b/users");
  const self = (me.email || "").toLowerCase();
  teamUsers = data.items || [];
  $("user-channel-picks").innerHTML = [
    ...CHANNELS.map((c) => `<label><input type="checkbox" value="${c.id}" ${c.id === "blinkit" ? "checked" : ""} /> ${c.label}</label>`),
    `<label><input type="checkbox" value="logistics" /> Delivery</label>`,
  ].join("");
  $("user-body").innerHTML = teamUsers.map((u) => {
    const mine = String(u.email || "").toLowerCase() === self;
    const on = Boolean(u.is_active);
    const ch = (u.channels || []).map((c) => (c === "logistics" ? "Delivery" : c)).join(", ");
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
  if ($("perm-delivery")) $("perm-delivery").value = u.perm_delivery || "";
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
  if (name.startsWith("logistics-") && !canViewDelivery()) { toast("No access to logistics delivery", true); return; }
  setView(name);
  if (name === "home") await refreshHome();
  if (name === "orders") await refreshOrders();
  if (name === "maps") await refreshMaps();
  if (name === "addresses") await refreshAddresses();
  if (name === "inbox") await refreshInbox();
  if (name === "users") await refreshUsers();
  if (name === "logs") await refreshLogs();
  if (name.startsWith("logistics-") && typeof loadLogisticsView === "function") await loadLogisticsView(name);
}

async function boot() {
  me = await api("/v1/b2b/me");
  $("who").innerHTML = `<strong>${esc(me.full_name || me.email)}</strong><small>${esc(me.role || "viewer")}${me.sap_salesperson_code ? " · " + esc(me.sap_salesperson_code) : ""}</small>`;
  document.querySelectorAll("[data-admin]").forEach((el) => el.classList.toggle("hidden", !canAdmin()));
  const allowed = myChannels();
  if (allowed.length) {
    currentChannel = allowed.includes("blinkit") ? "blinkit" : allowed[0];
    setChannel(currentChannel);
  } else {
    currentChannel = "";
    const pill = $("channel-pill");
    if (pill) {
      pill.className = "chip";
      pill.textContent = canViewDelivery() ? "Logistics" : "—";
    }
    $("channel-soon")?.classList.add("hidden");
    updateFetchButton();
    renderNav();
  }
  const firstView = canViewOrders() ? "home"
    : canViewSkuMap() ? "maps"
    : canViewShipTo() ? "addresses"
    : canViewDelivery() ? "logistics-home"
    : "home";
  await loadView(firstView);
  const rest = allowed.filter((id) => id !== currentChannel && CHANNELS.find((c) => c.id === id)?.live);
  rest.forEach((id) => refreshNavCounts(id));
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
$("confirm-cancel")?.addEventListener("click", () => closeConfirmDialog(false));
$("confirm-ok")?.addEventListener("click", () => closeConfirmDialog(true));
$("confirm-modal")?.addEventListener("click", (e) => { if (e.target === $("confirm-modal")) closeConfirmDialog(false); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("confirm-modal").classList.contains("hidden")) closeConfirmDialog(false);
  else if (!$("password-modal").classList.contains("hidden")) $("password-modal").classList.add("hidden");
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
$("logistics-nav")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (!btn?.dataset.view?.startsWith("logistics-")) return;
  loadView(btn.dataset.view).catch((err) => toast(err.message, true));
});
$("status-filter").addEventListener("change", () => { resetOrdersPage(); refreshOrders().catch((e) => toast(e.message, true)); });
$("po-search").addEventListener("change", () => { resetOrdersPage(); refreshOrders().catch((e) => toast(e.message, true)); });
$("sap-docnum-filter")?.addEventListener("change", () => { resetOrdersPage(); refreshOrders().catch((e) => toast(e.message, true)); });
$("sap-docentry-filter")?.addEventListener("change", () => { resetOrdersPage(); refreshOrders().catch((e) => toast(e.message, true)); });
$("created-from-filter")?.addEventListener("change", () => { resetOrdersPage(); refreshOrders().catch((e) => toast(e.message, true)); });
$("created-to-filter")?.addEventListener("change", () => { resetOrdersPage(); refreshOrders().catch((e) => toast(e.message, true)); });
$("orders-clear-filters")?.addEventListener("click", () => {
  clearOrderFilters();
  refreshOrders().catch((e) => toast(e.message, true));
});
$("orders-show-issues")?.addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  const filter = $("orders-issue-banner")?.dataset.filter || "needs_review";
  $("status-filter").value = filter;
  resetOrdersPage();
  refreshOrders().catch((err) => toast(err.message, true));
});
$("orders-issue-banner")?.addEventListener("click", (e) => {
  if (e.target.closest("button")) return;
  $("orders-show-issues")?.click();
});
$("orders-head")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".th-sort");
  if (!btn?.dataset.sort) return;
  e.preventDefault();
  const col = btn.dataset.sort;
  if (orderSort.col === col) orderSort.dir = orderSort.dir === "asc" ? "desc" : "asc";
  else { orderSort.col = col; orderSort.dir = col === "created_at" ? "desc" : "asc"; }
  resetOrdersPage();
  refreshOrders().catch((err) => toast(err.message, true));
});
$("orders-page-size")?.addEventListener("change", () => {
  orderPageSize = Number($("orders-page-size").value) || 10;
  orderPage = 0;
  refreshOrders().catch((e) => toast(e.message, true));
});
$("orders-pages")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-page]");
  if (!btn || btn.disabled) return;
  const pages = Math.max(1, Math.ceil((orderTotal || 0) / orderPageSize));
  const raw = btn.dataset.page;
  if (raw === "prev") orderPage = Math.max(0, orderPage - 1);
  else if (raw === "next") orderPage = Math.min(pages - 1, orderPage + 1);
  else orderPage = Number(raw) || 0;
  refreshOrders().catch((err) => toast(err.message, true));
});
async function handlePoAction(e) {
  const btn = e.target.closest("[data-act]");
  const po = (btn && btn.dataset.open) || e.target.closest("[data-open]")?.dataset.open;
  if (!po) return;
  e.preventDefault();
  const kind = btn?.dataset.act || "edit";
  const work = async () => {
    if (kind === "preview") await previewPo(po);
    else if (kind === "refetch" || kind === "fix") {
      const openDrawer = kind === "fix" || !$("drawer").classList.contains("hidden");
      await rematchPo(po, { openDrawer });
    }
    else if (kind === "approve") {
      if (!requirePoSavedForPo(po, "approve")) return;
      await act("/v1/b2b/approve", po);
    }
    else if (kind === "reject") await rejectPo(po);
    else if (kind === "update-sap") await updateSapPo(po);
    else if (kind === "cancel-sap") {
      const ok = await confirmDialog({
        title: "Cancel SAP order",
        message: `Cancel SAP order for ${displayPo(po)}? This cannot be undone.`,
        confirmText: "Cancel SAP",
      });
      if (!ok) return;
      await cancelSapPo(po);
    }
    else if (kind === "submit") {
      if (!requirePoSavedForPo(po, "approve")) return;
      await act("/v1/b2b/approve", po);
    }
    else if (kind === "delete") await deletePo(po);
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
$("drawer-close").addEventListener("click", () => {
  poDrawerBaseline = null;
  $("drawer").classList.add("hidden");
});
$("drawer-body")?.addEventListener("click", async (e) => {
  const btn = e.target.closest(".line-del");
  if (!btn) return;
  if (!canDeleteOrders()) {
    toast("You do not have delete permission for purchase orders", true);
    return;
  }
  const card = btn.closest(".line-card");
  if (!card) return;
  const box = $("po-lines");
  if (box.querySelectorAll(".line-card").length <= 1) {
    toast("At least one line item is required", true);
    return;
  }
  const desc = card.querySelector('[data-f="product_description"]')?.value?.trim()
    || card.querySelector('[data-f="sap_code"]')?.value?.trim()
    || card.querySelector('[data-f="item_code"]')?.value?.trim()
    || `Line ${[...box.querySelectorAll(".line-card")].indexOf(card) + 1}`;
  const ok = await confirmDialog({
    title: "Delete line item",
    message: `Remove "${desc}" from this PO? Click Edit / Save to persist the change.`,
    confirmText: "Delete line",
  });
  if (!ok) return;
  card.remove();
});
$("drawer-wide")?.addEventListener("click", () => {
  const panel = document.querySelector("#drawer .drawer-panel");
  panel?.classList.toggle("wide");
  $("drawer-wide").textContent = panel?.classList.contains("wide") ? "Narrow" : "Widen";
});
$("drawer").addEventListener("click", (e) => {
  if (e.target === $("drawer")) {
    poDrawerBaseline = null;
    $("drawer").classList.add("hidden");
  }
});
$("inbox-list").addEventListener("click", (e) => {
  const item = e.target.closest("[data-nid]");
  if (!item) return;
  const btn = e.target.closest("[data-act]");
  const kind = btn?.dataset.act;
  const nid = item.dataset.nid;
  const po = item.dataset.po;
  const work = async () => {
    if (kind === "preview") return previewPo(po);
    if (kind === "approve") {
      if (!requirePoSavedForPo(po, "approve")) return;
      return act("/v1/b2b/approve", po);
    }
    if (kind === "reject") return rejectPo(po);
    if (kind === "submit") {
      if (!requirePoSavedForPo(po, "approve")) return;
      return act("/v1/b2b/approve", po);
    }
    if (kind === "read") {
      await api("/v1/b2b/notifications/read", { method: "POST", body: JSON.stringify({ id: Number(nid), channel: currentChannel }) });
      await refreshInbox();
      await refreshNotices();
      toast("Marked as read");
      return;
    }
    if (kind === "delete") {
      const ok = await confirmDialog({
        title: "Delete alert",
        message: "Delete this mailbox alert?",
        confirmText: "Delete alert",
      });
      if (!ok) return;
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
  const delivery = e.target.closest("[data-bell='delivery']");
  if (delivery) {
    $("bell-panel").classList.add("hidden");
    openDeliveryBellAlert(delivery.dataset.lr).catch((err) => toast(err.message, true));
    return;
  }
  const item = e.target.closest("[data-bell='b2b']");
  if (!item) return;
  $("bell-panel").classList.add("hidden");
  openAlert(item.dataset.po, item.dataset.nid, item.dataset.channel).catch((err) => toast(err.message, true));
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
  if (!channelCanFetch()) {
    toast("Mail fetch is not configured for this channel yet.", true);
    return;
  }
  const label = CHANNELS.find((c) => c.id === currentChannel)?.label || currentChannel;
  let baseline = null;
  try {
    baseline = await fetchChannelSnapshot();
    startFetchWait(label);
    await api("/v1/b2b/fetch", {
      method: "POST",
      body: JSON.stringify({ channel: currentChannel }),
    });
    pollFetchUntilDone(baseline, label);
  } catch (e) {
    stopFetchWait();
    toast(e.message, true);
  }
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
$("sku-search-item")?.addEventListener("input", () => renderSkuTable());
$("sku-search-sap")?.addEventListener("input", () => renderSkuTable());
$("sku-search-clear")?.addEventListener("click", () => {
  if ($("sku-search-item")) $("sku-search-item").value = "";
  if ($("sku-search-sap")) $("sku-search-sap").value = "";
  renderSkuTable();
});
function csvEscape(value) {
  const text = String(value ?? "");
  if (/[",\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}
function downloadCsv(filename, headers, rows) {
  const lines = [headers.join(",")];
  for (const row of rows) lines.push(headers.map((h) => csvEscape(row[h])).join(","));
  const blob = new Blob([`\ufeff${lines.join("\n")}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  const src = String(text || "").replace(/^\ufeff/, "");
  for (let i = 0; i < src.length; i += 1) {
    const ch = src[i];
    if (quoted) {
      if (ch === '"' && src[i + 1] === '"') { cell += '"'; i += 1; }
      else if (ch === '"') quoted = false;
      else cell += ch;
      continue;
    }
    if (ch === '"') quoted = true;
    else if (ch === ",") { row.push(cell); cell = ""; }
    else if (ch === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
    else if (ch !== "\r") cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  if (!rows.length) return [];
  const headers = rows[0].map((h) => String(h || "").trim().toLowerCase());
  return rows.slice(1).map((vals) => {
    const rec = {};
    headers.forEach((h, idx) => { if (h) rec[h] = String(vals[idx] || "").trim(); });
    return rec;
  }).filter((rec) => Object.values(rec).some(Boolean));
}
function readCsvFile(input) {
  return new Promise((resolve, reject) => {
    const file = input.files && input.files[0];
    if (!file) return reject(new Error("Choose a CSV file"));
    const reader = new FileReader();
    reader.onload = () => resolve(parseCsv(String(reader.result || "")));
    reader.onerror = () => reject(new Error("Could not read that file"));
    reader.readAsText(file);
  });
}
$("sku-template")?.addEventListener("click", () => {
  downloadCsv(`${currentChannel}-item-map-template.csv`, ["item_code", "sap_code"], []);
});
$("sku-export")?.addEventListener("click", () => {
  downloadCsv(`${currentChannel}-item-map.csv`, ["item_code", "sap_code"], skuRows);
});
$("sku-import")?.addEventListener("click", () => $("sku-import-file")?.click());
$("sku-import-file")?.addEventListener("change", async (e) => {
  const input = e.target;
  try {
    await withBusy($("sku-import"), async () => {
      const parsed = await readCsvFile(input);
      const items = parsed.map((r) => ({
        item_code: r.item_code || r["channel item"] || r.sku || "",
        sap_code: r.sap_code || r["sap item"] || r.sap || "",
      })).filter((r) => r.item_code && r.sap_code);
      if (!items.length) throw new Error("CSV needs item_code and sap_code columns");
      let count = 0;
      try {
        const result = await api("/v1/b2b/sku-map/import", { method: "POST", body: JSON.stringify({ channel: currentChannel, items }) });
        count = result.count || items.length;
      } catch {
        for (const item of items) {
          await api("/v1/b2b/sku-map", { method: "PUT", body: JSON.stringify({ ...item, channel: currentChannel }) });
          count += 1;
        }
      }
      toast(`Imported ${count} item map rows`);
      await refreshMaps();
    });
  } catch (err) { toast(err.message, true); }
  input.value = "";
});
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
    const ok = await confirmDialog({
      title: "Delete item mapping",
      message: `Delete mapping for ${item}?`,
      confirmText: "Delete mapping",
    });
    if (!ok) return;
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
$("addr-search-id")?.addEventListener("input", () => renderAddrTable());
$("addr-search-entry")?.addEventListener("input", () => renderAddrTable());
$("addr-search-clear")?.addEventListener("click", () => {
  if ($("addr-search-id")) $("addr-search-id").value = "";
  if ($("addr-search-entry")) $("addr-search-entry").value = "";
  renderAddrTable();
});
$("addr-template")?.addEventListener("click", () => {
  downloadCsv(`${currentChannel}-ship-to-template.csv`, ["address_id", "address_entry"], []);
});
$("addr-export")?.addEventListener("click", () => {
  downloadCsv(`${currentChannel}-ship-to.csv`, ["address_id", "address_entry"], addrRows);
});
$("addr-import")?.addEventListener("click", () => $("addr-import-file")?.click());
$("addr-import-file")?.addEventListener("change", async (e) => {
  const input = e.target;
  try {
    await withBusy($("addr-import"), async () => {
      const parsed = await readCsvFile(input);
      const items = parsed.map((r) => ({
        address_id: r.address_id || r["sap name"] || r.sap_name || "",
        address_entry: r.address_entry || r["po text"] || r.po_text || "",
      })).filter((r) => r.address_id && r.address_entry);
      if (!items.length) throw new Error("CSV needs address_id and address_entry columns");
      let count = 0;
      try {
        const result = await api("/v1/b2b/ship-to/import", { method: "POST", body: JSON.stringify({ channel: currentChannel, items }) });
        count = result.count || items.length;
      } catch {
        for (const item of items) {
          await api("/v1/b2b/ship-to", { method: "PUT", body: JSON.stringify({ ...item, channel: currentChannel }) });
          count += 1;
        }
      }
      toast(`Imported ${count} ship-to rows`);
      await refreshAddresses();
    });
  } catch (err) { toast(err.message, true); }
  input.value = "";
});
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
    const ok = await confirmDialog({
      title: "Delete ship-to alias",
      message: `Delete ship-to alias ${id}?`,
      confirmText: "Delete alias",
    });
    if (!ok) return;
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
    if (btn.dataset.userAct === "delete") {
      const ok = await confirmDialog({
        title: "Delete user",
        message: `Delete ${email}? They will not be able to sign in.`,
        confirmText: "Delete user",
      });
      if (!ok) return;
    }
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
      const channels = pickedChannels();
      if (!channels.length) {
        toast("Pick at least one channel (or Delivery)", true);
        return;
      }
      let role = $("user-role").value;
      const onlyDelivery = channels.length === 1 && channels[0] === "logistics";
      // Admin always has Team + full access — Delivery-only must be reviewer/viewer.
      if (onlyDelivery && role === "admin") {
        role = "reviewer";
        $("user-role").value = role;
        applyRolePermDefaults(role);
      }
      let perm_orders = $("perm-orders").value;
      let perm_sku_map = $("perm-sku-map").value;
      let perm_ship_to = $("perm-ship-to").value;
      let perm_delivery = $("perm-delivery")?.value || "";
      if (!channels.includes("logistics")) {
        perm_delivery = "none";
      } else if (!perm_delivery || perm_delivery === "none") {
        // Delivery checkbox is the assign switch — grant edit unless a higher level is set.
        perm_delivery = "edit";
        if ($("perm-delivery")) $("perm-delivery").value = "edit";
      }
      if (onlyDelivery) {
        perm_orders = "none";
        perm_sku_map = "none";
        perm_ship_to = "none";
        $("perm-orders").value = "none";
        $("perm-sku-map").value = "none";
        $("perm-ship-to").value = "none";
      }
      await api("/v1/b2b/users", { method: "PUT", body: JSON.stringify({
        email: $("user-email").value.trim(), full_name: $("user-name").value.trim(),
        role, sap_salesperson_code: $("user-slp").value.trim(),
        is_active: true, password, channels,
        perm_orders, perm_sku_map, perm_ship_to, perm_delivery,
      }) });
      $("user-password").value = "";
      toast(onlyDelivery
        ? "Saved as Delivery-only (reviewer) — Logistics sidebar only"
        : "User saved — they can sign in with that email and password");
      await refreshUsers();
    });
  } catch (e) { toast(e.message, true); }
});

function applyLook() {
  const root = document.documentElement;
  const theme = localStorage.getItem(THEME_KEY) || "light";
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
initSimpleRain(document.querySelector(".login-rain"), 16);

(async function start() {
  auth = loadAuth();
  if (!valid(auth)) { showLogin(); return; }
  showApp();
  try { await boot(); } catch (e) { toast(e.message, true); }
})();
