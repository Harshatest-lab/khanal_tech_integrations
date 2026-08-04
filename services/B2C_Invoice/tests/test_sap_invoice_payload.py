"""Tests for Walmart order charge parsing and SAP Invoice payload helpers."""

from unittest.mock import MagicMock, patch

from walmart.order_pricing import (
    item_price_per_unit,
    line_display_status,
    line_invoiceable_quantity,
    line_quantity,
    line_status_quantities,
    line_promotion_discount,
    net_unit_price,
    order_has_county_surcharge_tax,
    order_has_promotion_discount,
    parse_line_charges,
)
from walmart.dates import ms_to_pacific_date_str
from walmart.sap_invoice_payload import (
    SHIPPING_ITEM_CODE,
    TAX_ITEM_CODE,
    _order_date_pacific_str,
    _ship_date_pacific_str,
    build_sap_invoice_payload,
)
from walmart.walmart_store import _line_tracking, line_ship_date_time_ms, order_ship_date_time_ms, order_shipping_state


def test_parse_line_charges_product_only() -> None:
    line = {
        "charges": {
            "charge": [
                {
                    "chargeType": "PRODUCT",
                    "chargeName": "ItemPrice",
                    "chargeAmount": {"amount": 28.99, "currency": "USD"},
                    "tax": {"taxAmount": {"amount": 2.54}},
                }
            ]
        },
        "orderLineQuantity": {"amount": "1"},
    }
    charges = parse_line_charges(line)
    assert charges["item_price"] == 28.99
    assert charges["product_tax"] == 2.54
    assert charges["total_tax"] == 2.54
    assert len(charges["tax_lines"]) == 1
    assert charges["shipping"] is None
    assert line_quantity(line) == 1.0


def test_parse_line_charges_includes_fee_tax() -> None:
    line = {
        "charges": {
            "charge": [
                {
                    "chargeType": "PRODUCT",
                    "chargeName": "ItemPrice",
                    "chargeAmount": {"amount": 14.57, "currency": "USD"},
                    "tax": {"taxName": "Tax1", "taxAmount": {"amount": 0.61, "currency": "USD"}},
                },
                {
                    "chargeType": "FEE",
                    "chargeName": "Fee",
                    "chargeAmount": {"amount": 0, "currency": "USD"},
                    "taxAndOtherFees": {
                        "taxAndOtherFeesName": "County Surcharge Tax",
                        "taxAndOtherFeesAmount": {"amount": 0.08, "currency": "USD"},
                    },
                },
            ]
        },
        "orderLineQuantity": {"amount": "1"},
    }
    charges = parse_line_charges(line)
    assert charges["product_tax"] == 0.61
    assert charges["total_tax"] == 0.69
    assert [row["name"] for row in charges["tax_lines"]] == [
        "Product tax",
        "County Surcharge Tax",
    ]


def test_parse_line_charges_includes_promotion_discount() -> None:
    line = {
        "charges": {
            "charge": [
                {
                    "chargeType": "PRODUCT",
                    "chargeName": "ItemPrice",
                    "chargeAmount": {"amount": 9.7, "currency": "USD"},
                    "tax": {"taxName": "Tax1", "taxAmount": {"amount": 0.59, "currency": "USD"}},
                }
            ]
        },
        "refund": {
            "refundCharges": {
                "refundCharge": [
                    {
                        "charge": {
                            "chargeName": "Promotion",
                            "chargeType": "PRODUCT",
                            "chargeAmount": {"amount": -0.91, "currency": "USD"},
                        }
                    }
                ]
            }
        },
        "orderLineQuantity": {"amount": "1"},
    }
    charges = parse_line_charges(line)
    assert charges["item_price"] == 9.7
    assert charges["discount"] == 0.91
    assert charges["net_item_price"] == 8.79
    assert charges["total_tax"] == 0.59
    assert line_promotion_discount(line) == 0.91
    assert charges["gross_unit_price"] == 9.7
    assert charges["net_unit_price"] == 8.79


