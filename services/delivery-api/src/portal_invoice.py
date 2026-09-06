"""SAP HANA AR invoice search for the logistics portal."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from khanal_shared.hana import get_hana_params, hana_connection, qualified_table


class PortalValidationError(Exception):
    pass


_MAX_STR = 200


def _serialize_value(val: Any) -> Any:
    if val is None:
        return None
    if hasattr(val, "isoformat") and callable(val.isoformat):
        try:
            return val.isoformat()
        except Exception:
            return str(val)
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (bytes, bytearray, memoryview)):
        try:
            return bytes(val).decode("utf-8", errors="replace")
        except Exception:
            return None
    return val


def _clip_str(val: Any, label: str) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if len(s) > _MAX_STR:
        raise PortalValidationError(f"{label} is too long (max {_MAX_STR} characters).")
    return s


def _parse_doc_num_optional(raw: Any) -> Optional[int]:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        n = int(str(raw).strip())
    except ValueError:
        raise PortalValidationError("DocNum must be a whole number.")
    if n < 0 or n > 999999999:
        raise PortalValidationError("DocNum is out of range.")
    return n


def _parse_doc_entry_filter(raw: Any) -> int:
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        de = int(str(raw).strip())
    except ValueError:
        raise PortalValidationError("DocEntry must be a whole number.")
    if de <= 0 or de > 999999999:
        raise PortalValidationError("DocEntry is out of range.")
    return de


def _schema() -> str:
    params = get_hana_params()
    if not params or not params.schema:
        raise PortalValidationError("HANA schema is not configured.")
    return params.schema


def search_ar_invoices(
    *,
    doc_num: Any = None,
    series_name: Any = None,
    doc_date: Any = None,
    card_code: Any = None,
    card_name: Any = None,
    doc_entry: Any = None,
) -> dict[str, Any]:
    if not get_hana_params():
        raise PortalValidationError("HANA is not configured (set HANA_USE_SSM and /sap/hana_config).")

    num = _parse_doc_num_optional(doc_num)
    doc_entry_f = _parse_doc_entry_filter(doc_entry)
    if num is None and not doc_entry_f:
        raise PortalValidationError("Enter DocNum and/or DocEntry to search.")

    series_f = _clip_str(series_name, "Series name")
    card_c = _clip_str(card_code, "Card code")
    card_n = _clip_str(card_name, "Card name")
    date_val = _clip_str(doc_date, "Doc date") if doc_date not in (None, "") else None

    schema = _schema()
    oinv = qualified_table(schema, "OINV", "T0")
    nnm1 = qualified_table(schema, "NNM1", "T2")

    sql = f"""
SELECT TOP 50
    T0."DocEntry",
    T0."DocNum",
    T2."SeriesName",
    T0."DocDate",
    T0."CardCode",
    T0."CardName",
    T0."DocTotal"
FROM {oinv}
LEFT JOIN {nnm1} ON T0."Series" = T2."Series"
WHERE 1=1
"""
    params: list[Any] = []
    if num is not None and doc_entry_f:
        sql += ' AND T0."DocNum" = ? AND T0."DocEntry" = ?'
        params.extend([num, doc_entry_f])
    elif num is not None:
        sql += ' AND T0."DocNum" = ?'
        params.append(num)
    else:
        sql += ' AND T0."DocEntry" = ?'
        params.append(doc_entry_f)
    if date_val is not None:
        sql += ' AND T0."DocDate" = ?'
        params.append(date_val)
    if series_f is not None:
        sql += ' AND T2."SeriesName" = ?'
        params.append(series_f)
    if card_c is not None:
        sql += ' AND T0."CardCode" = ?'
        params.append(card_c)
    if card_n is not None:
        sql += ' AND T0."CardName" = ?'
        params.append(card_n)
    sql += ' ORDER BY T0."DocDate" DESC'

    keys = (
        "doc_entry",
        "doc_num",
        "series_name",
        "doc_date",
        "card_code",
        "card_name",
        "doc_total",
    )
    out: list[dict[str, Any]] = []
    try:
        with hana_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            for row in cur.fetchall():
                out.append({k: _serialize_value(v) for k, v in zip(keys, row)})
    except PortalValidationError:
        raise
    except Exception as exc:
        raise PortalValidationError(f"HANA invoice search failed: {exc}") from exc
    return {"invoices": out}


def get_ar_invoice_detail(doc_entry: int, include_lines: bool = True) -> dict[str, Any]:
    if not get_hana_params():
        raise PortalValidationError("HANA is not configured.")
    de = int(doc_entry)
    if not de:
        raise PortalValidationError("DocEntry is required.")

    schema = _schema()
    oinv = qualified_table(schema, "OINV", "T0")
    inv1 = qualified_table(schema, "INV1", "T1")
    nnm1 = qualified_table(schema, "NNM1", "T2")

    include_u_lr = True
    if include_lines:
        sql = f"""
