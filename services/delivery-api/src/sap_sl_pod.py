"""Optional SAP Business One Service Layer: PATCH Invoice POD fields after RDS write."""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import datetime, timezone
from typing import Any

from khanal_shared.logging import log_json

from src.delhivery_models import frappe_grn_key_from_delhivery_status
from src.delhivery_store import fetch_doc_entry_and_delhivery_status_for_lr, save_pod_sap_sl_audit
from khanal_shared.sap_sl import (
    build_opener_and_base_from_env,
    build_opener_and_base_via_ssm,
    sap_sl_enabled,
    sap_sl_request_json,
    sap_sl_use_ssm,
)

logger = logging.getLogger(__name__)


def _safe_save_pod_sap_audit(pool: Any, pod_row_id: int, audit: dict[str, Any]) -> None:
    """Persist audit JSON on ``delhivery_lr_pod.sap_pod_response`` (never touches Frappe ``delhivery_lr.sap_response``)."""
    try:
        with pool.connection() as conn:
            save_pod_sap_sl_audit(conn, pod_row_id, audit)
    except Exception:
        log_json(
            logger,
            logging.WARNING,
            "delhivery_sap_pod_audit_save_failed",
            pod_id=pod_row_id,
            trace=traceback.format_exc(),
        )


def maybe_sync_invoice_pod_after_db(
    pool: Any,
    resolved_lrnum: str,
    fields: dict[str, Any],
    pod_row_id: int,
) -> None:
    """If SAP env is set and ``delhivery_lr.doc_entry`` exists, verify ``U_LRtrackno`` then PATCH POD fields.

    PATCH includes ``U_FrappeGRNKey`` (**1** = **Delivered**, **2** = **RTO**, case-insensitive) only when
    ``delhivery_lr.delhivery_status`` in RDS matches (shipment-status webhooks); otherwise the key is omitted.

    Outcome is stored on **this POD row** in ``delhivery_lr_pod.sap_pod_response`` (not ``delhivery_lr.sap_response``,
    which is reserved for Frappe). Swallows errors after logging — POD is already in Postgres; SAP must not poison SQS retries.
    """
    if not sap_sl_enabled():
        log_json(
            logger,
            logging.INFO,
            "delhivery_sap_pod_skip",
            reason="sap_not_configured",
            lrnum=resolved_lrnum,
            hint="Set SAP_SL_USE_SSM or SAP_SL_* on the Lambda at deploy",
        )
        return

    pod_url = fields.get("pod_url")
    if not pod_url or not isinstance(pod_url, str) or not pod_url.strip():
        log_json(logger, logging.INFO, "delhivery_sap_pod_skip", reason="no_pod_url", lrnum=resolved_lrnum)
        _safe_save_pod_sap_audit(
            pool,
            pod_row_id,
            {
                "ok": False,
                "phase": "skip",
                "reason": "no_pod_url",
                "lrnum": resolved_lrnum,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return

    try:
        with pool.connection() as conn:
            doc_entry_raw, db_delhivery_status = fetch_doc_entry_and_delhivery_status_for_lr(
                conn, resolved_lrnum
            )
    except Exception:
        log_json(
            logger,
            logging.ERROR,
            "delhivery_sap_pod_doc_entry_failed",
            lrnum=resolved_lrnum,
            trace=traceback.format_exc(),
        )
        _safe_save_pod_sap_audit(
            pool,
            pod_row_id,
            {
                "ok": False,
                "phase": "doc_entry_query_error",
                "lrnum": resolved_lrnum,
                "error": traceback.format_exc()[-1500:],
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return

    if not doc_entry_raw:
        log_json(logger, logging.INFO, "delhivery_sap_pod_skip", reason="no_doc_entry", lrnum=resolved_lrnum)
        _safe_save_pod_sap_audit(
            pool,
            pod_row_id,
            {
                "ok": False,
                "phase": "skip",
                "reason": "no_doc_entry",
                "lrnum": resolved_lrnum,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return

    de_s = str(doc_entry_raw).strip()
    if not re.fullmatch(r"-?\d+", de_s):
        log_json(logger, logging.WARNING, "delhivery_sap_pod_skip", reason="invalid_doc_entry", doc_entry=de_s)
        _safe_save_pod_sap_audit(
            pool,
            pod_row_id,
            {
                "ok": False,
                "phase": "skip",
                "reason": "invalid_doc_entry",
                "doc_entry_raw": de_s[:64],
                "lrnum": resolved_lrnum,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return
    doc_entry = int(de_s)

    try:
        if sap_sl_use_ssm():
            opener, base = build_opener_and_base_via_ssm()
        else:
            opener, base, st_login = build_opener_and_base_from_env()
            if st_login != 200:
                _safe_save_pod_sap_audit(
                    pool,
                    pod_row_id,
                    {
                        "ok": False,
                        "phase": "login",
                        "http_status": st_login,
                        "doc_entry": doc_entry,
                        "lrnum": resolved_lrnum,
                        "synced_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                log_json(logger, logging.WARNING, "delhivery_sap_pod_login_bad_status", status=st_login)
                return

        get_url = f"{base}/Invoices({doc_entry})?$select=DocEntry,U_LRtrackno"
        st_get, inv = sap_sl_request_json(opener, "GET", get_url, None)
        if st_get != 200 or not isinstance(inv, dict):
            log_json(
                logger,
                logging.WARNING,
                "delhivery_sap_pod_skip",
                reason="invoice_get_unexpected",
                doc_entry=doc_entry,
                http_status=st_get,
            )
            _safe_save_pod_sap_audit(
                pool,
                pod_row_id,
                {
                    "ok": False,
                    "phase": "invoice_get",
                    "http_status": st_get,
                    "doc_entry": doc_entry,
                    "lrnum": resolved_lrnum,
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return

        sap_lr = inv.get("U_LRtrackno")
        if str(sap_lr or "").strip() != str(resolved_lrnum).strip():
            log_json(
                logger,
                logging.WARNING,
                "delhivery_sap_pod_lr_mismatch",
                lrnum=resolved_lrnum,
                sap_u_lrtrackno=sap_lr,
                doc_entry=doc_entry,
            )
            _safe_save_pod_sap_audit(
                pool,
                pod_row_id,
                {
                    "ok": False,
                    "phase": "lr_mismatch",
                    "doc_entry": doc_entry,
                    "expected_lrnum": str(resolved_lrnum).strip(),
                    "sap_u_lrtrackno": sap_lr,
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return

        pod_date = fields.get("pod_date")
        u_pod_date: str | None
        if pod_date is not None and hasattr(pod_date, "isoformat"):
            u_pod_date = pod_date.isoformat()
        else:
            u_pod_date = None

        frappe_grn_key = frappe_grn_key_from_delhivery_status(db_delhivery_status)

        patch_body: dict[str, Any] = {
            "U_Pod_Link": pod_url.strip(),
            "U_LRtrackno": str(resolved_lrnum).strip(),
            "U_POD_DATE": u_pod_date,
        }
        if frappe_grn_key is not None:
            patch_body["U_FrappeGRNKey"] = frappe_grn_key

        patch_url = f"{base}/Invoices({doc_entry})"
        st_patch, patch_body_out = sap_sl_request_json(opener, "PATCH", patch_url, patch_body)
        ok_http = st_patch in (200, 204)

        log_json(
            logger,
            logging.INFO,
            "delhivery_sap_pod_patched",
            doc_entry=doc_entry,
            lrnum=resolved_lrnum,
            has_pod_date=u_pod_date is not None,
            u_frappe_grn_key=frappe_grn_key,
            http_status=st_patch,
        )
        _safe_save_pod_sap_audit(
            pool,
            pod_row_id,
            {
                "ok": ok_http,
                "phase": "patch",
                "http_status": st_patch,
                "doc_entry": doc_entry,
                "lrnum": resolved_lrnum,
                "has_pod_date": u_pod_date is not None,
                "u_frappe_grn_key": frappe_grn_key,
                "response_preview": (
                    json.dumps(patch_body_out)[:800]
                    if isinstance(patch_body_out, (dict, list))
                    else (str(patch_body_out)[:800] if patch_body_out is not None else None)
                ),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        log_json(
            logger,
            logging.ERROR,
            "delhivery_sap_pod_patch_failed",
            lrnum=resolved_lrnum,
            doc_entry=doc_entry,
            trace=traceback.format_exc(),
        )
        _safe_save_pod_sap_audit(
            pool,
            pod_row_id,
            {
                "ok": False,
                "phase": "error",
                "doc_entry": doc_entry,
                "lrnum": resolved_lrnum,
                "error": traceback.format_exc()[-1500:],
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        )
