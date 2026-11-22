import io
import json
import sys

import pytest

from stock_quant import __main__ as bridge
from stock_quant.jobs import JobStore


@pytest.mark.parametrize(("parameters", "field"), [
    ({"num_leaves": 7, "random_seed": 20261119}, "random_seed"),
    ({"learning_rate": 0}, "learning_rate"),
    ({"num_leaves": 7.5}, "num_leaves"),
    ({"max_depth": 2147483648}, "max_depth"),
    ({"min_child_samples": True}, "min_child_samples"),
    ({"n_estimators": 0}, "n_estimators"),
])
def test_invalid_training_rpc_is_audited_without_enqueue_or_wake(tmp_path, monkeypatch, capsys, parameters, field):
    registry = str(tmp_path / "quant.db")
    job = {"kind": "train_alpha_model", "feature_set_id": "sha256:" + "1" * 64,
           "label_name": "stock_forward_return", "target_type": "stock_return", "evaluation_mode": "validation_only",
           "research_protocol_hash": "sha256:" + "2" * 64, "stock_dataset_id": "sha256:" + "3" * 64,
           "split": {"train_start": "2024-01-01T00:00:00Z", "train_end": "2024-02-15T00:00:00Z",
                     "validation_start": "2024-02-15T00:00:00Z", "validation_end": "2024-03-15T00:00:00Z"},
           "hyperparameters": parameters}
    payload = {"method": "submit_alpha", "registry_path": registry, "job": job}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    wakes = []
    monkeypatch.setattr(bridge, "wake_worker", wakes.append)
    with pytest.raises(SystemExit, match="1"):
        bridge.main()
    assert wakes == []
    assert f"hyperparameters.{field}" in json.loads(capsys.readouterr().out)["error"]["message"]
    with JobStore(registry) as store:
        assert store.connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
        event, = store.list_alpha_research_audit()
    assert event["event_type"] == "TRAINING_SUBMISSION_REJECTED"
    assert event["detail"] == {"source": "runtime", "fields": [f"hyperparameters.{field}"]}