def test_net_unit_price_divides_discount_by_qty() -> None:
    assert net_unit_price(17.46, 0.92, 2) == 8.27
    assert net_unit_price(9.7, 0.91, 1) == 8.79


def test_item_price_per_unit_divides_by_qty() -> None:
    assert item_price_per_unit(20.41, 1) == 20.41
    assert item_price_per_unit(40.82, 2) == 20.41


def test_order_has_promotion_discount() -> None:
    order_with = {
        "orderLines": {
            "orderLine": [
                {
                    "refund": {
                        "refundCharges": {
                            "refundCharge": [
                                {
                                    "charge": {
                                        "chargeName": "Promotion",
                                        "chargeType": "PRODUCT",
                                        "chargeAmount": {"amount": -0.91},
                                    }
                                }
                            ]
                        }
                    }
                }
            ]
        }
    }
    order_without = {"orderLines": {"orderLine": [{"charges": {"charge": []}}]}}
    assert order_has_promotion_discount(order_with) is True
    assert order_has_promotion_discount(order_without) is False


def test_order_has_county_surcharge_tax() -> None:
    order_with = {
        "orderLines": {
            "orderLine": [
                {
                    "charges": {
                        "charge": [
                            {
                                "chargeType": "FEE",
                                "chargeName": "Fee",
                                "taxAndOtherFees": {
                                    "taxAndOtherFeesName": "County Surcharge Tax",
                                    "taxAndOtherFeesAmount": {"amount": 0.08},
                                },
                            }
                        ]
                    }
                }
            ]
        }
    }
    order_without = {"orderLines": {"orderLine": [{"charges": {"charge": []}}]}}
    assert order_has_county_surcharge_tax(order_with) is True
    assert order_has_county_surcharge_tax(order_without) is False


def test_order_date_pacific_str_uses_order_pacific() -> None:
    assert _order_date_pacific_str(1782939839000) == "2026-07-01"


def test_ms_to_pacific_date_str_from_ship_datetime() -> None:
    assert ms_to_pacific_date_str(1782939839000) == "2026-07-01"


def test_line_ship_date_time_ms_reads_tracking_info() -> None:
    line = {
        "orderLineStatuses": {
            "orderLineStatus": [
                {"trackingInfo": {"shipDateTime": 1782939839000, "trackingNumber": "555"}}
            ]
        }
    }
    assert line_ship_date_time_ms(line) == 1782939839000


def test_ship_date_pacific_str_uses_ship_date_pacific() -> None:
    order = {
        "orderDate": 1782683478948,
        "orderLines": {
            "orderLine": [
                {
                    "orderLineStatuses": {
                        "orderLineStatus": [
                            {"trackingInfo": {"shipDateTime": 1782939839000}}
                        ]
                    }
                }
            ]
        },
    }
    assert _ship_date_pacific_str(order, order["orderDate"]) == "2026-07-01"
    assert order_ship_date_time_ms(order) == 1782939839000


def test_order_shipping_state_reads_postal_address() -> None:
    order = {
        "shippingInfo": {
            "postalAddress": {
                "state": "ok",
                "city": "Yukon",
            }
        }
    }
    assert order_shipping_state(order) == "OK"


def test_build_sap_invoice_payload_sets_payto_and_shipto_from_state() -> None:
    order = {
        "purchaseOrderId": "PO-STATE",
        "customerOrderId": "CO-STATE",
        "orderDate": 1782683478948,
        "shippingInfo": {
            "postalAddress": {
                "state": "OK",
                "city": "Yukon",
                "postalCode": "73099",
            }
        },
        "orderLines": {
            "orderLine": [
                {
                    "lineNumber": "1",
                    "item": {"sku": "SKU1"},
                    "orderLineQuantity": {"amount": "1"},
                    "charges": {
                        "charge": [
                            {
                                "chargeType": "PRODUCT",
                                "chargeName": "ItemPrice",
                                "chargeAmount": {"amount": 10.0},
                            }
                        ]
                    },
                }
            ]
        },
    }

    with patch(
        "walmart.sap_invoice_payload.resolve_item_code",
        return_value="FG-TEST-001",
    ):
        payload = build_sap_invoice_payload(
            MagicMock(),
            order,
            require_batches=False,
        )

    assert payload["PayToCode"] == "OK"
    assert payload["ShipToCode"] == "OK"


