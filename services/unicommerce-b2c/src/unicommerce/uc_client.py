"""Unicommerce REST API client.

Ported from khanal_tech_integrations/unicommerce/client/unicommerce.py
(Frappe). Logs in fresh once per Lambda invocation -- matches how the
Frappe worker calls ``uc.login()`` at the top of each scheduled run, and
keeps this simple (no cross-invocation token cache to reason about).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from unicommerce.credentials import UcCredentials

logger = logging.getLogger(__name__)

_TIMEOUT = 30


class UcClient:
    def __init__(self, creds: UcCredentials):
        self.base_url = creds.base_url
        self.username = creds.username
        self.password = creds.password
        self.facility_code = creds.facility_code
        self.token: str | None = None

    def login(self) -> None:
        """Full password grant -- always fetches a fresh token from UC."""
        resp = requests.post(
            f"{self.base_url}/oauth/token",
            data={
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "client_id": "my-trusted-client",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]

    def _resolve_facility_codes(self) -> list[str]:
        if not self.facility_code:
            return []
        return [f.strip() for f in self.facility_code.split(",") if f.strip()]

    def _headers(self) -> dict[str, str]:
        if not self.token:
            self.login()
        return {"Authorization": f"Bearer {self.token}"}

    def _get_with_reauth(self, endpoint: str, params: dict) -> dict:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=_TIMEOUT)
        if resp.status_code == 401:
            self.login()
            resp = requests.get(url, headers=self._headers(), params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _post_with_reauth(self, endpoint: str, body: dict, extra_headers: dict | None = None) -> dict:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        resp = requests.post(url, headers=headers, json=body, timeout=_TIMEOUT)
        if resp.status_code == 401:
            self.login()
            headers = self._headers()
            if extra_headers:
                headers.update(extra_headers)
            resp = requests.post(url, headers=headers, json=body, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _to_utc_iso(dt_value: str) -> str:
        """UC saleOrder/search wants 'yyyy-MM-dd'T'HH:mm:ss'Z''. Inputs here
        are always naive UTC strings already (fetch_sync builds them that
        way), so no timezone conversion is needed -- unlike the Frappe
        client, which converted from IST because Frappe's now_datetime()
        is IST-local."""
        dt = datetime.strptime(dt_value[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _to_uc_date(dt_value: str) -> str:
        dt = datetime.strptime(dt_value[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def get_orders_between(
        self, from_date: str, to_date: str, channel: str | None = None
    ) -> list[dict]:
        """Fetch sale orders between explicit UTC datetime bounds
        ('YYYY-MM-DD HH:MM:SS' strings)."""
        all_orders: list[dict] = []
        page_size = 1000
        display_start = 0
        total_records = None
        max_pages = 200
        prev_page_first_code = None

        for _page in range(max_pages):
            body: dict[str, Any] = {
                "fromDate": self._to_utc_iso(from_date),
                "toDate": self._to_utc_iso(to_date),
                "searchOptions": {
                    "displayLength": page_size,
                    "displayStart": display_start,
                    "getCount": True,
                },
            }
            facility_codes = self._resolve_facility_codes()
            if facility_codes:
                body["facilityCodes"] = facility_codes
            if channel:
                body["channel"] = channel

            data = self._post_with_reauth("services/rest/v1/oms/saleOrder/search", body=body)
            elements = data.get("elements", []) if isinstance(data, dict) else []
            current_first_code = (elements[0] or {}).get("code") if elements else None
            if prev_page_first_code and current_first_code and current_first_code == prev_page_first_code:
                break
            prev_page_first_code = current_first_code
            all_orders.extend(elements)

            if total_records is None and isinstance(data, dict):
                total_records = data.get("totalRecords") or data.get("totalElements")

            if len(elements) < page_size:
                break
            if total_records and len(all_orders) >= int(total_records):
                break
            display_start += page_size

        return all_orders

    def get_order_detail(self, order_code: str) -> dict:
        body: dict[str, Any] = {"code": order_code}
        facility_codes = self._resolve_facility_codes()
        if facility_codes:
            body["facilityCodes"] = facility_codes
        data = self._post_with_reauth("services/rest/v1/oms/saleorder/get", body=body)
        if isinstance(data, dict) and data.get("saleOrderDTO"):
            return data["saleOrderDTO"]
        return data if isinstance(data, dict) else {}

    def _search_returns_single(
        self, return_type: str, from_uc: str, to_uc: str, use_updated: bool, facility: str
    ) -> list[dict]:
        body: dict = {"returnType": return_type}
        if use_updated:
            body["updatedFrom"] = from_uc
            body["updatedTo"] = to_uc
        else:
            body["createdFrom"] = from_uc
            body["createdTo"] = to_uc

        url = f"{self.base_url}/services/rest/v1/oms/return/search"
        headers = {**self._headers(), "Facility": facility}

        resp = requests.post(url, headers=headers, json=body, timeout=_TIMEOUT)
        if resp.status_code == 401:
            self.login()
            headers = {**self._headers(), "Facility": facility}
            resp = requests.post(url, headers=headers, json=body, timeout=_TIMEOUT)

        if not resp.ok:
            logger.warning(
                "UC return/search facility=%s type=%s status=%s", facility, return_type, resp.status_code
            )
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        rows = data.get("returnOrders") or []
        for row in rows:
            row["_facility"] = facility
        return rows

    def _search_returns_by_type(
        self, return_type: str, from_uc: str, to_uc: str, use_updated: bool, facility_codes: list[str]
    ) -> list[dict]:
        all_rows: list[dict] = []
        for fac in facility_codes or [""]:
            all_rows.extend(self._search_returns_single(return_type, from_uc, to_uc, use_updated, fac))
        return all_rows

    def get_returns_between(
        self, from_date: str, to_date: str, use_updated: bool = False
    ) -> list[dict]:
        """Full return details between UTC datetime bounds. Splits into
        29-day chunks (UC enforces a max 30-day search window), then fetches
        full detail for each unique summary code."""
        from_dt = datetime.strptime(from_date[:19], "%Y-%m-%d %H:%M:%S")
        to_dt = datetime.strptime(to_date[:19], "%Y-%m-%d %H:%M:%S")
        facility_codes = self._resolve_facility_codes()

        chunks: list[tuple[datetime, datetime]] = []
        chunk_start = from_dt
        while chunk_start < to_dt:
            from datetime import timedelta

            chunk_end = chunk_start + timedelta(days=29)
            if chunk_end > to_dt:
                chunk_end = to_dt
            chunks.append((chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(seconds=1)

        code_to_facility: dict[str, str] = {}
        for chunk_from, chunk_to in chunks:
            from_uc = self._to_uc_date(chunk_from.strftime("%Y-%m-%d %H:%M:%S"))
            to_uc = self._to_uc_date(chunk_to.strftime("%Y-%m-%d %H:%M:%S"))
            for rtype in ("CIR", "RTO"):
                summaries = self._search_returns_by_type(rtype, from_uc, to_uc, use_updated, facility_codes)
                for row in summaries:
                    code = row.get("code") or row.get("reversePickupCode")
                    if code and code not in code_to_facility:
                        code_to_facility[str(code)] = row.get("_facility") or ""

        full_returns: list[dict] = []
        for code, fac in code_to_facility.items():
            try:
                detail = self.get_return_detail(code, facility=fac or None)
                if detail:
                    full_returns.append(detail)
            except Exception as e:
                logger.warning("UC return/get failed code=%s error=%s", code, e)
        return full_returns

    def get_return_detail(self, code: str, facility: str | None = None) -> dict:
        """RP-prefixed codes -> reversePickupCode; other codes (RTO search
        results) -> shipmentCode."""
        extra_headers = {"Facility": facility} if facility else None
        if str(code).upper().startswith("RP"):
            body = {"reversePickupCode": code}
        else:
            body = {"shipmentCode": code}

        data = self._post_with_reauth(
            "services/rest/v1/oms/return/get", body=body, extra_headers=extra_headers
        )
        if not isinstance(data, dict):
            return {}
        if not data.get("successful", True) and not data.get("returnSaleOrderValue"):
            return {}
        return data


def utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
