"""Lambda entry: EventBridge schedule or manual invoke."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# Lambda package root is ``src/``; local modules use ``from walmart.*``.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from khanal_shared.config import get_settings
from khanal_shared.db.pool import ensure_warmed_for_lambda
from khanal_shared.logging import configure_logging, log_json

from walmart.credentials import DATABASE_URL, WM_BASIC_AUTH
from walmart.order_creation import DEFAULT_LIMIT, DEFAULT_SHIP_NODE_TYPE, run_creation_sync
from walmart.order_updation import run_updation_sync
from walmart.returns_fetch import run_returns_backfill, run_returns_sync
from walmart.sap_invoice_sync import (
    DEFAULT_RETRY_LIMIT,
    DEFAULT_SAP_POST_LIMIT,
    run_sap_invoice_post_sync,
    run_sap_invoice_retry_sync,
)
from walmart.sap_credit_note_sync import (
    DEFAULT_CN_POST_LIMIT,
    DEFAULT_CN_RETRY_LIMIT,
    run_sap_cn_post_sync,
    run_sap_cn_retry_sync,
)

logger = logging.getLogger(__name__)


def _ship_node_type(event: dict[str, Any]) -> str:
    return (
        (event.get("ship_node_type") or os.environ.get("WM_SHIP_NODE_TYPE") or DEFAULT_SHIP_NODE_TYPE)
        .strip()
    )


def _limit(event: dict[str, Any]) -> int:
    raw = event.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    return int(raw)


def _validate_config(mode: str) -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    if mode in ("updation", "creation", "returns_backfill", "returns_sync") and not WM_BASIC_AUTH:
        raise RuntimeError("WM_BASIC_AUTH is not set")


def handler(event: dict[str, Any] | None, context: object) -> dict[str, Any]:
    configure_logging(get_settings().log_level)
    if os.environ.get("AWS_EXECUTION_ENV"):
        ensure_warmed_for_lambda()

    payload = event or {}
    mode = (payload.get("mode") or "updation").strip().lower()
    _validate_config(mode)

    ship_node_type = _ship_node_type(payload)
    limit = _limit(payload)
    store = not bool(payload.get("no_store"))

    try:
        if mode == "sap_cn_retry":
            retry_limit = int(payload.get("limit") or DEFAULT_CN_RETRY_LIMIT)
            output = run_sap_cn_retry_sync(
                return_order_id=(payload.get("return_order_id") or "").strip() or None,
                limit=retry_limit,
                post_to_sap=not bool(payload.get("no_post")),
            )
            log_json(
                logger,
                logging.INFO,
                "walmart_sap_cn_retry_complete",
                mode=mode,
                candidates=output.get("candidates"),
                posted=output.get("posted"),
                failed=output.get("failed"),
                skipped=output.get("skipped"),
            )
            return {"ok": True, "mode": mode, **output}
        if mode == "sap_cn_post":
            post_limit = int(payload.get("limit") or DEFAULT_CN_POST_LIMIT)
            output = run_sap_cn_post_sync(
                return_order_id=(payload.get("return_order_id") or "").strip() or None,
                limit=post_limit,
                post_to_sap=not bool(payload.get("no_post")),
            )
            log_json(
                logger,
                logging.INFO,
                "walmart_sap_cn_post_complete",
                mode=mode,
                candidates=output.get("candidates"),
                posted=output.get("posted"),
                prepared=output.get("prepared"),
                failed=output.get("failed"),
                skipped=output.get("skipped"),
            )
            return {"ok": True, "mode": mode, **output}
        if mode == "sap_retry":
            retry_limit = int(payload.get("limit") or DEFAULT_RETRY_LIMIT)
            output = run_sap_invoice_retry_sync(
                purchase_order_id=(payload.get("purchase_order_id") or "").strip() or None,
                limit=retry_limit,
                post_to_sap=not bool(payload.get("no_post")),
            )
            log_json(
                logger,
                logging.INFO,
                "walmart_sap_retry_complete",
                mode=mode,
                candidates=output.get("candidates"),
                posted=output.get("posted"),
                failed=output.get("failed"),
                skipped=output.get("skipped"),
            )
            return {"ok": True, "mode": mode, **output}
        if mode == "sap_post":
            post_limit = int(payload.get("limit") or DEFAULT_SAP_POST_LIMIT)
            output = run_sap_invoice_post_sync(
                purchase_order_id=(payload.get("purchase_order_id") or "").strip() or None,
                limit=post_limit,
                post_to_sap=not bool(payload.get("no_post")),
            )
            log_json(
                logger,
                logging.INFO,
                "walmart_sap_post_complete",
                mode=mode,
                candidates=output.get("candidates"),
                posted=output.get("posted"),
                prepared=output.get("prepared"),
                failed=output.get("failed"),
                skipped=output.get("skipped"),
            )
            return {"ok": True, "mode": mode, **output}
        if mode == "updation":
            output = run_updation_sync(
                last_modified_start_date=payload.get("last_modified_start_date"),
                last_modified_end_date=payload.get("last_modified_end_date"),
                ship_node_type=ship_node_type,
                limit=limit,
                store=store,
            )
        elif mode == "creation":
            start = payload.get("created_start_date")
            end = payload.get("created_end_date")
            if not start or not end:
                raise ValueError(
                    "creation mode requires created_start_date and created_end_date in the event"
                )
            output = run_creation_sync(
                created_start_date=str(start),
                created_end_date=str(end),
                ship_node_type=ship_node_type,
                limit=limit,
                store=store,
            )
        elif mode == "returns_backfill":
            start = payload.get("start_date")
            end = payload.get("end_date")
            if not start or not end:
                raise ValueError(
                    "returns_backfill mode requires start_date and end_date in the event"
                )
            output = run_returns_backfill(
                start_date=str(start),
                end_date=str(end),
                store=store,
            )
        elif mode == "returns_sync":
            output = run_returns_sync(
                start_date=(payload.get("start_date") or "").strip() or None,
                end_date=(payload.get("end_date") or "").strip() or None,
                store=store,
            )
        else:
            raise ValueError(
                f"Unknown mode: {mode!r} "
                "(use 'updation', 'creation', 'sap_post', 'sap_retry', "
                "'sap_cn_post', 'sap_cn_retry', 'returns_backfill', or 'returns_sync')"
            )

        if mode in ("returns_backfill", "returns_sync"):
            log_json(
                logger,
                logging.INFO,
                "walmart_returns_sync_complete",
                mode=mode,
                total_returns=output.get("total_returns"),
                inserted=output.get("inserted"),
                updated=output.get("updated"),
                skipped=output.get("skipped"),
            )
            return {"ok": True, "mode": mode, **output}

        log_json(
            logger,
            logging.INFO,
            "walmart_sync_complete",
            mode=mode,
            total_orders=output.get("total_orders"),
            total_order_lines=output.get("total_order_lines"),
            pages_fetched=output.get("pages_fetched"),
            log_id=(output.get("db") or {}).get("log_id"),
            sap_invoice_results=(output.get("db") or {}).get("sap_invoice_results"),
        )
        return {"ok": True, "mode": mode, **output}
    except Exception:
        log_json(logger, logging.ERROR, "walmart_sync_error", mode=mode, trace=traceback.format_exc())
        raise