def test_build_sap_invoice_payload_defaults_missing_item_price_to_zero() -> None:
    """Walmart WFS orders can ship with empty charges[]; post with UnitPrice 0."""
    order = {
        "purchaseOrderId": "119121191213116",
        "customerOrderId": "200015073510516",
        "orderDate": 1785303898924,
        "orderLines": {
            "orderLine": [
                {
                    "lineNumber": "1",
                    "item": {"sku": "FBA_DC_M_1P"},
                    "orderLineQuantity": {"amount": "1"},
                    "charges": {"charge": []},
                    "orderLineStatuses": {
                        "orderLineStatus": [
                            {
                                "status": "Shipped",
                                "statusQuantity": {"amount": "1"},
                            }
                        ]
                    },
                }
            ]
        },
    }

    with patch(
        "walmart.sap_invoice_payload.resolve_item_code",
        return_value="FG-TEST-001",
    ), patch(
        "walmart.sap_invoice_payload.fetch_batches_for_item",
        return_value=[],
    ):
        payload = build_sap_invoice_payload(
            MagicMock(),
            order,
            require_batches=False,
        )

    product = next(line for line in payload["DocumentLines"] if line["ItemCode"] == "FG-TEST-001")
    assert product["Quantity"] == 1
    assert product["UnitPrice"] == 0.0
    assert not any(line["ItemCode"] == TAX_ITEM_CODE for line in payload["DocumentLines"])


def test_build_sap_invoice_payload_includes_tax_date() -> None:
    order = {
        "purchaseOrderId": "PO1",
        "customerOrderId": "CO1",
        "orderDate": 1782683478948,
        "orderLines": {
            "orderLine": [
                {
                    "lineNumber": "1",
                    "item": {"sku": "SKU1"},
                    "orderLineQuantity": {"amount": "1"},
                    "charges": {
                        "charge": [
                            {
                                "chargeType": "PRODUCT",
                                "chargeName": "ItemPrice",
                                "chargeAmount": {"amount": 10.0},
                            }
                        ]
                    },
                    "orderLineStatuses": {
                        "orderLineStatus": [
                            {"trackingInfo": {"shipDateTime": 1782939839000}}
                        ]
                    },
                }
            ]
        },
    }

    with patch(
        "walmart.sap_invoice_payload.resolve_item_code",
        return_value="FG-TEST-001",
    ):
        payload = build_sap_invoice_payload(
            MagicMock(),
            order,
            require_batches=False,
        )

    assert payload["DocDate"] == "2026-07-01"
    assert payload["TaxDate"] == "2026-06-28"
    assert payload["DocDueDate"] == payload["DocDate"]


def test_build_sap_invoice_payload_uses_total_tax_including_fee_tax() -> None:
    order = {
        "purchaseOrderId": "129118179397968",
        "customerOrderId": "200015130141155",
        "orderDate": 1782683478948,
        "orderLines": {
            "orderLine": [
                {
                    "lineNumber": "1",
                    "item": {"sku": "KK-X815-W3C7"},
                    "orderLineQuantity": {"amount": "1"},
                    "charges": {
                        "charge": [
                            {
                                "chargeType": "PRODUCT",
                                "chargeName": "ItemPrice",
                                "chargeAmount": {"amount": 14.57, "currency": "USD"},
                                "tax": {
                                    "taxName": "Tax1",
                                    "taxAmount": {"amount": 0.61, "currency": "USD"},
                                },
                            },
                            {
                                "chargeType": "FEE",
                                "chargeName": "Fee",
                                "chargeAmount": {"amount": 0, "currency": "USD"},
                                "taxAndOtherFees": {
                                    "taxAndOtherFeesName": "County Surcharge Tax",
                                    "taxAndOtherFeesAmount": {"amount": 0.08, "currency": "USD"},
                                },
                            },
                        ]
                    },
                }
            ]
        },
    }

    with patch(
        "walmart.sap_invoice_payload.resolve_item_code",
        return_value="FG-TEST-001",
    ):
        payload = build_sap_invoice_payload(
            MagicMock(),
            order,
            require_batches=False,
        )

    tax_lines = [
        line
        for line in payload["DocumentLines"]
        if line.get("ItemCode") == TAX_ITEM_CODE
    ]
    assert len(tax_lines) == 1
    assert tax_lines[0]["UnitPrice"] == 0.69


