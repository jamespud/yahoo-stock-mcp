"""Trusted protocol adapter, run ONLY inside the configured isolated container.

The container policy is the security boundary. Python exec is used for loading a
strategy inside that boundary, never as a substitute for OS isolation.
"""
import datetime
import json
import os
import random
import sys


def send(value):
    payload = (json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n").encode()
    while payload:
        payload = payload[os.write(1, payload):]


def receive():
    raw = sys.stdin.buffer.readline(262145)
    if len(raw) > 262144 or not raw.endswith(b"\n"):
        raise RuntimeError("invalid host frame")
    return json.loads(raw)


class Bar(dict):
    def __getattr__(self, key):
        return self[key]


class BoundedLog:
    used = 0

    def write(self, value):
        payload = str(value).encode()
        self.used += len(payload)
        if self.used > 8192:
            raise RuntimeError("RESOURCE_LIMIT_EXCEEDED: strategy log budget exceeded")
        os.write(2, payload)
        return len(value)

    def flush(self):
        pass


class Context:
    def __init__(self, event, random_seed):
        self._event = event
        self._random_seed = random_seed
        self._request = 0

    @property
    def random_seed(self):
        return self._random_seed

    @property
    def now(self):
        return datetime.datetime.fromisoformat(self._event["now"])

    def position(self, symbol):
        return self._event["positions"].get(symbol, 0.)

    def equity(self):
        return self._event["equity"]

    def _rpc(self, request):
        self._request += 1
        send({**request, "event_id": self._event["id"], "request_id": self._request})
        response = receive()
        if response.get("type") != "history_result" or response.get("request_id") != self._request:
            raise RuntimeError("unexpected host reply")
        return [Bar(row) for row in response["rows"]]

    def history(self, symbol, timeframe, bars):
        return self._rpc({"type": "history", "symbol": symbol, "timeframe": timeframe, "bars": bars})


def main():
    # Normal strategy print() is a bounded log stream. The host also treats
    # direct writes to fd 1 as untrusted protocol, validating every message.
    sys.stdout = sys.stderr = BoundedLog()
    load = receive()
    scope = {"__name__": "quant_strategy"}
    try:
        random.seed(load["random_seed"], version=2)
        exec(compile(load["source"], "<quant-strategy>", "exec"), scope)
        strategy = scope[load["class_name"]]()
        send({"type": "ready", "id": load["id"]})
        while True:
            event = receive()
            ctx = Context(event, load["random_seed"])
            method = getattr(strategy, event["event"], None)
            if method is None:
                result = None
            elif event["event"] == "on_bar":
                result = method(ctx, Bar(event["bar"]))
            elif event["event"] == "on_fill":
                result = method(ctx, event["fill"])
            else:
                result = method(ctx)
            send({"type": "result", "id": event["id"], "value": result or []})
    except BaseException as error:
        send({"type": "error", "message": f"{type(error).__name__}: {str(error)[:1000]}"})


if __name__ == "__main__":
    main()
