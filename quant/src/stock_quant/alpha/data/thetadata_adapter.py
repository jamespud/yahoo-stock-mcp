"""ThetaData v3 historical option adapter with injected transport for fixtures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
import os

from ..contracts import OptionDatasetRequest
from ..errors import AlphaError, AlphaErrorCode
from ..options.contracts import OptionContract, option_contract_id
from ..options.quotes import OptionQuote
from .option_dataset import OptionSnapshotBatch


class ThetaDataV3ResponseError(ValueError):
    pass


class ThetaDataV3Client(Protocol):
    def get(self, path: str, params: dict) -> dict: ...


class ThetaDataV3Provider:
    def __init__(self, client: ThetaDataV3Client):
        self.client = client

    def read(self, request: OptionDatasetRequest) -> OptionSnapshotBatch:
        if request.source != "thetadata" or request.quote_policy != "historical_bid_ask":
            raise AlphaError(AlphaErrorCode.OPTION_HISTORY_UNAVAILABLE, "ThetaData adapter requires historical_bid_ask policy")
        rows = []
        try:
            for underlying in request.underlyings:
                params = {
                    "root": underlying,
                    "start_date": request.start.date().isoformat(),
                    "end_date": request.end.date().isoformat(),
                }
                response = self.client.get("/v3/option/history/quote", params)
                self._validate_envelope(response)
                for item in response.get("response", []):
                    rows.append(self._convert(item, underlying))
        except PermissionError as error:
            raise AlphaError(AlphaErrorCode.PROVIDER_ACCESS_DENIED, str(error), {"provider": "thetadata"}) from error
        except ThetaDataV3ResponseError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            if "403" in str(error) or "401" in str(error):
                raise AlphaError(AlphaErrorCode.PROVIDER_ACCESS_DENIED, str(error), {"provider": "thetadata"}) from error
            raise ThetaDataV3ResponseError(f"Invalid ThetaData v3 response: {error}") from error
        return OptionSnapshotBatch(
            rows=rows, source_version="thetadata-v3", quality={"provider": "thetadata", "quote_policy": request.quote_policy},
        )

    @staticmethod
    def _validate_envelope(response: dict) -> None:
        if not isinstance(response, dict) or not isinstance(response.get("response"), list):
            raise ThetaDataV3ResponseError("ThetaData v3 response must contain a response list")
        header = response.get("header", {})
        error_type = header.get("error_type") if isinstance(header, dict) else None
        if error_type not in (None, "", "null"):
            message = str(header.get("error_msg", error_type))
            if "403" in message or "401" in message or "subscription" in message.lower() or "permission" in message.lower():
                raise PermissionError(message)
            raise ThetaDataV3ResponseError(message)

    @staticmethod
    def _convert(item: dict, underlying: str) -> OptionQuote:
        quote_at = datetime.strptime(str(item["date"]), "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(milliseconds=int(item["ms_of_day"]))
        exercise = {"A": "american", "E": "european"}.get(str(item.get("exercise_style", "A")).upper(), "american")
        settlement = {"P": "physical", "C": "cash"}.get(str(item.get("settlement_type", "P")).upper(), "physical")
        option_type = "CALL" if str(item["right"]).upper().startswith("C") else "PUT"
        fields = {
            "underlying_id": underlying.upper(), "option_type": option_type,
            "strike": float(item["strike"]) / 1000.0,
            "expiration": datetime.strptime(str(item["expiration"]), "%Y%m%d").date(),
            "exercise_style": exercise, "settlement_type": settlement,
            "multiplier": int(item.get("multiplier", 100)), "currency": "USD",
            "deliverable": f"{int(item.get('multiplier', 100))} shares {underlying.upper()}",
            "adjustment_version": str(item.get("contract") or "thetadata-v3"),
        }
        contract = OptionContract(contract_id=option_contract_id(**fields), **fields)
        return OptionQuote(
            contract=contract, observed_at=quote_at, quote_at=quote_at, available_at=quote_at + timedelta(minutes=1),
            bid=float(item["bid"]) / 100.0, ask=float(item["ask"]) / 100.0,
            bid_size=int(item.get("bid_size", 0)), ask_size=int(item.get("ask_size", 0)),
            last_price=float(item["last"]) / 100.0 if item.get("last") is not None else None,
            last_trade_at=None, volume=int(item.get("volume", 0)), open_interest=int(item.get("open_interest", 0)),
            implied_volatility=float(item["implied_volatility"]) if item.get("implied_volatility") is not None else None,
            source="thetadata", source_version="thetadata-v3", quality_flags=[],
        )


class ThetaDataHttpClient:
    """Authenticated v3 transport. Access denial is surfaced, never bypassed."""
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        if not base_url or not api_key:
            raise ValueError("ThetaData base URL and API key are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "ThetaDataHttpClient":
        return cls(os.environ.get("THETADATA_BASE_URL", "https://api.thetadata.us"), os.environ.get("THETADATA_API_KEY", ""))

    def get(self, path: str, params: dict) -> dict:
        try:
            import httpx
        except ImportError as error:
            raise AlphaError(AlphaErrorCode.OPTION_HISTORY_UNAVAILABLE, "Install the theta optional dependency to access ThetaData") from error
        try:
            response = httpx.get(self.base_url + path, params=params, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout)
        except httpx.HTTPError as error:
            raise AlphaError(AlphaErrorCode.DATA_UNAVAILABLE, "ThetaData request failed", {"reason": str(error)}) from error
        if response.status_code in (401, 403):
            raise PermissionError(f"ThetaData HTTP {response.status_code}: subscription or permission required")
        response.raise_for_status()
        return response.json()
