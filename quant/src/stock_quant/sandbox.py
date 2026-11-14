"""Trusted Docker policy and bounded lifecycle/PIT RPC for untrusted strategies."""
from __future__ import annotations

import ast
from datetime import datetime
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError, model_validator

from .compiler import digest
from .contracts import StrictModel, Timeframe
from .data import MarketBar, Snapshot


class PythonArtifact(StrictModel):
    mode: Literal["python"] = "python"
    name: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=65536)
    class_name: str = Field(default="Strategy", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    data_timeframes: list[Timeframe] = Field(default_factory=lambda: ["1d"], min_length=1, max_length=6)
    decision_timeframe: Timeframe = "1d"

    @model_validator(mode="after")
    def authorized_clock(self):
        if self.decision_timeframe not in self.data_timeframes or len(set(self.data_timeframes)) != len(self.data_timeframes):
            raise ValueError("Decision timeframe must be authorized; duplicate periods are forbidden")
        return self


class OrderIntent(StrictModel):
    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, strict=True)


class HistoryRequest(StrictModel):
    type: Literal["history"]
    event_id: int = Field(strict=True)
    request_id: int = Field(ge=1, le=100, strict=True)
    symbol: str
    timeframe: Timeframe
    bars: int = Field(ge=1, le=256, strict=True)


class FillView(StrictModel):
    order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, strict=True)
    price: float = Field(gt=0, strict=True)
    commission: float = Field(ge=0, strict=True)
    time: AwareDatetime


INTENTS = TypeAdapter(list[OrderIntent])


def validate_python(raw: dict) -> dict:
    try:
        artifact = PythonArtifact.model_validate(raw)
        ast.parse(artifact.source, filename="<quant-strategy>")
        return {"valid": True, "errors": [], "artifact_hash": digest(artifact.model_dump()),
                "warnings": ["Syntax only; strategy code and lifecycle are validated only in an isolated session"]}
    except (ValidationError, SyntaxError, RecursionError, ValueError) as error:
        return {"valid": False, "errors": [{"code": "SCHEMA_INVALID", "message": str(error)[:4096]}]}


def bar_payload(bar: MarketBar) -> dict:
    value = bar.model_dump(mode="json")
    for field in ("start_time", "end_time", "available_at"):
        value[field] = getattr(bar, field).isoformat()
    return value


