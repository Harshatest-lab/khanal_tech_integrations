"""SAP B1 Service Layer writes: PATCH AR Invoices / Delivery Notes with
waybill + POD info.

Ported from safexpress2/sap_integration.py, rebuilt on
khanal_shared.sap_sl (the same SSM-cached session machinery
delivery-api/amazon-us-b2c/unicommerce-b2c already share) instead of a
one-off AuthenticateSAPB1() call per request.
"""

from __future__ import annotations

import logging

from khanal_shared.sap_sl import build_opener_and_base_via_ssm, sap_sl_request_json
from khanal_shared.sap_sl.ssm_session import param_b1_session

from .config import (
    SAP_FIELD_POD_DATE,
    SAP_FIELD_POD_LINK,
    SAP_FIELD_TRACKING_NUMBER,
    SAP_FIELD_TRANSPORTATION_CODE,
    SAP_FIELD_TRANSPORTER_NAME,
    SAP_TRANSPORTATION_CODE_SAFEXPRESS,
    TRANSPORTER_NAME_SAFEXPRESS,
)
from .exceptions import SafexpressSAPIntegrationError

logger = logging.getLogger(__name__)


def _invalidate_cached_session() -> None:
    import json

    import boto3

    try:
        ssm = boto3.client("ssm")
        name = param_b1_session()
        raw = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        data = json.loads(raw)
        data["expires_on"] = "1970-01-01T00:00:00+00:00"
        data["is_locked"] = False
        ssm.put_parameter(Name=name, Value=json.dumps(data), Type="SecureString", Overwrite=True)
    except Exception:
        logger.warning("Could not invalidate cached SAP session", exc_info=True)


def _patch(path: str, body: dict) -> None:
    """PATCH with one retry on a stale cached session (401) — same pattern
    amazon-us-b2c's sap_client._request uses."""
    opener, base = build_opener_and_base_via_ssm()
    status, resp_body = sap_sl_request_json(opener, "PATCH", f"{base}/{path}", body=body)
    if status == 401:
        logger.info("SAP session stale (401); re-logging in")
        _invalidate_cached_session()
        opener, base = build_opener_and_base_via_ssm()
        status, resp_body = sap_sl_request_json(opener, "PATCH", f"{base}/{path}", body=body)
    if status not in (200, 204):
        raise SafexpressSAPIntegrationError(f"SAP PATCH {path} failed: HTTP {status}: {resp_body!r}")


def update_invoice_with_pod(doc_entry: int, waybill_number: str, pod_url: str, pod_date: str | None = None) -> None:
    payload = {
        SAP_FIELD_TRANSPORTER_NAME: TRANSPORTER_NAME_SAFEXPRESS,
        SAP_FIELD_TRANSPORTATION_CODE: SAP_TRANSPORTATION_CODE_SAFEXPRESS,
        SAP_FIELD_TRACKING_NUMBER: waybill_number,
        SAP_FIELD_POD_LINK: pod_url,
    }
    if pod_date:
        payload[SAP_FIELD_POD_DATE] = pod_date
    try:
        _patch(f"Invoices({int(doc_entry)})", payload)
        logger.info("Updated SAP invoice %s with POD %s", doc_entry, pod_url)
    except SafexpressSAPIntegrationError:
        raise
    except Exception as e:
        raise SafexpressSAPIntegrationError(f"Error updating SAP invoice {doc_entry} with POD: {e}") from e


def update_delivery_note_with_tracking(doc_entry: int, waybill_number: str) -> None:
    payload = {
        SAP_FIELD_TRANSPORTER_NAME: TRANSPORTER_NAME_SAFEXPRESS,
        SAP_FIELD_TRACKING_NUMBER: waybill_number,
    }
    try:
        _patch(f"DeliveryNotes({int(doc_entry)})", payload)
        logger.info("Updated SAP delivery note %s with tracking %s", doc_entry, waybill_number)
    except SafexpressSAPIntegrationError:
        raise
    except Exception as e:
        raise SafexpressSAPIntegrationError(
            f"Error updating SAP delivery note {doc_entry} with tracking: {e}"
        ) from e
