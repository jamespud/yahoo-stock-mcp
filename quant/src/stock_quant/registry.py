"""Content-addressed strategy and plan registry in an independent SQLite file."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .compiler import compile_strategy, digest
from .python_plans import compile_python
from .sandbox import PythonArtifact, validate_python


class Registry:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY, definition TEXT NOT NULL, content_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_plans (
                id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES strategies(id),
                plan TEXT NOT NULL, content_hash TEXT NOT NULL);
        ''')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def register(self, definition: dict) -> dict:
        if definition.get("mode") == "python":
            checked = validate_python(definition)
            if not checked["valid"]:
                raise ValueError("SCHEMA_INVALID: " + checked["errors"][0]["message"])
            definition = PythonArtifact.model_validate(definition).model_dump()
            strategy_id = digest(definition)
        else:
            strategy_id = compile_strategy(definition).strategy_hash
        encoded = json.dumps(definition, sort_keys=True, allow_nan=False)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO strategies VALUES (?, ?, ?)", (strategy_id, encoded, digest(definition)))
        self.get_strategy(strategy_id)
        return {"strategy_id": strategy_id, "definition_hash": strategy_id}

    def get_strategy(self, strategy_id: str) -> dict:
        row = self.connection.execute("SELECT definition, content_hash FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown strategy_id")
        definition = json.loads(row[0])
        if digest(definition) != row[1]:
            raise ValueError("Strategy integrity check failed")
        identity = digest(PythonArtifact.model_validate(definition).model_dump()) if definition.get("mode") == "python" else compile_strategy(definition).strategy_hash
        if identity != strategy_id:
            raise ValueError("Strategy integrity check failed")
        return {"strategy_id": strategy_id, "definition": definition}

    def compile(self, strategy_id: str, parameters: dict | None = None, symbol: str | None = None) -> dict:
        definition = self.get_strategy(strategy_id)["definition"]
        plan = compile_python(definition, parameters, symbol) if definition.get("mode") == "python" else compile_strategy(definition, parameters, symbol)
        result = {"plan_id": plan.plan_hash, **plan.to_dict()}
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO execution_plans VALUES (?, ?, ?, ?)", (plan.plan_hash, strategy_id, encoded, digest(result)))
        return self.get_plan(plan.plan_hash)

    def get_plan(self, plan_id: str) -> dict:
        row = self.connection.execute("SELECT plan, content_hash FROM execution_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown plan_id")
        result = json.loads(row[0])
        if digest(result) != row[1] or result["plan_hash"] != plan_id:
            raise ValueError("Plan integrity check failed")
        return result
