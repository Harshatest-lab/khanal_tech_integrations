/**
 * Logistics delivery portal — calls delivery-api HTTP API (/v1/delivery/*).
 * Requires app.js (auth, hdrs, $, toast, me, canViewDelivery, canEditDelivery, setView, withBusy, esc).
 */
/* global me, hdrs, $, toast, withBusy, esc, canViewDelivery, canEditDelivery, setView */

const DELIVERY_API_BASE = "https://w2tsert3kd.execute-api.ap-south-1.amazonaws.com";

let ldTrackerPage = 1;
let ldSelectedInvoice = null;
let ldInvoiceResults = [];
let ldAdminDetail = null;
let ldAdminTab = "overview";
let ldAdminLr = "";

async function deliveryApi(path, opts = {}) {
  if (!DELIVERY_API_BASE) {
    throw new Error("DELIVERY_API_BASE is not configured in delivery.js.");
  }
  const res = await fetch(`${DELIVERY_API_BASE}${path}`, {
    ...opts,
    headers: { ...hdrs(), ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.message || `Request failed (${res.status})`);
  return data;
}

function ldDash(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "object") return "—";
  return String(v);
}

function ldFmtDate(v) {
  if (!v) return "—";
  const s = String(v);
  if (s.length >= 10) return s.slice(0, 10);
  return s;
}

function ldFmtDateTime(v) {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v).slice(0, 19).replace("T", " ");
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ldPrettyJson(v) {
  if (v === null || v === undefined) return "null — not set";
  if (typeof v === "string") {
    const t = v.trim();
    if (!t) return "null — not set";
    try {
      return JSON.stringify(JSON.parse(t), null, 2);
    } catch {
      return t;
    }
  }
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function ldStatusBadge(status) {
  const s = String(status || "").toLowerCase();
  let cls = "ld-badge ld-badge--other";
  if (s.includes("deliver")) cls = "ld-badge ld-badge--delivered";
  else if (s.includes("rto")) cls = "ld-badge ld-badge--rto";
  else if (s.includes("transit") || s.includes("out for")) cls = "ld-badge ld-badge--transit";
  else if (s.includes("manifest") || s.includes("pending")) cls = "ld-badge ld-badge--manifested";
  return `<span class="${cls}">${esc(status || "—")}</span>`;
}

function ldSapLrCell(r) {
  const sap = r.sap_response;
  let label = "—";
  let cls = "ld-badge ld-badge--other";
  if (sap && typeof sap === "object") {
    if (sap.ok === true || sap.sap_sync_status === "synced") {
      label = "OK";
      cls = "ld-badge ld-badge--sap-ok";
    } else if (sap.ok === false || sap.sap_sync_status === "failed") {
      label = "FAILED";
      cls = "ld-badge ld-badge--sap-failed";
    } else {
      label = String(sap.sap_sync_status || sap.phase || "pending");
      cls = "ld-badge ld-badge--sap-stale";
    }
  } else if (r.doc_entry) {
    label = "Pending";
    cls = "ld-badge ld-badge--sap-stale";
  }
  return `<div class="ld-sap-card"><span class="${cls}">${esc(label)}</span></div>`;
}

function ldSapPodCell(r) {
  const d = r.sap_pod_display || "none";
  const map = {
    none: ["—", "ld-badge ld-badge--other"],
    posted: ["OK", "ld-badge ld-badge--sap-ok"],
    pending: ["Pending", "ld-badge ld-badge--sap-stale"],
    failed_fixable: ["FAILED", "ld-badge ld-badge--sap-failed"],
    failed_blocked: ["FAILED", "ld-badge ld-badge--sap-failed"],
  };
  const [label, cls] = map[d] || ["—", "ld-badge ld-badge--other"];
  const sub = d === "failed_blocked" ? "<div class='ld-cell-sub'>No DocEntry</div>" : "";
  const fix =
    canEditDelivery() && r.sap_pod_fixable
      ? `<button class="btn small primary ld-fix-pod" data-lr="${esc(r.lrnum)}" type="button">Fix it</button>`
      : "";
  return `<div class="ld-sap-pod-cell"><span class="${cls}">${esc(label)}</span>${sub}${fix}</div>`;
}

async function loadLogisticsView(name) {
  if (!canViewDelivery()) return;
  $("channel-pill").textContent = "Logistics";
  $("channel-pill").className = "chip more";
  $("fetch-btn")?.classList.add("hidden");
  if (name === "logistics-home") await refreshLogisticsHome();
  if (name === "logistics-delivery") {
    $("ld-invoice-body").innerHTML = "";
    $("ld-delivery-status").textContent = "";
  }
  if (name === "logistics-tracker") await refreshLogisticsTracker(1);
}

async function refreshLogisticsHome() {
  const kpi = $("ld-kpi-row");
  const alertsEl = $("ld-alerts-list");
  if (!kpi || !alertsEl) return;
  try {
    const [retry, tracker, alerts] = await Promise.all([
      deliveryApi("/v1/delivery/sap-retry/count"),
      deliveryApi("/v1/delivery/tracker?page=1&page_size=1"),
      deliveryApi("/v1/delivery/alerts?limit=20"),
    ]);
    kpi.innerHTML = `
      <div class="kpi"><span>Tracker rows</span><strong>${tracker.total ?? "—"}</strong></div>
      <div class="kpi"><span>SAP retry pending</span><strong>${retry.count ?? 0}</strong></div>
      <div class="kpi"><span>Open issues</span><strong>${alerts.count ?? 0}</strong></div>
    `;
    alertsEl.innerHTML =
      (alerts.alerts || [])
        .map(
          (a) => `
      <div class="inbox-item ld-alert-item ${a.severity === "danger" ? "danger" : ""}" data-lr="${esc(a.lrnum || "")}">
        <strong>${esc(a.title || "Issue")} · LR ${esc(a.lrnum || "")}</strong>
        <p class="muted">${esc(a.message || "")}</p>
      </div>`
        )
        .join("") || "<p class='muted'>No open issues.</p>";
  } catch (err) {
    toast(err.message, true);
  }
}

async function searchLdInvoices() {
  const qs = new URLSearchParams();
  const fields = {
    doc_num: $("ld-doc-num")?.value?.trim(),
    doc_entry: $("ld-doc-entry-search")?.value?.trim(),
    series_name: $("ld-series-name")?.value?.trim(),
    doc_date: $("ld-doc-date")?.value?.trim(),
    card_code: $("ld-card-code")?.value?.trim(),
    card_name: $("ld-card-name")?.value?.trim(),
  };
  Object.entries(fields).forEach(([k, v]) => {
    if (v) qs.set(k, v);
  });
  if (!fields.doc_num && !fields.doc_entry) {
    toast("Enter DocNum (AR Invoice Num) and/or DocEntry to search", true);
    return;
  }
  const data = await deliveryApi(`/v1/delivery/invoices?${qs}`);
  const body = $("ld-invoice-body");
  const rows = data.invoices || [];
  ldInvoiceResults = rows;
  body.innerHTML =
    rows
      .map((inv) => {
        const selected =
          ldSelectedInvoice && String(ldSelectedInvoice.doc_entry) === String(inv.doc_entry)
            ? "is-selected"
            : "";
        return `
    <tr class="ld-result-row ${selected}" data-de="${esc(inv.doc_entry)}">
      <td class="ld-row-pick"></td>
      <td>${esc(inv.doc_entry)}</td>
      <td>${esc(inv.doc_num)}</td>
      <td>${esc(inv.series_name)}</td>
      <td>${esc(ldFmtDate(inv.doc_date))}</td>
      <td>${esc(inv.card_code)}</td>
      <td>${esc(inv.card_name)}</td>
      <td>${esc(inv.doc_total)}</td>
    </tr>`;
      })
      .join("") || "<tr><td colspan='8' class='muted'>No invoices found.</td></tr>";
  if ($("ld-invoice-hint")) {
    $("ld-invoice-hint").textContent = rows.length
      ? `${rows.length} invoice(s) — click a row to select`
      : "No invoices found for these filters.";
  }
  if (rows.length === 1) selectLdInvoiceByEntry(rows[0].doc_entry);
}

function selectLdInvoiceByEntry(docEntry) {
  const inv = ldInvoiceResults.find((r) => String(r.doc_entry) === String(docEntry));
  if (!inv) {
    toast("Invoice not found in search results — search again", true);
    return;
  }
  ldSelectedInvoice = {
    doc_entry: inv.doc_entry != null ? String(inv.doc_entry) : "",
    doc_num: inv.doc_num != null ? String(inv.doc_num) : "",
    series_name: inv.series_name != null ? String(inv.series_name) : "",
    card_code: inv.card_code != null ? String(inv.card_code) : "",
    card_name: inv.card_name != null ? String(inv.card_name) : "",
  };
  if ($("ld-selected-de")) $("ld-selected-de").value = ldSelectedInvoice.doc_entry;
  if ($("ld-selected-dn")) $("ld-selected-dn").value = ldSelectedInvoice.doc_num;
  if ($("ld-selected-series")) $("ld-selected-series").value = ldSelectedInvoice.series_name;
  // Mirror into search fields so filters match the selected invoice
  if ($("ld-doc-num") && ldSelectedInvoice.doc_num) $("ld-doc-num").value = ldSelectedInvoice.doc_num;
  if ($("ld-doc-entry-search") && ldSelectedInvoice.doc_entry) {
    $("ld-doc-entry-search").value = ldSelectedInvoice.doc_entry;
  }
  if ($("ld-series-name") && ldSelectedInvoice.series_name) {
    $("ld-series-name").value = ldSelectedInvoice.series_name;
  }
  if ($("ld-card-code")) $("ld-card-code").value = ldSelectedInvoice.card_code || "";
  if ($("ld-card-name")) $("ld-card-name").value = ldSelectedInvoice.card_name || "";

  if ($("ld-selected-label")) {
    const dn = ldSelectedInvoice.doc_num ? ` · DocNum ${ldSelectedInvoice.doc_num}` : "";
    $("ld-selected-label").textContent =
      `DocEntry ${ldSelectedInvoice.doc_entry}${dn} · ${ldSelectedInvoice.card_name || "invoice"}`;
  }
  document.querySelectorAll("#ld-invoice-body .ld-result-row").forEach((tr) => {
    tr.classList.toggle("is-selected", tr.dataset.de === String(ldSelectedInvoice.doc_entry));
  });
  toast(
    ldSelectedInvoice.doc_num
      ? `Selected DocEntry ${ldSelectedInvoice.doc_entry} · DocNum ${ldSelectedInvoice.doc_num}`
      : `Selected DocEntry ${ldSelectedInvoice.doc_entry}`
  );
}

async function verifyLdLr() {
  if (!canEditDelivery()) {
    toast("Edit permission required", true);
    return;
  }
  const lr = $("ld-lrnum")?.value?.trim();
  const de = ldSelectedInvoice?.doc_entry || $("ld-selected-de")?.value?.trim();
  if (!lr || !de) {
    toast("Select an invoice and enter LR number", true);
    return;
  }
  const data = await deliveryApi("/v1/delivery/lr/verify", {
    method: "POST",
    body: JSON.stringify({
      lrnum: lr,
      doc_entry: Number(de),
      doc_num: ldSelectedInvoice?.doc_num || $("ld-selected-dn")?.value || null,
      series: ldSelectedInvoice?.series_name || $("ld-selected-series")?.value || null,
    }),
  });
  $("ld-delivery-status").textContent = ldPrettyJson(data);
  toast("LR checked with Delhivery");
}

async function submitLdLr() {
  if (!canEditDelivery()) {
    toast("Edit permission required", true);
    return;
  }
  const lr = $("ld-lrnum")?.value?.trim();
  const de = ldSelectedInvoice?.doc_entry || $("ld-selected-de")?.value?.trim();
  if (!lr || !de) {
    toast("Select an invoice and enter LR number", true);
    return;
  }
  if ($("ld-delivery-status")) $("ld-delivery-status").textContent = "Submitting to SAP…";
  const data = await deliveryApi("/v1/delivery/lr/submit", {
    method: "POST",
    body: JSON.stringify({
      lrnum: lr,
      doc_entry: Number(de),
      doc_num: ldSelectedInvoice?.doc_num || $("ld-selected-dn")?.value || null,
      series: ldSelectedInvoice?.series_name || $("ld-selected-series")?.value || null,
    }),
  });
  $("ld-delivery-status").textContent = ldPrettyJson(data);
  toast(data.message || "Submitted");
}

async function loadTrackerAlertsBanner() {
  const el = $("ld-tracker-alerts");
  const countLabel = $("ld-retry-count-label");
  if (!el) return;
  try {
    const [alerts, retry] = await Promise.all([
      deliveryApi("/v1/delivery/alerts?limit=35"),
      deliveryApi("/v1/delivery/sap-retry/count"),
    ]);
    if (countLabel) {
      countLabel.textContent =
        (retry.count || 0) > 0
          ? `${retry.count} failed SAP sync(s) in database`
          : "No failed SAP syncs in database.";
    }
    el.innerHTML =
      (alerts.alerts || [])
        .map(
          (a) => `
      <button type="button" class="ld-alert-row ${a.severity === "danger" ? "is-danger" : ""}" data-lr="${esc(a.lrnum || "")}">
        <strong>${esc(a.title || "SAP")}</strong>
        <span>${esc(a.message || "")}</span>
      </button>`
        )
        .join("") || "<p class='muted'>No open SAP POD / tracking issues.</p>";
  } catch (err) {
    el.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

async function refreshLogisticsTracker(page = ldTrackerPage) {
  ldTrackerPage = page;
  await loadTrackerAlertsBanner();
  const qs = new URLSearchParams({ page: String(page), page_size: "20" });
  const f = {
    lrnum: $("ld-f-lrnum")?.value?.trim(),
    doc_entry: $("ld-f-doc-entry")?.value?.trim(),
    doc_num: $("ld-f-doc-num")?.value?.trim(),
    status: $("ld-f-status")?.value?.trim(),
  };
  Object.entries(f).forEach(([k, v]) => {
    if (v) qs.set(k, v);
  });
  const data = await deliveryApi(`/v1/delivery/tracker?${qs}`);
  const start = (data.page - 1) * data.page_size;
  $("ld-tracker-count").textContent = `Records (${data.total})`;
  const body = $("ld-tracker-body");
  body.innerHTML =
    (data.records || [])
      .map((r, i) => {
        const idx = start + i + 1;
        return `
    <tr data-lr="${esc(r.lrnum)}">
      <td>${idx}</td>
      <td><strong>${esc(r.lrnum)}</strong></td>
      <td>${ldStatusBadge(r.delhivery_status)}</td>
      <td class="ld-clip">${esc(ldDash(r.waybill))}</td>
      <td>${ldSapLrCell(r)}</td>
      <td>${esc(ldDash(r.doc_entry))}</td>
      <td>${esc(ldDash(r.doc_num))}</td>
      <td>${esc(ldDash(r.series))}</td>
      <td>${ldSapPodCell(r)}</td>
      <td class="ld-clip">${esc(ldFmtDateTime(r.updated_at))}</td>
      <td class="ld-actions">
        <button class="btn small primary ld-admin-open" data-lr="${esc(r.lrnum)}" type="button">Admin view</button>
        ${
          canEditDelivery() && r.needs_sap_retry
            ? `<button class="btn small ghost ld-retry-sap" data-lr="${esc(r.lrnum)}" type="button">Retry SAP</button>`
            : ""
        }
      </td>
    </tr>`;
      })
      .join("") || "<tr><td colspan='11' class='muted'>No rows.</td></tr>";

  const pager = $("ld-tracker-pager");
  if (pager) {
    pager.innerHTML = `
      <button class="btn ghost" type="button" data-ld-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>Prev</button>
      <span class="muted small">Page ${data.page} / ${data.pages}</span>
      <button class="btn ghost" type="button" data-ld-page="${page + 1}" ${page >= data.pages ? "disabled" : ""}>Next</button>
    `;
  }
}

function closeLdAdmin() {
  $("ld-admin-modal")?.classList.add("hidden");
  ldAdminDetail = null;
  ldAdminLr = "";
}

function ldOverviewGrid(lr, meta) {
  const sap = typeof lr.sap_response === "object" && lr.sap_response ? lr.sap_response : null;
  let trackSap = "—";
  if (sap) {
    if (sap.ok === true || sap.sap_sync_status === "synced") trackSap = "Posted";
    else if (sap.ok === false || sap.sap_sync_status === "failed") trackSap = "Failed";
  }
  const fields = [
    ["LR number", lr.lrnum],
    ["Delhivery status", lr.delhivery_status],
    ["Status type", lr.status_type],
    ["Waybill (WBN)", lr.waybill],
    ["Location", lr.location],
    ["Shipment remark", lr.shipment_remark],
    ["Latest POD id", lr.latest_pod_id],
    ["POD received", lr.pod_received],
    ["Test (legacy)", lr.test],
    ["DocEntry", lr.doc_entry],
    ["DocNum", lr.doc_num],
    ["Series", lr.series],
    ["Track → SAP", trackSap],
    ["POD → SAP", meta.sap_pod_posted ? "Posted" : meta.sap_pod_display === "none" ? "—" : "Not posted"],
    ["Created", ldFmtDateTime(lr.created_at)],
    ["Updated", ldFmtDateTime(lr.updated_at)],
  ];
  return `
    <p class="ld-admin-section-label">Overview (delhivery_lr)</p>
    <div class="ld-overview-grid">
      ${fields
        .map(
          ([k, v]) => `
        <div class="ld-ov-item">
          <span class="ld-ov-k">${esc(k)}</span>
          <span class="ld-ov-v">${esc(ldDash(v))}</span>
        </div>`
        )
        .join("")}
    </div>`;
}

function ldStatusHistoryHtml(events) {
  if (!events.length) {
    return `<p class="muted">No webhook status events yet for this LR.<br/>
      Status history is filled when Delhivery sends shipment-status webhooks — Check LR / Submit does not create these rows.</p>`;
  }
  return `
    <p class="ld-admin-section-label">delhivery_lr_status_events</p>
    <p class="muted small">${events.length} event(s) · newest first</p>
    ${events
      .map(
        (ev) => `
      <article class="ld-event-card">
        <header>Event id ${esc(ev.id)} · ${esc(ldFmtDateTime(ev.received_at))}</header>
        <pre class="ld-json-block">${esc(ldPrettyJson(ev.payload))}</pre>
      </article>`
      )
      .join("")}`;
}

function ldPodsHtml(pods) {
  if (!pods.length) return "<p class='muted'>No POD rows.</p>";
  const n = pods.length;
  return `
    <p class="ld-admin-section-label">delhivery_lr_pod</p>
    <p class="muted small">${n} POD row(s) · newest first · <strong>v${n}</strong> = latest, <strong>v1</strong> = earliest.</p>
    ${pods
      .map((pod, i) => {
        const ver = n - i;
        const url = pod.pod_url ? String(pod.pod_url).trim() : "";
        return `
      <article class="ld-event-card">
        <header>V${ver} · ${esc(ldFmtDateTime(pod.received_at))} · ID ${esc(pod.id)}</header>
        <div class="ld-overview-grid ld-overview-grid-3">
          <div class="ld-ov-item"><span class="ld-ov-k">Waybill</span><span class="ld-ov-v">${esc(ldDash(pod.waybill))}</span></div>
          <div class="ld-ov-item"><span class="ld-ov-k">Order ID</span><span class="ld-ov-v">${esc(ldDash(pod.order_id))}</span></div>
          <div class="ld-ov-item"><span class="ld-ov-k">POD format</span><span class="ld-ov-v">${esc(ldDash(pod.pod_format))}</span></div>
        </div>
        ${url ? `<p><a class="btn small primary" href="${esc(url)}" target="_blank" rel="noopener">Download / open POD</a></p>` : ""}
        <p class="ld-admin-section-label">raw_payload (full POD webhook JSON)</p>
        <pre class="ld-json-block">${esc(ldPrettyJson(pod.raw_payload))}</pre>
        <p class="ld-admin-section-label">sap_pod_response (SAP after POD upload)</p>
        <pre class="ld-json-block">${esc(ldPrettyJson(pod.sap_pod_response))}</pre>
      </article>`;
      })
      .join("")}`;
}

function ldRawHtml(lr) {
  return `
    <p class="ld-admin-section-label">raw_tracking (Delhivery track / snapshot)</p>
    <pre class="ld-json-block">${esc(ldPrettyJson(lr.raw_tracking))}</pre>
    <p class="ld-admin-section-label">last_status_webhook</p>
    <pre class="ld-json-block">${esc(ldPrettyJson(lr.last_status_webhook))}</pre>
    <p class="ld-admin-section-label">sap_response (PATCH audit)</p>
    <pre class="ld-json-block">${esc(ldPrettyJson(lr.sap_response))}</pre>`;
}

function renderLdAdminBody() {
  const body = $("ld-admin-body");
  if (!body || !ldAdminDetail) return;
  const lr = ldAdminDetail.lr || {};
  const warnings = (ldAdminDetail.warnings || []).map((w) => `<p class="ld-warn">${esc(w)}</p>`).join("");
  let html = warnings;
  if (ldAdminTab === "overview") html += ldOverviewGrid(lr, ldAdminDetail);
  else if (ldAdminTab === "status") html += ldStatusHistoryHtml(ldAdminDetail.status_events || []);
  else if (ldAdminTab === "pods") html += ldPodsHtml(ldAdminDetail.pods || []);
  else html += ldRawHtml(lr);
  body.innerHTML = html;
}

async function openLdAdmin(lrnum) {
  const lr = String(lrnum || "").trim();
  if (!lr) return;
  const data = await deliveryApi(`/v1/delivery/tracker/${encodeURIComponent(lr)}`);
  ldAdminDetail = data;
  ldAdminLr = lr;
  ldAdminTab = "overview";
  $("ld-admin-title").textContent = `Admin — LR ${lr}`;
  document.querySelectorAll("#ld-admin-tabs .ld-admin-tab").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.tab === "overview");
  });
  renderLdAdminBody();
  $("ld-admin-modal")?.classList.remove("hidden");
}

function bindLogisticsEvents() {
  // Logistics sidebar clicks are bound in app.js (logistics-nav).

  $("ld-toggle-filters")?.addEventListener("click", () => {
    const panel = $("ld-filters-panel");
    if (!panel) return;
    panel.classList.toggle("is-open");
    $("ld-toggle-filters").classList.toggle("is-active", panel.classList.contains("is-open"));
  });
  $("ld-clear-date")?.addEventListener("click", () => {
    if ($("ld-doc-date")) $("ld-doc-date").value = "";
  });
  $("ld-clear-invoice-search")?.addEventListener("click", () => {
    ["ld-doc-num", "ld-series-name", "ld-doc-date", "ld-doc-entry-search", "ld-card-code", "ld-card-name"].forEach((id) => {
      if ($(id)) $(id).value = "";
    });
    ldSelectedInvoice = null;
    ldInvoiceResults = [];
    if ($("ld-invoice-body")) $("ld-invoice-body").innerHTML = "";
    if ($("ld-invoice-hint")) {
      $("ld-invoice-hint").textContent = "Enter DocNum (AR Invoice Num) and/or DocEntry, then Search.";
    }
    if ($("ld-selected-de")) $("ld-selected-de").value = "";
    if ($("ld-selected-dn")) $("ld-selected-dn").value = "";
    if ($("ld-selected-series")) $("ld-selected-series").value = "";
    if ($("ld-selected-label")) $("ld-selected-label").textContent = "No invoice selected";
    if ($("ld-delivery-status")) $("ld-delivery-status").textContent = "";
  });

  $("ld-search-invoices")?.addEventListener("click", () =>
    withBusy($("ld-search-invoices"), () => searchLdInvoices()).catch((e) => toast(e.message, true))
  );
  $("ld-invoice-body")?.addEventListener("click", (e) => {
    const row = e.target.closest(".ld-result-row");
    if (row?.dataset.de) selectLdInvoiceByEntry(row.dataset.de);
  });
  $("ld-verify-lr")?.addEventListener("click", () =>
    withBusy($("ld-verify-lr"), () => verifyLdLr()).catch((e) => toast(e.message, true))
  );
  $("ld-submit-lr")?.addEventListener("click", () =>
    withBusy($("ld-submit-lr"), () => submitLdLr()).catch((e) => toast(e.message, true))
  );

  $("ld-tracker-search")?.addEventListener("click", () =>
    withBusy($("ld-tracker-search"), () => refreshLogisticsTracker(1)).catch((e) => toast(e.message, true))
  );
  $("ld-tracker-clear")?.addEventListener("click", () => {
    ["ld-f-lrnum", "ld-f-doc-entry", "ld-f-doc-num"].forEach((id) => {
      if ($(id)) $(id).value = "";
    });
    if ($("ld-f-status")) $("ld-f-status").value = "";
    refreshLogisticsTracker(1).catch((e) => toast(e.message, true));
  });
  $("ld-retry-all")?.addEventListener("click", () => {
    if (!canEditDelivery()) {
      toast("Edit permission required", true);
      return;
    }
    withBusy($("ld-retry-all"), () =>
      deliveryApi("/v1/delivery/sap-retry", { method: "POST", body: JSON.stringify({ max_rows: 50 }) }).then((d) => {
        toast(d.message || "Done");
        return refreshLogisticsTracker(1);
      })
    ).catch((e) => toast(e.message, true));
  });

  $("ld-tracker-alerts")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-lr]");
    if (!btn?.dataset.lr) return;
    if ($("ld-f-lrnum")) $("ld-f-lrnum").value = btn.dataset.lr;
    refreshLogisticsTracker(1).catch((err) => toast(err.message, true));
  });
  $("ld-alerts-list")?.addEventListener("click", (e) => {
    const item = e.target.closest("[data-lr]");
    if (!item?.dataset.lr) return;
    if (typeof setView === "function") setView("logistics-tracker");
    if ($("ld-f-lrnum")) $("ld-f-lrnum").value = item.dataset.lr;
    loadLogisticsView("logistics-tracker").catch((err) => toast(err.message, true));
  });

  $("ld-tracker-body")?.addEventListener("click", (e) => {
    const admin = e.target.closest(".ld-admin-open");
    const fix = e.target.closest(".ld-fix-pod");
    const retry = e.target.closest(".ld-retry-sap");
    if (admin) {
      withBusy(admin, () => openLdAdmin(admin.dataset.lr)).catch((err) => toast(err.message, true));
      return;
    }
    if (fix) {
      withBusy(fix, () =>
        deliveryApi("/v1/delivery/pod/fix", {
          method: "POST",
          body: JSON.stringify({ lrnum: fix.dataset.lr }),
        }).then((d) => {
          toast(d.message || "Fixed");
          return refreshLogisticsTracker();
        })
      ).catch((err) => toast(err.message, true));
      return;
    }
    if (retry) {
      withBusy(retry, () =>
        deliveryApi("/v1/delivery/sap-retry", {
          method: "POST",
          body: JSON.stringify({ lrnum: retry.dataset.lr }),
        }).then((d) => {
          const st = d.status || "";
          if (st === "already_synced") toast(d.message || "Already synced — nothing to retry");
          else if (st === "skipped") toast(d.message || "No SAP retry needed", true);
          else if (st === "failed") toast(d.message || "SAP retry failed", true);
          else toast(d.message || "SAP retry done");
          return refreshLogisticsTracker();
        })
      ).catch((err) => toast(err.message, true));
    }
  });

  $("ld-tracker-pager")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-ld-page]");
    if (!btn || btn.disabled) return;
    refreshLogisticsTracker(Number(btn.dataset.ldPage)).catch((err) => toast(err.message, true));
  });

  $("ld-admin-close")?.addEventListener("click", closeLdAdmin);
  $("ld-admin-modal")?.addEventListener("click", (e) => {
    if (e.target === $("ld-admin-modal")) closeLdAdmin();
  });
  $("ld-admin-tabs")?.addEventListener("click", (e) => {
    const tab = e.target.closest(".ld-admin-tab");
    if (!tab) return;
    ldAdminTab = tab.dataset.tab;
    document.querySelectorAll("#ld-admin-tabs .ld-admin-tab").forEach((btn) => {
      btn.classList.toggle("is-active", btn === tab);
    });
    renderLdAdminBody();
  });
}

document.addEventListener("DOMContentLoaded", () => bindLogisticsEvents());
