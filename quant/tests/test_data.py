from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, strategies as st

from stock_quant.data import MarketBar, Snapshot

T = datetime(2026, 1, 2, 16, tzinfo=timezone.utc)


def bar(symbol="AAPL", offset=0, delay=0, **kwargs):
    end = T + timedelta(days=offset)
    return MarketBar(symbol=symbol, timeframe="1d", exchange="XNAS", start_time=end - timedelta(hours=6, minutes=30), end_time=end,
                     available_at=end + timedelta(minutes=delay), open=100., high=110., low=90., close=105., volume=1000.,
                     source="golden", adjustment="raw", dataset_version="1", **kwargs)


def test_future_and_delayed_data_hidden():
    snapshot = Snapshot([bar(), bar(offset=1, delay=10)], calendar_version="XNAS-test-v1")
    assert len(snapshot.history("AAPL", "1d", T + timedelta(days=1), 10)) == 1
    assert len(snapshot.history("AAPL", "1d", T + timedelta(days=1, minutes=10), 10)) == 2


def test_snapshot_order_independent_and_symbol_isolated():
    rows = [bar(), bar("MSFT"), bar(offset=1)]
    s = Snapshot(rows, calendar_version="v1")
    assert s.id == Snapshot(list(reversed(rows)), calendar_version="v1").id
    assert len(s.history("MSFT", "1d", T, 10)) == 1
    assert s.id != Snapshot(rows, calendar_version="v2").id


@pytest.mark.parametrize("change", [{"available_at": T - timedelta(seconds=1)}, {"start_time": T}, {"close": float("nan")}, {"volume": -1}, {"high": 99}, {"open": 0}, {"end_time": T.replace(tzinfo=None)}])
def test_invalid_bars_rejected(change):
    raw = bar().model_dump()
    raw.update(change)
    with pytest.raises(ValueError):
        MarketBar.model_validate(raw)


def test_duplicate_identity_and_mixed_revisions_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        Snapshot([bar(), bar()], calendar_version="v1")
    changed = bar().model_copy(update={"dataset_version": "2", "end_time": T + timedelta(days=1), "available_at": T + timedelta(days=1)})
    with pytest.raises(ValueError, match="version"):
        Snapshot([bar(), changed], calendar_version="v1")


@given(st.integers(min_value=-100000, max_value=100000))
def test_pit_invariant(seconds):
    now = T + timedelta(seconds=seconds)
    rows = Snapshot([bar(offset=i, delay=5) for i in range(-2, 3)], calendar_version="v1").history("AAPL", "1d", now, 10)
    assert all(row.available_at <= now for row in rows)


def test_naive_decision_and_oversized_history_fail():
    s = Snapshot([bar()], calendar_version="v1")
    with pytest.raises(ValueError):
        s.history("AAPL", "1d", T.replace(tzinfo=None), 10)
    with pytest.raises(ValueError):
        s.history("AAPL", "1d", T, 100001)


def test_overlapping_observations_and_boolean_prices_rejected():
    a = bar()
    b = a.model_copy(update={"start_time": a.start_time + timedelta(minutes=1), "end_time": a.end_time + timedelta(minutes=1), "available_at": a.available_at + timedelta(minutes=1)})
    with pytest.raises(ValueError, match="Overlapping"):
        Snapshot([a, b], calendar_version="v1")
    with pytest.raises(ValueError):
        MarketBar.model_validate({**a.model_dump(), "open": True})


def test_unverified_provider_prices_remain_readable_but_cannot_execute(tmp_path):
    from stock_quant.jobs import JobStore
    from stock_quant.portfolio import run_portfolio
    from test_runtime import plan
    s = Snapshot([bar(offset=i).model_copy(update={"adjustment": "unknown"}) for i in range(3)], calendar_version="unverified")
    assert s.bars[0].adjustment == "unknown"
    with pytest.raises(ValueError, match="requires raw"):
        run_portfolio({"AAPL": plan()}, s)
    with JobStore(tmp_path / "unverified.db") as store:
        sid = store.register({"schema_version": "1.0", "name": "hold", "decision_timeframe": "1d", "entry": {"kind": "const", "value": True}, "exit": {"kind": "const", "value": False}})["strategy_id"]
        pid = store.compile(sid)["plan_id"]
        store.import_snapshot([b.model_dump(mode="json") for b in s.bars], s.calendar_version)
        with pytest.raises(ValueError, match="requires raw"):
            store.submit({"plan_id": pid, "data_snapshot_id": s.id, "symbol": "AAPL", "timeframe": "1d"})
        assert store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