def test_build_sap_invoice_payload_uses_net_unit_price_for_promotion() -> None:
    order = {
        "purchaseOrderId": "119118185161396",
        "customerOrderId": "200015030709332",
        "orderDate": 1782772114914,
        "orderLines": {
            "orderLine": [
                {
                    "lineNumber": "1",
                    "item": {"sku": "Original_Monster_1"},
                    "orderLineQuantity": {"amount": "1"},
                    "charges": {
                        "charge": [
                            {
                                "chargeType": "PRODUCT",
                                "chargeName": "ItemPrice",
                                "chargeAmount": {"amount": 9.7, "currency": "USD"},
                                "tax": {
                                    "taxName": "Tax1",
                                    "taxAmount": {"amount": 0.59, "currency": "USD"},
                                },
                            }
                        ]
                    },
                    "refund": {
                        "refundCharges": {
                            "refundCharge": [
                                {
                                    "charge": {
                                        "chargeName": "Promotion",
                                        "chargeType": "PRODUCT",
                                        "chargeAmount": {"amount": -0.91, "currency": "USD"},
                                    }
                                }
                            ]
                        }
                    },
                }
            ]
        },
    }

    with patch(
        "walmart.sap_invoice_payload.resolve_item_code",
        return_value="FG-TEST-001",
    ):
        payload = build_sap_invoice_payload(
            MagicMock(),
            order,
            require_batches=False,
        )

    product_lines = [
        line
        for line in payload["DocumentLines"]
        if line.get("ItemCode") == "FG-TEST-001"
    ]
    assert len(product_lines) == 1
    assert product_lines[0]["UnitPrice"] == 8.79
    assert "DiscountPercent" not in product_lines[0]


def test_build_sap_invoice_payload_net_unit_price_with_qty_two() -> None:
    order = {
        "purchaseOrderId": "PO-QTY2",
        "customerOrderId": "CO-QTY2",
        "orderDate": 1782772114914,
        "orderLines": {
            "orderLine": [
                {
                    "lineNumber": "1",
                    "item": {"sku": "SKU2"},
                    "orderLineQuantity": {"amount": "2"},
                    "charges": {
                        "charge": [
                            {
                                "chargeType": "PRODUCT",
                                "chargeName": "ItemPrice",
                                "chargeAmount": {"amount": 17.46, "currency": "USD"},
                            }
                        ]
                    },
                    "refund": {
                        "refundCharges": {
                            "refundCharge": [
                                {
                                    "charge": {
                                        "chargeName": "Promotion",
                                        "chargeType": "PRODUCT",
                                        "chargeAmount": {"amount": -0.92, "currency": "USD"},
                                    }
                                }
                            ]
                        }
                    },
                }
            ]
        },
    }

    with patch(
        "walmart.sap_invoice_payload.resolve_item_code",
        return_value="FG-TEST-001",
    ):
        payload = build_sap_invoice_payload(
            MagicMock(),
            order,
            require_batches=False,
        )

    product_lines = [
        line
        for line in payload["DocumentLines"]
        if line.get("ItemCode") == "FG-TEST-001"
    ]
    assert product_lines[0]["Quantity"] == 2
    assert product_lines[0]["UnitPrice"] == 8.27
    assert "DiscountPercent" not in product_lines[0]