class SandboxSession:
    """One immutable artifact and authorized universe per disposable container.

    No host paths, environment secrets, registry or future dataset are mounted or
    passed to the guest. Image IDs and optional OCI runtime are operator settings.
    """
    def __init__(self, artifact: PythonArtifact, *, symbols: list[str], image: str | None = None,
                 callback_seconds: float = 2., wall_seconds: float = 120., checkpoint=None, runtime: str | None = None, random_seed: int = 0):
        self.container_id = None
        self.create_uncertain = False
        self.create_notice = None
        self.process = self.watchdog = self.selector = None
        self.closed = False
        if type(random_seed) is not int:
            raise ValueError("SCHEMA_INVALID: random_seed must be an integer")
        self.checkpoint = checkpoint
        self.callback_seconds = callback_seconds
        self.wall_deadline = time.monotonic() + wall_seconds
        self.sequence = 0
        self.buffer = bytearray()
        self.output_bytes = 0
        self.total_output = 0
        self.stderr_tail = bytearray()
        self.artifact = PythonArtifact.model_validate(artifact.model_dump())
        if not 1 <= len(symbols) <= 500 or len(set(symbols)) != len(symbols) or any(not 1 <= len(s) <= 64 for s in symbols):
            raise ValueError("SANDBOX_POLICY_VIOLATION: expected a bounded explicit universe")
        self.symbols = frozenset(symbols)
        self.image = image or os.environ.get("QUANT_SANDBOX_IMAGE", "")
        self.runtime = runtime or os.environ.get("QUANT_SANDBOX_RUNTIME", "runc")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image):
            raise ValueError("SANDBOX_UNAVAILABLE: configure a trusted immutable QUANT_SANDBOX_IMAGE ID")
        if not validate_python(self.artifact.model_dump())["valid"]:
            raise ValueError("SCHEMA_INVALID: invalid Python artifact")
        try:
            self.preflight(self.image, self.runtime)
            if checkpoint:
                checkpoint()
            name = "stock-quant-" + uuid4().hex
            command = ["create", "--name", name, "--label", "stock-quant.sandbox=v1", "--network", "none",
                       "--read-only", "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                       "--pids-limit", "32", "--memory", "128m", "--memory-swap", "128m", "--cpus", "0.5",
                       "--ulimit", "cpu=30:30", "--ulimit", "nofile=64:64", "--ulimit", "fsize=1048576:1048576",
                       "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8388608,mode=1777", "--workdir", "/tmp",
                       "--ipc", "none", "--log-driver", "none", "--restart", "no", "--stop-timeout", "0", "-i",
                       "--entrypoint", "/usr/local/bin/python"]
            command += ["--runtime", self.runtime]
            command += [self.image, "-I", "-u", "-c", Path(__file__).with_name("sandbox_guest.py").read_text()]
            # Separate trusted process so a worker crash cannot leave untrusted
            # code running indefinitely in the daemon. Track the name and start
            # cleanup before create; its reply can be lost after daemon commit.
            self.container_id = name
            self.create_uncertain = True
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
            notice_read, self.create_notice = os.pipe()
            try:
                self.watchdog = subprocess.Popen([sys.executable, "-m", "stock_quant.sandbox_watchdog", self.container_id,
                                                  str(time.time() + wall_seconds), str(notice_read)], env=env, pass_fds=(notice_read,), stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            finally:
                os.close(notice_read)
            self.container_id = self._docker(*command).strip()
            self._settle_create()
            self.create_uncertain = False
            self.process = subprocess.Popen(["docker", "start", "--attach", "--interactive", self.container_id],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.selector = selectors.DefaultSelector()
            for stream in (self.process.stdout, self.process.stderr):
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ)
            os.set_blocking(self.process.stdin.fileno(), False)
            deadline = min(self.wall_deadline, time.monotonic() + 5)
            self._send({"type": "load", "id": 0, "source": self.artifact.source, "class_name": self.artifact.class_name,
                        "random_seed": random_seed}, deadline)
            response = self._receive(deadline)
            if response != {"type": "ready", "id": 0}:
                raise ValueError("SANDBOX_EXECUTION_FAILED: " + str(response)[:1024])
        except BaseException:
            self.close()
            raise

    @staticmethod
    def preflight(image: str, runtime: str | None = None):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("SANDBOX_UNAVAILABLE: immutable operator image is required")
        runtime = runtime or os.environ.get("QUANT_SANDBOX_RUNTIME", "runc")
        policy = json.loads(SandboxSession._docker("info", "--format", '{"security":{{json .SecurityOptions}},"memory":{{json .MemoryLimit}},"swap":{{json .SwapLimit}},"pids":{{json .PidsLimit}},"cpu":{{json .CPUCfsQuota}},"runtimes":{{json .Runtimes}}}'))
        if runtime not in policy["runtimes"]:
            raise ValueError("SANDBOX_UNAVAILABLE: operator OCI runtime is not installed")
        if not any("seccomp" in item for item in policy["security"]) or not all(policy[name] is True for name in ("memory", "swap", "pids", "cpu")):
            raise ValueError("SANDBOX_UNAVAILABLE: seccomp and cgroup resource limits are required")
        # Never pull/resolve a tag or permit an image-declared host/anonymous volume.
        record = json.loads(SandboxSession._docker("image", "inspect", image))[0]
        if record["Config"].get("Volumes"):
            raise ValueError("SANDBOX_UNAVAILABLE: sandbox images must not declare volumes")

    @staticmethod
    def _docker(*args) -> str:
        try:
            result = subprocess.run(["docker", *args], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("SANDBOX_UNAVAILABLE: Docker control failed") from error
        if result.returncode != 0:
            raise ValueError("SANDBOX_UNAVAILABLE: Docker rejected the requested policy: " + result.stderr.decode(errors="replace")[:1024])
        return result.stdout.decode()

    def _probe(self, deadline):
        if self.checkpoint:
            self.checkpoint()
        if time.monotonic() >= min(deadline, self.wall_deadline):
            raise TimeoutError("RESOURCE_LIMIT_EXCEEDED: sandbox wall/callback deadline exceeded")

    def _send(self, message, deadline):
        data = (json.dumps(message, allow_nan=False) + "\n").encode()
        if len(data) > 262144:
            raise ValueError("RESOURCE_LIMIT_EXCEEDED: sandbox input frame exceeds 256 KiB")
        offset = 0
        while offset < len(data):
            self._probe(deadline)
            try:
                offset += os.write(self.process.stdin.fileno(), data[offset:])
            except BlockingIOError:
                # Drain both streams while the peer is applying backpressure.
                self._drain(deadline)
            except BrokenPipeError as error:
                raise ValueError("SANDBOX_EXECUTION_FAILED: guest input closed") from error

    def _drain(self, deadline):
        self._probe(deadline)
        stdout_closed = False
        for key, _ in self.selector.select(.05):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                self.selector.unregister(key.fileobj)
                if key.fileobj is self.process.stdout:
                    stdout_closed = True
                continue
            self.output_bytes += len(chunk)
            self.total_output += len(chunk)
            if self.output_bytes > 131072 or self.total_output > 1048576:
                raise ValueError("RESOURCE_LIMIT_EXCEEDED: sandbox output budget exceeded")
            if key.fileobj is self.process.stdout:
                self.buffer.extend(chunk)
                if len(self.buffer) > 65536:
                    raise ValueError("RESOURCE_LIMIT_EXCEEDED: sandbox output frame exceeds 64 KiB")
            else:
                self.stderr_tail.extend(chunk)
                self.stderr_tail = self.stderr_tail[-4096:]
        if stdout_closed:
            raise ValueError("SANDBOX_EXECUTION_FAILED: guest exited or exhausted resources: " + self.stderr_tail.decode(errors="replace"))

    def _receive(self, deadline):
        while True:
            self._probe(deadline)
            if b"\n" in self.buffer:
                raw, _, remaining = self.buffer.partition(b"\n")
                self.buffer = bytearray(remaining)
                try:
                    message = json.loads(raw)
                except (ValueError, RecursionError) as error:
                    raise ValueError("SANDBOX_POLICY_VIOLATION: invalid guest JSON") from error
                if not isinstance(message, dict):
                    raise ValueError("SANDBOX_POLICY_VIOLATION: expected guest object")
                return message
            self._drain(deadline)

    def callback(self, event: Literal["initialize", "on_bar", "on_fill"], *, now: datetime,
                 snapshot: Snapshot, positions: dict[str, float], equity: float,
                 bar: MarketBar | None = None, fill: dict | None = None) -> list[dict]:
        if self.closed:
            raise ValueError("SANDBOX_EXECUTION_FAILED: session already closed")
        if now.tzinfo is None or now.utcoffset() is None or event not in ("initialize", "on_bar", "on_fill"):
            raise ValueError("SANDBOX_POLICY_VIOLATION: invalid host lifecycle clock/event")
        if bar is not None and (bar.available_at > now or bar.symbol not in self.symbols):
            raise ValueError("SANDBOX_POLICY_VIOLATION: unauthorized/future host bar")
        if event == "on_fill":
            try:
                view = FillView.model_validate(fill)
                if view.symbol not in self.symbols or view.time > now:
                    raise ValueError("Unauthorized/future fill")
            except ValueError as error:
                raise ValueError("SANDBOX_POLICY_VIOLATION: invalid fill view") from error
            fill = view.model_dump(mode="json")
        elif fill is not None:
            raise ValueError("SANDBOX_POLICY_VIOLATION: fill outside on_fill")
        self.sequence += 1
        self.output_bytes = 0
        deadline = time.monotonic() + self.callback_seconds
        message = {"type": "event", "id": self.sequence, "event": event, "now": now.isoformat(),
                   "positions": {s: positions.get(s, 0.) for s in self.symbols}, "equity": equity,
                   "bar": bar_payload(bar) if bar is not None else None, "fill": fill}
        try:
            self._send(message, deadline)
            histories, history_bytes = 0, 0
            while True:
                response = self._receive(deadline)
                if response.get("type") == "history":
                    histories += 1
                    try:
                        request = HistoryRequest.model_validate(response)
                    except ValidationError as error:
                        raise ValueError("SANDBOX_POLICY_VIOLATION: invalid history request") from error
                    if histories > 100 or request.event_id != self.sequence or request.symbol not in self.symbols or request.timeframe not in self.artifact.data_timeframes:
                        raise ValueError("SANDBOX_POLICY_VIOLATION: unauthorized history request")
                    rows = [bar_payload(b) for b in snapshot.history(request.symbol, request.timeframe, now, request.bars)]
                    reply = {"type": "history_result", "request_id": request.request_id, "rows": rows}
                    history_bytes += len(json.dumps(reply).encode())
                    if history_bytes > 1048576:
                        raise ValueError("RESOURCE_LIMIT_EXCEEDED: history callback exceeds 1 MiB")
                    self._send(reply, deadline)
                elif response.get("type") == "error":
                    raise ValueError("SANDBOX_EXECUTION_FAILED: " + str(response.get("message"))[:1024])
                elif response.get("type") == "result" and type(response.get("id")) is int and response.get("id") == self.sequence and set(response) == {"type", "id", "value"}:
                    value = response["value"]
                    try:
                        if not isinstance(value, list) or len(value) > 50:
                            raise ValueError("Expected at most 50 intents")
                        intents = INTENTS.validate_python(value)
                        if event != "on_bar" and intents or any(intent.symbol not in self.symbols for intent in intents):
                            raise ValueError("Unauthorized intent")
                    except ValueError as error:
                        raise ValueError("SANDBOX_POLICY_VIOLATION: invalid order intents") from error
                    return [intent.model_dump() for intent in intents]
                else:
                    raise ValueError("SANDBOX_POLICY_VIOLATION: unexpected guest message")
        except BaseException:
            self.close()
            raise

    def _settle_create(self):
        if self.create_notice is not None:
            try:
                os.write(self.create_notice, b"1")
            except BrokenPipeError:
                pass
            finally:
                os.close(self.create_notice)
                self.create_notice = None

    def close(self):
        if self.closed:
            return
        self.closed = True
        removed = self.container_id is None
        if self.container_id:
            committed = not self.create_uncertain
            if not committed:
                try:
                    record = json.loads(self._docker("inspect", self.container_id))[0]
                    committed = ((record["Config"].get("Labels") or {}).get("stock-quant.sandbox") == "v1"
                                 and record["Name"] == "/" + self.container_id)
                except (ValueError, KeyError, IndexError):
                    pass
            try:
                self._docker("rm", "--force", self.container_id)
                removed = True
                # Docker rm may succeed for an absent name. Only an actual
                # observation or create acknowledgement establishes commit.
                if committed:
                    self.create_uncertain = False
                    self._settle_create()
            except ValueError as error:
                removed = "no such container" in str(error).lower() or "no such object" in str(error).lower()
                # Otherwise the detached watchdog remains responsible.
        if self.create_notice is not None:
            # EOF without acknowledgement means creation remains uncertain.
            os.close(self.create_notice)
            self.create_notice = None
        if self.selector:
            self.selector.close()
        if self.process:
            self.process.kill()
            self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
        if self.watchdog and removed and not self.create_uncertain:
            self.watchdog.terminate()
            self.watchdog.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
