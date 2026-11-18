from datetime import date, datetime, timedelta, timezone

import pytest

from stock_quant.alpha.contracts import OptionDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
from stock_quant.alpha.data.option_dataset import OptionSnapshotBatch, prepare_option_dataset, validate_historical_quotes
from stock_quant.alpha.data.thetadata_adapter import ThetaDataV3Provider, ThetaDataV3ResponseError
from stock_quant.alpha.errors import AlphaError, AlphaErrorCode
from stock_quant.alpha.options.contracts import OptionContract, option_contract_id
from stock_quant.alpha.options.quotes import OptionQuote


def _contract(adjustment="standard", multiplier=100, option_type="CALL"):
    fields = dict(
        underlying_id="AAPL", option_type=option_type, strike=200.0, expiration=date(2026, 12, 18),
        exercise_style="american", settlement_type="physical", multiplier=multiplier,
        currency="USD", deliverable="100 shares AAPL", adjustment_version=adjustment,
    )
    return OptionContract(contract_id=option_contract_id(**fields), **fields)


def _quote(observed, contract=None, quote_at=None, bid=5.0, ask=5.2, source="yahoo"):
    contract = contract or _contract()
    return OptionQuote(
        contract=contract, observed_at=observed, quote_at=quote_at,
        available_at=observed + timedelta(seconds=1), bid=bid, ask=ask, bid_size=10, ask_size=12,
        last_price=5.1, last_trade_at=None, volume=100, open_interest=500, implied_volatility=0.3,
        source=source, source_version="fixture-v1", quality_flags=[],
    )


def test_option_contract_identity_includes_adjustment_and_multiplier():
    standard = _contract()
    adjusted = _contract(adjustment="adjusted-2025", multiplier=150)
    assert standard.contract_id != adjusted.contract_id
    assert option_contract_id(**standard.model_dump(exclude={"contract_id"})) == standard.contract_id


def test_yahoo_snapshots_are_append_only_and_not_historical():
    request = OptionDatasetRequest(
        source="yahoo", underlyings=["AAPL"], start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 3, tzinfo=timezone.utc), quote_policy="observed_snapshot",
    )
    class Provider:
        def read(self, request):
            return OptionSnapshotBatch(
                rows=[_quote(datetime(2026, 1, 1, 15, tzinfo=timezone.utc)), _quote(datetime(2026, 1, 2, 15, tzinfo=timezone.utc))],
                source_version="yahoo-snapshot-v1", quality={"quote_at": "unavailable"},
            )
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as directory:
        store = DatasetStore(Path(directory))
        manifest = prepare_option_dataset(request, store=store, provider=Provider())
        rows = store.read_rows(manifest.dataset_id)
        assert len(rows) == 2
        assert all(row["quote_at"] is None for row in rows)
        with pytest.raises(AlphaError) as info:
            validate_historical_quotes(manifest)
        assert info.value.code == AlphaErrorCode.OPTION_HISTORY_UNAVAILABLE


def test_theta_v3_fixture_maps_contracts_and_quotes():
    calls = []
    class Client:
        def get(self, path, params):
            calls.append((path, params))
            return {"header": {"error_type": "null"}, "response": [{
                "date": 20260102, "ms_of_day": 34200000, "root": "AAPL", "expiration": 20261218,
                "strike": 200000, "right": "C", "bid": 500, "ask": 520, "bid_size": 10, "ask_size": 12,
                "last": 510, "volume": 100, "open_interest": 500, "implied_volatility": 0.3,
                "contract": "AAPL  261218C00200000", "exercise_style": "A", "settlement_type": "P", "multiplier": 100,
            }]}
    provider = ThetaDataV3Provider(Client())
    batch = provider.read(OptionDatasetRequest(
        source="thetadata", underlyings=["AAPL"], start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 5, tzinfo=timezone.utc), quote_policy="historical_bid_ask",
    ))
    assert calls[0][0] == "/v3/option/history/quote"
    assert len(batch.rows) == 1
    row = batch.rows[0]
    assert row.contract.multiplier == 100
    assert row.quote_at == datetime(2026, 1, 2, 9, 30, tzinfo=timezone.utc)
    assert row.bid == 5.0 and row.ask == 5.2


def test_theta_provider_denial_maps_to_stable_error():
    class Client:
        def get(self, path, params):
            raise PermissionError("HTTP 403 subscription required")
    provider = ThetaDataV3Provider(Client())
    with pytest.raises(AlphaError) as info:
        provider.read(OptionDatasetRequest(
            source="thetadata", underlyings=["AAPL"], start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 5, tzinfo=timezone.utc), quote_policy="historical_bid_ask",
        ))
    assert info.value.code == AlphaErrorCode.PROVIDER_ACCESS_DENIED


def test_invalid_crossed_quotes_are_rejected():
    with pytest.raises(ValueError):
        _quote(datetime.now(timezone.utc), bid=6.0, ask=5.0, source="thetadata")


def test_option_underlying_path_traversal_is_rejected_before_writing():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        OptionDatasetRequest(source="thetadata", underlyings=["X/../../../tmp/escape"], start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 2, tzinfo=timezone.utc), quote_policy="historical_bid_ask")


def test_current_yahoo_snapshot_cannot_generate_historical_option_labels(tmp_path):
    from stock_quant.alpha.models.labels import historical_option_labels
    request = OptionDatasetRequest(source="yahoo", underlyings=["AAPL"], start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 3, tzinfo=timezone.utc), quote_policy="observed_snapshot")
    class Provider:
        def read(self, request):
            return OptionSnapshotBatch(rows=[_quote(datetime(2026, 1, 1, 15, tzinfo=timezone.utc))], source_version="yahoo-v1", quality={})
    store = DatasetStore(tmp_path / "alpha")
    manifest = prepare_option_dataset(request, store=store, provider=Provider())
    with pytest.raises(AlphaError) as info:
        historical_option_labels(manifest, store)
    assert info.value.code == AlphaErrorCode.OPTION_HISTORY_UNAVAILABLE


def test_historical_admission_requires_positive_bid_and_ask(tmp_path):
    request = OptionDatasetRequest(source="thetadata", underlyings=["AAPL"], start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 5, tzinfo=timezone.utc), quote_policy="historical_bid_ask")
    class Provider:
        def read(self, request):
            quote = _quote(datetime(2026, 1, 2, 15, tzinfo=timezone.utc), quote_at=datetime(2026, 1, 2, 15, tzinfo=timezone.utc), source="thetadata")
            quote = quote.model_copy(update={"bid": None})
            return OptionSnapshotBatch(rows=[quote], source_version="v3", quality={})
    with pytest.raises(AlphaError) as info:
        prepare_option_dataset(request, store=DatasetStore(tmp_path / "alpha"), provider=Provider())
    assert info.value.code == AlphaErrorCode.INVALID_OPTION_QUOTE