def test_partial_delivery_uses_status_quantities_independent_of_order() -> None:
    line = {
        "orderLineQuantity": {"amount": "4"},
        "orderLineStatuses": {
            "orderLineStatus": [
                {
                    "status": "Delivered",
                    "statusQuantity": {"amount": "3"},
                    "trackingInfo": {
                        "trackingNumber": "523966532697",
                        "carrierName": {"otherCarrier": "Fedex"},
                        "shipDateTime": 1782768719000,
                    },
                },
                {
                    "status": "Cancelled",
                    "statusQuantity": {"amount": "1"},
                    "trackingInfo": {},
                },
            ]
        },
    }

    quantities = line_status_quantities(line)
    assert quantities["total"] == 4
    assert quantities["invoiceable"] == 3
    assert quantities["cancelled"] == 1
    assert quantities["is_partial"] is True
    assert line_invoiceable_quantity(line) == 3
    assert line_display_status(line) == "Delivered"
    assert _line_tracking(line) == ("523966532697", None, "Fedex")
    assert line_ship_date_time_ms(line) == 1782768719000


def test_partial_delivery_prorates_sap_tax_and_shipping_totals() -> None:
    order = {
        "purchaseOrderId": "129117970920219",
        "customerOrderId": "200014893288892",
        "orderDate": 1782591240089,
        "orderLines": {
            "orderLine": [
                {
                    "lineNumber": "1",
                    "item": {"sku": "DC_XL_1LB"},
                    "orderLineQuantity": {"amount": "4"},
                    "charges": {
                        "charge": [
                            {
                                "chargeType": "PRODUCT",
                                "chargeName": "ItemPrice",
                                "chargeAmount": {"amount": 71.28},
                                "tax": {"taxAmount": {"amount": 6.15}},
                            },
                            {
                                "chargeType": "FEE",
                                "chargeName": "Fee",
                                "chargeAmount": {"amount": 0},
                                "taxAndOtherFees": {
                                    "taxAndOtherFeesName": "County Surcharge Tax",
                                    "taxAndOtherFeesAmount": {"amount": 0.40},
                                },
                            },
                            {
                                "chargeType": "SHIPPING",
                                "chargeName": "Shipping",
                                "chargeAmount": {"amount": 2.75},
                                "taxAndOtherFees": {
                                    "taxAndOtherFeesName": "Tax2",
                                    "taxAndOtherFeesAmount": {"amount": 0.22},
                                },
                            },
                        ]
                    },
                    "orderLineStatuses": {
                        "orderLineStatus": [
                            {"status": "Delivered", "statusQuantity": {"amount": "3"}},
                            {"status": "Cancelled", "statusQuantity": {"amount": "1"}},
                        ]
                    },
                }
            ]
        },
    }

    with patch(
        "walmart.sap_invoice_payload.resolve_item_code",
        return_value="FG-TEST-001",
    ), patch(
        "walmart.sap_invoice_payload.fetch_batches_for_item",
        return_value=[],
    ):
        payload = build_sap_invoice_payload(
            MagicMock(),
            order,
            require_batches=False,
        )

    product = next(line for line in payload["DocumentLines"] if line["ItemCode"] == "FG-TEST-001")
    shipping = next(line for line in payload["DocumentLines"] if line["ItemCode"] == SHIPPING_ITEM_CODE)
    tax = next(line for line in payload["DocumentLines"] if line["ItemCode"] == TAX_ITEM_CODE)
    assert payload["U_CreatedBy"] == "Posted By Walmart API"
    assert product["Quantity"] == 3
    assert product["UnitPrice"] == 17.82
    assert shipping["Quantity"] == 1
    assert shipping["UnitPrice"] == 2.0625
    assert tax["Quantity"] == 1
    assert tax["UnitPrice"] == 5.08
