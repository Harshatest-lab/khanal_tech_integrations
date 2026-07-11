/** Hardcoded API — change when deploying to prod / another stage. */
const API_BASE = "https://r6y9eiin7d.execute-api.us-east-1.amazonaws.com";

const COLSPAN = 12;

let currentOffset = 0;
let pageTotal = 0;
let lastDashboardStats = null;

const statusFilter = document.getElementById("status-filter");
const poFilter = document.getElementById("po-filter");
const orderDateFrom = document.getElementById("order-date-from");
const orderDateTo = document.getElementById("order-date-to");
const shippingFilter = document.getElementById("shipping-filter");
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
    const res = await fetch(`${API_BASE}${path}`);
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

function statusBadge(status) {
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
  return `<span class="badge ${cls}">${escapeHtml(status || "—")}</span>`;
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
  statusFilter.innerHTML = '<option value="">All statuses</option>';
  for (const row of byStatus || []) {
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
  sapAlertWrap?.closest(".page-topbar-actions")?.classList.toggle("hidden", !visible);
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

function formatOrderDateUtc(ms) {
  if (ms == null || ms === "") return "—";
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) return "—";
  const date = d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
  const time = d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  });
  return `${date} ${time} UTC`;
}

function formatDateStackFromMs(ms) {
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
    timeZone: "UTC",
  });
  const timeLine = d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  });
  return `<span class="date-stack"><span class="date-line">${dateLine}</span><span class="time-line">${timeLine} UTC</span></span>`;
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
      <td>${formatDateStackFromMs(row.order_date_ms)}</td>
      <td class="cell-stack cell-center">${renderLineStack(lines, (_line, index) => `<div class="line-stack line-num">${displayLineIndex(index)}</div>`)}</td>
      <td class="cell-stack">${renderLineStack(lines, (line) => `<div class="line-stack">${statusBadge(line.status)}</div>`)}</td>
      <td class="cell-stack cell-mono">${renderLineStack(lines, (line) => `<div class="line-stack">${escapeHtml(line.sku || "—")}</div>`)}</td>
      <td class="cell-stack cell-center">${renderLineStack(lines, (line) => `<div class="line-stack">${escapeHtml(line.quantity_amount || "—")}</div>`)}</td>
      <td class="cell-stack">${renderLineStack(lines, (line) => `<div class="line-stack">${formatDateStack(line.status_date)}</div>`)}</td>
      <td>${formatDateStack(row.updated_at)}</td>
      <td class="cell-sap-sync">${renderSapSyncCell(row)}</td>
      <td class="cell-center"><button type="button" class="btn-view">View</button></td>
    </tr>`;
    })
    .join("");

  ordersBody.querySelectorAll(".data-row .btn-view").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest(".data-row");
      toggleOrderDetail(btn, row.getAttribute("data-po"));
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
  const result = {
    unitPrice: null,
    currency: "USD",
    productTax: null,
    shipping: null,
    shippingTax: null,
  };

  for (const charge of charges) {
    const chargeType = String(charge.chargeType || "").toUpperCase();
    const chargeName = String(charge.chargeName || "");
    const amount = charge.chargeAmount?.amount;
    const tax =
      charge.tax?.taxAmount?.amount ??
      charge.taxAndOtherFees?.taxAndOtherFeesAmount?.amount;

    if (chargeType === "PRODUCT" || chargeName === "ItemPrice") {
      result.unitPrice = amount != null ? Number(amount) : null;
      result.productTax = tax != null ? Number(tax) : null;
      result.currency = charge.chargeAmount?.currency || result.currency;
    } else if (chargeType === "SHIPPING" || chargeName === "Shipping") {
      result.shipping = amount != null ? Number(amount) : null;
      result.shippingTax = tax != null ? Number(tax) : null;
    }
  }

  return result;
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
    const lineTotal =
      (charges.unitPrice ?? 0) +
      (charges.productTax ?? 0) +
      (charges.shipping ?? 0) +
      (charges.shippingTax ?? 0);

    return {
      ...line,
      charges,
      qty,
      hasShipping,
      lineTotal: jsonLine ? lineTotal : null,
      shipMethod: jsonLine?.fulfillment?.shipMethod || null,
    };
  });
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
        ${statusBadge(row.status)}
        <span class="line-item-meta cell-mono">${escapeHtml(row.sku || "—")}</span>
        <span class="line-item-meta">Qty ${escapeHtml(String(row.qty))}</span>
      </div>
      <p class="line-item-product">${escapeHtml(row.product_name || "—")}</p>
      <div class="line-item-panels">
        <div class="line-item-panel">
          <div class="panel-title">Charges &amp; tax</div>
          <dl class="detail-dl">
            <div><dt>Unit price</dt><dd class="money">${hasPricing ? formatMoney(c.unitPrice, currency) : "—"}</dd></div>
            <div><dt>Product tax</dt><dd class="money">${hasPricing ? formatMoney(c.productTax, currency) : "—"}</dd></div>
            <div><dt>Shipping</dt><dd class="money">${hasPricing ? shippingCell : "—"}</dd></div>
            <div><dt>Shipping tax</dt><dd class="money">${hasPricing && row.hasShipping ? formatMoney(c.shippingTax, currency) : "—"}</dd></div>
            <div class="detail-dl-total"><dt>Line total</dt><dd class="money money-total">${hasPricing ? formatMoney(row.lineTotal, currency) : "—"}</dd></div>
          </dl>
        </div>
        <div class="line-item-panel">
          <div class="panel-title">Shipment</div>
          <dl class="detail-dl">
            <div><dt>Tracking</dt><dd class="cell-mono">${escapeHtml(row.tracking_number || "—")}</dd></div>
            <div><dt>Carrier</dt><dd>${escapeHtml(row.carrier_name || "—")}</dd></div>
            <div><dt>Ship method</dt><dd>${escapeHtml(row.shipMethod || "—")}</dd></div>
          </dl>
        </div>
      </div>
    </div>`;
}

