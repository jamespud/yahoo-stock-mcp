from datetime import datetime, timezone

import pytest

from stock_quant.alpha.data.action_validation import validate_execution_inputs
from stock_quant.alpha.data.corporate_actions import (
    CorporateAction,
    CorporateActionCoverage,
    apply_cash_dividend,
    apply_split,
    build_corporate_action_dataset,
    cash_dividend_entitlement,
)
from stock_quant.alpha.data.price_attestation import PriceAttestation
from stock_quant.alpha.errors import AlphaError

AS_OF = datetime(2024, 1, 10, tzinfo=timezone.utc)


def _action(action_type, *, action_id, terms, ex_date="2024-01-05", payment_date=None):
    return CorporateAction(
        action_id=action_id,
        instrument_id="AAPL",
        action_type=action_type,
        ex_date=datetime.fromisoformat(ex_date).replace(tzinfo=timezone.utc),
        payment_date=datetime.fromisoformat(payment_date).replace(tzinfo=timezone.utc) if payment_date else None,
        effective_at=datetime.fromisoformat(ex_date).replace(tzinfo=timezone.utc),
        source="fixture-actions",
        source_version="v1",
        quality_status="VERIFIED",
        terms=terms,
    )


def _coverage(start="2024-01-01", end="2024-02-01"):
    return CorporateActionCoverage(
        instrument_id="AAPL",
        start_at=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end_at=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
        source="fixture-actions",
        source_version="v1",
        status="VERIFIED",
    )


def test_split_event_preserves_equity():
    action = _action("split", action_id="split-2", terms={"ratio": 2.0})
    position = apply_split({"quantity": 10, "unit_cost": 100.0}, action)
    assert position["quantity"] == pytest.approx(20)
    assert position["unit_cost"] == pytest.approx(50.0)
    assert position["quantity"] * position["unit_cost"] == pytest.approx(1000.0)


def test_cash_dividend_updates_entitled_account():
    action = _action("cash_dividend", action_id="div-1", terms={"cash_per_share": 0.5}, payment_date="2024-01-10")
    assert cash_dividend_entitlement({"quantity": 10}, action) == pytest.approx(5.0)
    account = {"cash": 100.0, "ledger": []}
    updated = apply_cash_dividend(account, {"AAPL": 10}, action, as_of=AS_OF)
    assert updated["cash"] == pytest.approx(105.0)
    assert updated["ledger"][0]["action_id"] == "div-1"


def test_missing_action_coverage_blocks_total_return_claim():
    dataset = build_corporate_action_dataset(
        [_action("cash_dividend", action_id="div-1", terms={"cash_per_share": 0.5}, payment_date="2024-01-10")],
        expected_instruments=["AAPL"],
        start_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
        coverage=[],
    )
    assert dataset.coverage_status == "BLOCKED"
    assert dataset.total_return_claim_allowed is False

    attestation = PriceAttestation(
        schema_version="price-attestation-v1",
        attestation_id="sha256:" + "2" * 64,
        price_dataset_id="sha256:" + "1" * 64,
        price_basis="raw_attested",
        source_and_source_version={"source": "yahoo", "source_version": "v1"},
        verification_method="INDEPENDENT_RAW_OHLC_REFERENCE_V1",
        reference_evidence_ids=["ref-1"],
        calendar_identity={"calendar_name": "XNYS", "calendar_version": "v1"},
        coverage_window={"start_at": "2024-01-01T00:00:00+00:00", "end_at": "2024-02-01T00:00:00+00:00"},
        verified_symbols=["AAPL"], unverified_symbols=[], verified_scopes=[], attestation_status="VERIFIED",
    )
    validity = validate_execution_inputs(
        attestation, dataset, instruments=["AAPL"],
        start_at=datetime(2024, 1, 1, tzinfo=timezone.utc), end_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
    )
    assert validity.execution_status == "BLOCKED"
    assert validity.total_return_claim_allowed is False


def test_reverse_split_is_accounted():
    action = _action("reverse_split", action_id="reverse-1", terms={"ratio": 0.1})
    position = apply_split({"quantity": 10, "unit_cost": 100.0}, action)
    assert position["quantity"] == pytest.approx(1.0)
    assert position["unit_cost"] == pytest.approx(1000.0)
    assert position["quantity"] * position["unit_cost"] == pytest.approx(1000.0)


def test_unsupported_corporate_action_is_explicit():
    merger = _action("merger", action_id="merge-1", terms={"acquirer": "MSFT"})
    with pytest.raises(AlphaError) as error:
        build_corporate_action_dataset(
            [merger], expected_instruments=["AAPL"],
            start_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
            coverage=[_coverage()],
        )
    assert error.value.context["reason"] == "UNSUPPORTED_CORPORATE_ACTION"
    assert error.value.context["action_type"] == "merger"


def test_complete_action_coverage_allows_joint_validation():
    dataset = build_corporate_action_dataset(
        [_action("split", action_id="split-2", terms={"ratio": 2.0})],
        expected_instruments=["AAPL"],
        start_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
        coverage=[_coverage()],
    )
    assert dataset.coverage_status == "VERIFIED"
    assert dataset.total_return_claim_allowed is True
