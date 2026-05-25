"""Carrier / delivery aggregator client stub."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeliveryClient:
    api_key: str

    def create_label_stub(self, shipment: dict) -> dict:
        return {"label_url": "https://example.invalid/label.pdf", "input": shipment}