function renderUnifiedOrderTable(detail, lines) {
  const enriched = mergeLinesWithCharges(lines, orderLineItemsFromDetail(detail));
  const hasPricing = enriched.some((row) => row.charges.unitPrice != null);
  const currency = enriched.find((row) => row.charges.currency)?.charges.currency || "USD";

  let subtotal = 0;
  let productTax = 0;
  let shipping = 0;
  let shippingTax = 0;

  const lineBoxes = enriched
    .map((row, index) => {
      const c = row.charges;
      if (c.unitPrice != null) subtotal += c.unitPrice;
      if (c.productTax != null) productTax += c.productTax;
      if (c.shipping != null) shipping += c.shipping;
      if (c.shippingTax != null) shippingTax += c.shippingTax;
      return renderLineDetailBox(row, index, currency, hasPricing);
    })
    .join("");

  const orderTotal = subtotal + productTax + shipping + shippingTax;

  const totalsBox = hasPricing
    ? `
    <div class="order-totals-box">
      <div class="panel-title">Order totals</div>
      <dl class="totals-dl">
        <div><dt>Subtotal</dt><dd class="money">${formatMoney(subtotal, currency)}</dd></div>
        <div><dt>Product tax</dt><dd class="money">${formatMoney(productTax, currency)}</dd></div>
        <div><dt>Shipping</dt><dd class="money">${shipping > 0 ? formatMoney(shipping, currency) : "—"}</dd></div>
        <div><dt>Shipping tax</dt><dd class="money">${shippingTax > 0 ? formatMoney(shippingTax, currency) : "—"}</dd></div>
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
          </div>
        </div>
        <button type="button" class="btn-outline btn-hide-trace">Hide trace</button>
      </div>

      <div class="trace-cards">
        <div class="trace-card"><span class="trace-card-label">Customer order</span><span class="trace-card-value">${escapeHtml(detail.customer_order_id || "—")}</span></div>
        <div class="trace-card"><span class="trace-card-label">Order date (UTC)</span><span class="trace-card-value">${escapeHtml(formatOrderDateUtc(detail.order_date_ms))}</span></div>
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

  const status = statusFilter.value;
  const po = poFilter.value.trim();
  const limit = pageSize();
  const statusQ = status ? `&status=${encodeURIComponent(status)}` : "";
  const poQ = po ? `&purchase_order_id=${encodeURIComponent(po)}` : "";
  const fromQ = orderDateFrom.value
    ? `&order_date_from=${encodeURIComponent(orderDateFrom.value)}`
    : "";
  const toQ = orderDateTo.value
    ? `&order_date_to=${encodeURIComponent(orderDateTo.value)}`
    : "";
  const shipQ = shippingFilter.value
    ? `&has_shipping=${encodeURIComponent(shippingFilter.value)}`
    : "";
  const sapQ = sapSyncFilter?.value
    ? `&sap_sync=${encodeURIComponent(sapSyncFilter.value)}`
    : "";

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
      `/v1/walmart/orders?limit=${limit}&offset=${currentOffset}${statusQ}${poQ}${fromQ}${toQ}${shipQ}${sapQ}`
    );
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

function schedulePoFilterSearch() {
  clearTimeout(poFilterTimer);
  poFilterTimer = setTimeout(() => {
    loadOrders(true).catch((e) => showError(e.message));
  }, 400);
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

shippingFilter.addEventListener("change", () => {
  loadOrders(true).catch((e) => showError(e.message));
});

if (sapSyncFilter) {
  sapSyncFilter.addEventListener("change", () => {
    if (lastDashboardStats) updateSapAlertBell(lastDashboardStats);
    loadOrders(true).catch((e) => showError(e.message));
  });
}

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

document.addEventListener("click", (e) => {
  if (!sapAlertWrap?.contains(e.target)) {
    closeSapAlertPanel();
  }
});

poFilter.addEventListener("input", schedulePoFilterSearch);

poFilter.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    clearTimeout(poFilterTimer);
    loadOrders(true).catch((err) => showError(err.message));
  }
});

clearFiltersBtn.addEventListener("click", () => {
  poFilter.value = "";
  statusFilter.value = "";
  orderDateFrom.value = "";
  orderDateTo.value = "";
  shippingFilter.value = "";
  if (sapSyncFilter) sapSyncFilter.value = "";
  loadOrders(true).catch((e) => showError(e.message));
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

loadOrders(true).catch((e) => {
  showError(e.message);
  ordersBody.innerHTML =
    `<tr><td colspan="${COLSPAN}" class="loading-cell">Failed to load data.</td></tr>`;
});

if (isFileProtocol()) {
  showError(fileProtocolHelp());
}