SELECT
    T0."DocEntry", T0."DocNum", T2."SeriesName", T0."DocDate",
    T0."CardCode", T0."CardName", T0."DocTotal", T0."U_LRtrackno",
    T1."LineNum", T1."ItemCode", T1."Dscription", T1."Quantity", T1."Price", T1."LineTotal"
FROM {oinv}
INNER JOIN {inv1} ON T0."DocEntry" = T1."DocEntry"
LEFT JOIN {nnm1} ON T0."Series" = T2."Series"
WHERE T0."DocEntry" = ?
ORDER BY T1."LineNum"
"""
    else:
        sql = f"""
SELECT
    T0."DocEntry", T0."DocNum", T2."SeriesName", T0."DocDate",
    T0."CardCode", T0."CardName", T0."DocTotal", T0."U_LRtrackno"
FROM {oinv}
LEFT JOIN {nnm1} ON T0."Series" = T2."Series"
WHERE T0."DocEntry" = ?
"""

    rows: list[tuple] = []
    with hana_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql, (de,))
            rows = cur.fetchall()
        except Exception as exc:
            msg_l = str(exc).lower()
            if "u_lrtrackno" in msg_l and include_u_lr:
                include_u_lr = False
                if include_lines:
                    sql = f"""
SELECT
    T0."DocEntry", T0."DocNum", T2."SeriesName", T0."DocDate",
    T0."CardCode", T0."CardName", T0."DocTotal",
    T1."LineNum", T1."ItemCode", T1."Dscription", T1."Quantity", T1."Price", T1."LineTotal"
FROM {oinv}
INNER JOIN {inv1} ON T0."DocEntry" = T1."DocEntry"
LEFT JOIN {nnm1} ON T0."Series" = T2."Series"
WHERE T0."DocEntry" = ?
ORDER BY T1."LineNum"
"""
                else:
                    sql = f"""
SELECT
    T0."DocEntry", T0."DocNum", T2."SeriesName", T0."DocDate",
    T0."CardCode", T0."CardName", T0."DocTotal"
FROM {oinv}
LEFT JOIN {nnm1} ON T0."Series" = T2."Series"
WHERE T0."DocEntry" = ?
"""
                cur.execute(sql, (de,))
                rows = cur.fetchall()
            else:
                raise

    if not rows:
        raise PortalValidationError("No invoice found for this DocEntry.")

    first = rows[0]
    if include_lines:
        if include_u_lr:
            header = {
                "doc_entry": _serialize_value(first[0]),
                "doc_num": _serialize_value(first[1]),
                "series_name": _serialize_value(first[2]),
                "doc_date": _serialize_value(first[3]),
                "card_code": _serialize_value(first[4]),
                "card_name": _serialize_value(first[5]),
                "doc_total": _serialize_value(first[6]),
                "u_lrtrackno": _serialize_value(first[7]),
            }
            line_lo = 8
        else:
            header = {
                "doc_entry": _serialize_value(first[0]),
                "doc_num": _serialize_value(first[1]),
                "series_name": _serialize_value(first[2]),
                "doc_date": _serialize_value(first[3]),
                "card_code": _serialize_value(first[4]),
                "card_name": _serialize_value(first[5]),
                "doc_total": _serialize_value(first[6]),
                "u_lrtrackno": None,
            }
            line_lo = 7
        line_keys = ("line_num", "item_code", "dscription", "quantity", "price", "line_total")
        lines = [
            {k: _serialize_value(v) for k, v in zip(line_keys, r[line_lo:line_lo + 6])}
            for r in rows
        ]
    else:
        if include_u_lr:
            header = {
                "doc_entry": _serialize_value(first[0]),
                "doc_num": _serialize_value(first[1]),
                "series_name": _serialize_value(first[2]),
                "doc_date": _serialize_value(first[3]),
                "card_code": _serialize_value(first[4]),
                "card_name": _serialize_value(first[5]),
                "doc_total": _serialize_value(first[6]),
                "u_lrtrackno": _serialize_value(first[7]),
            }
        else:
            header = {
                "doc_entry": _serialize_value(first[0]),
                "doc_num": _serialize_value(first[1]),
                "series_name": _serialize_value(first[2]),
                "doc_date": _serialize_value(first[3]),
                "card_code": _serialize_value(first[4]),
                "card_name": _serialize_value(first[5]),
                "doc_total": _serialize_value(first[6]),
                "u_lrtrackno": None,
            }
        lines = []

    return {"header": header, "lines": lines}
