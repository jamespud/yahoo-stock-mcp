"""Portfolio ledger for splits, dividends and explicitly unsupported events."""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..data.corporate_actions import CorporateAction, CorporateActionDataset


def _day(value) -> date:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    return value


class CorporateActionLedger:
    def __init__(self, dataset: CorporateActionDataset):
        self.dataset = dataset
        self.actions = sorted(dataset.actions, key=lambda action: (action.ex_date, action.action_id))
        self._entitlements: dict[str, dict] = {}
        self._paid: set[str] = set()
        self.action_entries: list[dict] = []
        self.cash_entries: list[dict] = []
        self.risk_events: list[dict] = []

    def process(self, day, positions: dict, cash: float) -> tuple[float, list[dict], list[dict], list[dict]]:
        action_rows = []
        cash_rows = []
        risk_rows = []
        for action in self.actions:
            if _day(action.ex_date) != day:
                continue
            if action.action_type in {"split", "reverse_split"}:
                position = positions.get(action.instrument_id.upper())
                if position is None or position["quantity"] <= 0:
                    continue
                old_quantity = float(position["quantity"])
                old_basis = float(position["basis"])
                ratio = float(action.terms.get("ratio", 0))
                if ratio <= 0:
                    raise ValueError(f"Invalid split ratio for {action.action_id}")
                position["quantity"] = old_quantity * ratio
                position["basis"] = old_basis
                row = {
                    "date": day.isoformat(), "action_id": action.action_id,
                    "instrument_id": action.instrument_id.upper(), "kind": "split",
                    "old_quantity": old_quantity, "new_quantity": position["quantity"],
                    "old_unit_cost": old_basis / old_quantity,
                    "new_unit_cost": old_basis / position["quantity"] if position["quantity"] else None,
                    "source": action.source, "source_version": action.source_version,
                }
                self.action_entries.append(row)
                action_rows.append(row)
            elif action.action_type == "cash_dividend":
                position = positions.get(action.instrument_id.upper())
                quantity = float(position["quantity"]) if position is not None else 0.0
                amount = quantity * float(action.terms.get("cash_per_share", 0))
                if quantity <= 0:
                    continue
                entitlement = {
                    "action_id": action.action_id,
                    "instrument_id": action.instrument_id.upper(),
                    "quantity": quantity,
                    "amount": amount,
                    "payment_date": action.payment_date,
                }
                self._entitlements[action.action_id] = entitlement
                row = {
                    "date": day.isoformat(), "action_id": action.action_id,
                    "instrument_id": action.instrument_id.upper(), "kind": "dividend_entitlement",
                    "quantity": quantity, "amount": amount,
                    "payment_date": _day(action.payment_date).isoformat(),
                    "source": action.source, "source_version": action.source_version,
                }
                self.action_entries.append(row)
                action_rows.append(row)
            elif action.action_type == "symbol_change":
                old = action.instrument_id.upper()
                new = str(action.terms.get("new_symbol", "")).strip().upper()
                if old in positions and new:
                    positions[new] = positions.pop(old)
                    row = {"date": day.isoformat(), "action_id": action.action_id, "kind": "symbol_change", "old_symbol": old, "new_symbol": new}
                    self.action_entries.append(row)
                    action_rows.append(row)
            elif action.action_type == "delisting":
                risk = {"time": day.isoformat(), "code": "DELISTED_SECURITY", "symbol": action.instrument_id.upper(), "action_id": action.action_id}
                self.risk_events.append(risk)
                risk_rows.append(risk)

        for action in self.actions:
            if action.action_type != "cash_dividend" or action.action_id in self._paid:
                continue
            if action.payment_date is None or _day(action.payment_date) != day:
                continue
            entitlement = self._entitlements.get(action.action_id)
            if entitlement is None:
                continue
            amount = float(entitlement["amount"])
            cash += amount
            self._paid.add(action.action_id)
            cash_row = {
                "date": day.isoformat(), "kind": "dividend", "amount": amount,
                "instrument_id": entitlement["instrument_id"], "action_id": action.action_id,
            }
            payment_row = {
                "date": day.isoformat(), "action_id": action.action_id,
                "instrument_id": entitlement["instrument_id"], "kind": "dividend_payment",
                "amount": amount, "payment_date": _day(action.payment_date).isoformat(),
            }
            cash_rows.append(cash_row)
            action_rows.append(payment_row)
            self.cash_entries.append(cash_row)
            self.action_entries.append(payment_row)
        return cash, action_rows, cash_rows, risk_rows
