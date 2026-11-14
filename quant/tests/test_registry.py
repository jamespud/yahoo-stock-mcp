import sqlite3

import pytest

from stock_quant.registry import Registry
from test_compiler import strategy


def test_registry_reopen_and_immutable_content(tmp_path):
    path = tmp_path / "quant.db"
    with Registry(path) as store:
        registered = store.register(strategy())
        plan = store.compile(registered["strategy_id"])
        assert store.register(strategy()) == registered
    with Registry(path) as store:
        assert store.get_plan(plan["plan_id"]) == plan
        assert store.get_strategy(registered["strategy_id"])["definition"] == strategy()
        assert store.compile(registered["strategy_id"]) == plan
        with pytest.raises(KeyError):
            store.compile("missing")


def test_invalid_definition_not_registered(tmp_path):
    with Registry(tmp_path / "quant.db") as store:
        with pytest.raises(ValueError):
            store.register({"bad": "definition"})
        assert store.connection.execute("SELECT count(*) FROM strategies").fetchone()[0] == 0


def test_corrupt_content_detected_on_read(tmp_path):
    with Registry(tmp_path / "quant.db") as store:
        registered = store.register(strategy())
        store.connection.execute("UPDATE strategies SET definition = '{}' ")
        store.connection.commit()
        with pytest.raises(ValueError, match="integrity"):
            store.get_strategy(registered["strategy_id"])
