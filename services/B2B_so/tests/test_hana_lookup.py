from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hana_lookup


class _Cur:
    def __init__(self, batches: list[list[tuple]]):
        self._batches = list(batches)
        self.description = []

    def execute(self, sql, params=None):
        self._sql = sql
        self._params = params
        batch = self._batches.pop(0)
        if batch and isinstance(batch[0], tuple) and batch[0] and isinstance(batch[0][0], str) and batch[0][0] == "__cols__":
            self.description = [(c,) for c in batch[0][1:]]
            self._rows = batch[1:]
        else:
            self._rows = batch

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def test_get_business_partner_maps_crd1_like_service_layer():
    header = [
        ("__cols__", "CardCode", "CardName", "FederalTaxID", "GSTIN"),
        ("C03570", "BLINK COMMERCE PR", None, None),
    ]
    addrs = [
        ("__cols__", "AddressName", "AddressType", "Street", "Block", "City", "State", "ZipCode", "Country", "GSTIN"),
        ("BCPL Chennai C5", "S", "St", None, "Chennai", "TN", None, "IN", "33AAFCG9846E1ZI"),
        ("BCPL Chennai bill", "B", "St", None, "Chennai", "TN", None, "IN", None),
    ]
    cur = _Cur([header, addrs])
    params = SimpleNamespace(schema="FINAL_AUDIT")
    cm = MagicMock()
    cm.__enter__.return_value = _Conn(cur)
    cm.__exit__.return_value = False
    with patch("hana_lookup.get_hana_params", return_value=params), patch(
        "hana_lookup.hana_connection", return_value=cm
    ):
        bp = hana_lookup.get_business_partner("C03570")
    assert bp["CardCode"] == "C03570"
    assert bp["BPAddresses"][0]["AddressName"] == "BCPL Chennai C5"
    assert bp["BPAddresses"][0]["State"] == "TN"
    assert bp["BPAddresses"][0]["GSTIN"] == "33AAFCG9846E1ZI"
    assert bp["BPAddresses"][1]["GSTIN"] == ""  # header GST empty, no fallback fill from None


def test_get_items_returns_tax_udfs():
    rows = [
        ("__cols__", "ItemCode", "ItemName", "U_TaxRate", "U_GstTax", "U_IgstTax"),
        ("FGDV0001", "Dogsee Veda Tea Tree Shampoo - 200ML", 18, "GST@18", "IGST@18"),
    ]
    cur = _Cur([rows])
    params = SimpleNamespace(schema=None)
    cm = MagicMock()
    cm.__enter__.return_value = _Conn(cur)
    cm.__exit__.return_value = False
    with patch("hana_lookup.get_hana_params", return_value=params), patch(
        "hana_lookup.hana_connection", return_value=cm
    ):
        found = hana_lookup.get_items(["FGDV0001"])
    assert found["FGDV0001"]["ItemName"].startswith("Dogsee")
    assert found["FGDV0001"]["U_TaxRate"] == 18
    assert found["FGDV0001"]["U_GstTax"] == "GST@18"
    assert found["FGDV0001"]["U_IgstTax"] == "IGST@18"


def test_get_items_falls_back_to_service_layer_when_hana_has_no_tax():
    hana_rows = [
        ("__cols__", "ItemCode", "ItemName", "U_TaxRate", "U_GstTax", "U_IgstTax"),
        ("FGHN0065", "Ghee", None, None, None),
    ]
    cur = _Cur([hana_rows])
    params = SimpleNamespace(schema="FINAL_AUDIT")
    cm = MagicMock()
    cm.__enter__.return_value = _Conn(cur)
    cm.__exit__.return_value = False
    sl_body = {
        "value": [
            {
                "ItemCode": "FGHN0065",
                "ItemName": "Ghee",
                "U_TaxRate": 5,
                "U_GstTax": "GST@5",
                "U_IgstTax": "IGST@5",
            }
        ]
    }

    def fake_sl(opener, method, url, body=None):
        return 200, sl_body

    with patch("hana_lookup.get_hana_params", return_value=params), patch(
        "hana_lookup.hana_connection", return_value=cm
    ), patch(
        "khanal_shared.sap_sl.build_opener_and_base_via_ssm",
        return_value=(None, "https://sl.test/b1s/v1"),
    ), patch("khanal_shared.sap_sl.sap_sl_request_json", side_effect=fake_sl):
        found = hana_lookup.get_items(["FGHN0065"])
    assert found["FGHN0065"]["U_TaxRate"] == 5
