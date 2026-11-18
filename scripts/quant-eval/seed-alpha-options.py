#!/usr/bin/env python3
"""Seed a deterministic historical option fixture into the Alpha catalog for MCP E2E tests."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

from stock_quant.alpha.contracts import OptionDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.option_dataset import OptionSnapshotBatch, prepare_option_dataset
from stock_quant.alpha.options.contracts import OptionContract, option_contract_id
from stock_quant.alpha.options.quotes import OptionQuote


class Provider:
    def __init__(self, rows):
        self.rows = rows

    def read(self, request):
        return OptionSnapshotBatch(rows=self.rows, source_version="theta-fixture-v3", quality={})


def contract(option_type: str, strike: float, expiration: date):
    fields = dict(underlying_id="AAPL", option_type=option_type, strike=strike, expiration=expiration,
                  exercise_style="american", settlement_type="physical", multiplier=100,
                  currency="USD", deliverable="100 shares AAPL", adjustment_version="standard")
    return OptionContract(contract_id=option_contract_id(**fields), **fields)


def main():
    registry = Path(sys.argv[1])
    parent_dataset_id = sys.argv[2]
    store = DatasetStore(registry.with_suffix(".alpha"))
    rows = []
    for day in range(4):
        at = datetime(2026, 1, 2, 15, tzinfo=timezone.utc) + timedelta(days=day)
        for expiry_index, expiration in enumerate((date(2026, 2, 20), date(2026, 3, 20))):
            for strike, base_iv in ((180.0, 0.24), (200.0, 0.28), (220.0, 0.32)):
                for option_type in ("CALL", "PUT"):
                    option = contract(option_type, strike, expiration)
                    iv = base_iv + expiry_index * 0.02 + day * 0.001 + (0.01 if option_type == "PUT" else 0)
                    rows.append(OptionQuote(
                        contract=option, observed_at=at, quote_at=at, available_at=at + timedelta(minutes=1),
                        bid=4.8, ask=5.0, bid_size=10, ask_size=10, last_price=4.9,
                        last_trade_at=None, volume=10, open_interest=100, implied_volatility=iv,
                        source="thetadata", source_version="theta-fixture-v3", quality_flags=[],
                    ))
    request = OptionDatasetRequest(
        source="thetadata", underlyings=["AAPL"],
        start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 10, tzinfo=timezone.utc),
        quote_policy="historical_bid_ask",
    )
    manifest = prepare_option_dataset(request, store=store, provider=Provider(rows))
    prediction_contract = contract("CALL", 200.0, date(2026, 2, 20))
    prediction_set = store.publish_prediction_panel(
        [{"decision_at": datetime(2026, 1, 2, 15, tzinfo=timezone.utc),
          "instrument_id": prediction_contract.contract_id, "prediction_value": 1.0,
          "model_id": "sha256:" + "a" * 64, "model_set_id": "sha256:" + "b" * 64, "fold": "fixture"}],
        parent_feature_set_id=parent_dataset_id, model_id="sha256:" + "a" * 64,
        model_set_id="sha256:" + "b" * 64,
        start_at=datetime(2026, 1, 1, tzinfo=timezone.utc), end_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        price_basis="price_basis_unverified",
    )
    print(json.dumps({"dataset_id": manifest.dataset_id, "prediction_set_id": prediction_set.dataset_id}))


if __name__ == "__main__":
    main()
