"""Optional SAP Service Layer: PATCH invoice ``U_FrappeGRNKey`` (and Delivered ``U_POD_DATE``) after shipment-status RDS write."""

from __future__ import annotations

import json
import logging
import re
import traceback
from typing import Any

from khanal_shared.logging import log_json
from khanal_shared.sap_sl import (
    build_opener_and_base_from_env,
    build_opener_and_base_via_ssm,
    sap_sl_enabled,
    sap_sl_request_json,
    sap_sl_use_ssm,
)

from src.delhivery_models import frappe_grn_key_from_delhivery_status
from src.delhivery_store import fetch_doc_entry_for_lr

logger = logging.getLogger(__name__)


def maybe_sync_invoice_grn_after_shipment_status(pool: Any, fields: dict[str, Any]) -> None:
    """When Delhivery status is **Delivered** or **RTO**, PATCH SAP ``U_FrappeGRNKey`` (1 / 2) on the invoice.

    On **Delivered**, also sets ``U_POD_DATE`` from this webhook's ``status_date`` (usually ``timestamp``)
    when present — so SAP gets the delivery date even before the separate POD webhook arrives.

    Uses ``fields['delhivery_status']`` from this shipment webhook (not a second RDS read for status).
    Requires ``delhivery_lr.doc_entry``. Swallows errors after logging — RDS is already updated.
    """
    if not sap_sl_enabled():
        log_json(
            logger,
            logging.DEBUG,
            "delhivery_sap_shipment_grn_skip",
            reason="sap_not_configured",
        )
        return

    lrnum = str(fields["lrnum"]).strip()
    grn = frappe_grn_key_from_delhivery_status(fields.get("delhivery_status"))
    if grn is None:
        log_json(
            logger,
            logging.DEBUG,
            "delhivery_sap_shipment_grn_skip",
            reason="status_not_delivered_or_rto",
            lrnum=lrnum,
            delhivery_status=fields.get("delhivery_status"),
        )
        return

    try:
        with pool.connection() as conn:
            doc_entry_raw = fetch_doc_entry_for_lr(conn, lrnum)
    except Exception:
        log_json(
            logger,
            logging.ERROR,
            "delhivery_sap_shipment_grn_doc_entry_failed",
            lrnum=lrnum,
            trace=traceback.format_exc(),
        )
        return

    if not doc_entry_raw:
        log_json(
            logger,
            logging.INFO,
            "delhivery_sap_shipment_grn_skip",
            reason="no_doc_entry",
            lrnum=lrnum,
        )
        return

    de_s = str(doc_entry_raw).strip()
    if not re.fullmatch(r"-?\d+", de_s):
        log_json(
            logger,
            logging.WARNING,
            "delhivery_sap_shipment_grn_skip",
            reason="invalid_doc_entry",
            lrnum=lrnum,
            doc_entry_raw=de_s[:64],
        )
        return
    doc_entry = int(de_s)

    try:
        if sap_sl_use_ssm():
            opener, base = build_opener_and_base_via_ssm()
        else:
            opener, base, st_login = build_opener_and_base_from_env()
            if st_login != 200:
                log_json(
                    logger,
                    logging.WARNING,
                    "delhivery_sap_shipment_grn_login_bad_status",
                    lrnum=lrnum,
                    http_status=st_login,
                )
                return

        get_url = f"{base}/Invoices({doc_entry})?$select=DocEntry,U_LRtrackno"
        st_get, inv = sap_sl_request_json(opener, "GET", get_url, None)
        if st_get != 200 or not isinstance(inv, dict):
            log_json(
                logger,
                logging.WARNING,
                "delhivery_sap_shipment_grn_skip",
                reason="invoice_get_unexpected",
                lrnum=lrnum,
                doc_entry=doc_entry,
                http_status=st_get,
            )
            return

        sap_lr = inv.get("U_LRtrackno")
        if str(sap_lr or "").strip() != lrnum:
            log_json(
                logger,
                logging.WARNING,
                "delhivery_sap_shipment_grn_lr_mismatch",
                lrnum=lrnum,
                sap_u_lrtrackno=sap_lr,
                doc_entry=doc_entry,
            )
            return

        patch_body: dict[str, Any] = {
            "U_LRtrackno": lrnum,
            "U_FrappeGRNKey": grn,
        }
        # Delivered only: use this status webhook's event date as U_POD_DATE (POD webhook may refine later).
        u_pod_date: str | None = None
        if grn == 1:
            status_date = fields.get("status_date")
            if status_date is not None and hasattr(status_date, "isoformat"):
                u_pod_date = status_date.isoformat()
                patch_body["U_POD_DATE"] = u_pod_date

        patch_url = f"{base}/Invoices({doc_entry})"
        st_patch, patch_body_out = sap_sl_request_json(opener, "PATCH", patch_url, patch_body)
        ok_http = st_patch in (200, 204)

        log_json(
            logger,
            logging.INFO,
            "delhivery_sap_shipment_grn_patched",
            lrnum=lrnum,
            doc_entry=doc_entry,
            u_frappe_grn_key=grn,
            has_pod_date=u_pod_date is not None,
            http_status=st_patch,
            ok=ok_http,
        )
        if not ok_http:
            log_json(
                logger,
                logging.WARNING,
                "delhivery_sap_shipment_grn_patch_http",
                lrnum=lrnum,
                doc_entry=doc_entry,
                response_preview=(
                    json.dumps(patch_body_out)[:800]
                    if isinstance(patch_body_out, (dict, list))
                    else (str(patch_body_out)[:800] if patch_body_out is not None else None)
                ),
            )
    except Exception:
        log_json(
            logger,
            logging.ERROR,
            "delhivery_sap_shipment_grn_failed",
            lrnum=lrnum,
            doc_entry=doc_entry,
            trace=traceback.format_exc(),
        )
