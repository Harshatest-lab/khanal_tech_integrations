"""Unicommerce B2C: Unicommerce REST API -> SAP B1 pipeline (India entity).

SAP profile: unset / default (SAP_SL_PROFILE not set) -> /sap/* SSM params,
same entity delivery-api already uses -- confirmed 2026-07-23 via a direct
openssl s_client check against the SAP Service Layer host
(103.69.202.108:50000, self-signed cert CN=INHYDMSRDVSA135), the same host
the Frappe Unicommerce integration talks to. NOT amazon-us-b2c/B2C_Invoice's
/sap/inc/* (US entity).

Ported from khanal_tech_integrations/unicommerce/ (Frappe). See
docs/migration/03-unicommerce-b2c.md for the full architecture and the
7-function build plan; only the fetch worker is implemented so far.
"""

ORDER_DOCTYPE_TABLE = "unicommerce_orders"
ORDER_LINES_TABLE = "unicommerce_order_lines"

# Matches the Frappe fetch_orders semantics exactly (khanal_tech_integrations
# unicommerce/workers/fetch.py) for dual-run comparability:
#   - pipeline_status == 'posted' -> skip entirely, no refresh at all.
#   - anything else -> always refresh order_json + rebuild line items;
#     pipeline_status only gets overwritten to 'fetched' if it's currently
#     '' / 'fetched' / 'ready' (a re-fetch must never reset an in-flight
#     'posting'/'batch_hold'/'verify'/'failed'/'dead' order back to
#     'fetched' underneath whatever's processing it).
STATUS_SKIP_ON_REFETCH = "posted"
STATUS_OVERWRITABLE_ON_REFETCH = ("", "fetched", "ready")
