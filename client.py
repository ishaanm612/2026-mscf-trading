"""RIT transport. Mutations are never retried automatically."""
import base64
import json
import os
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Client:
    def __init__(self, url: str | None = None) -> None:
        """Configure REST or DMA authentication from environment variables.

        :param url: Optional API base URL overriding ``RIT_API_URL``.
        """
        self.url = (url or os.environ.get("RIT_API_URL", "http://localhost:9999/v1")).rstrip("/")
        mode = os.environ.get("RIT_API_MODE", "rest")
        if mode == "dma":
            credentials = f"{os.environ['RIT_USERNAME']}:{os.environ['RIT_PASSWORD']}"
            self.headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
        elif mode == "rest":
            self.headers = {"X-API-Key": os.environ["RIT_API_KEY"]}
        else:
            raise ValueError("RIT_API_MODE must be rest or dma")

    def get(self, endpoint: str, **params: Any) -> Any:
        """Issue a retryable RIT read request.

        :param endpoint: Relative API endpoint.
        :param params: Query-string parameters.
        :returns: Decoded JSON response.
        """
        return self.request("GET", endpoint, **params)

    def request(self, method: str, endpoint: str, **params: Any) -> Any:
        """Issue one API request, retrying only rate-limited reads.

        :param method: HTTP method.
        :param endpoint: Relative API endpoint.
        :param params: Query-string parameters.
        :returns: Decoded JSON response.
        """
        request = Request(f"{self.url}/{endpoint}?{urlencode(params)}", headers=self.headers,
                          method=method)
        for attempt in range(3):
            try:
                with urlopen(request, timeout=5) as response:
                    body = response.read()
                    return json.loads(body) if body else {}
            except HTTPError as error:
                if method != "GET" or error.code != 429 or attempt == 2:
                    raise RuntimeError(f"RIT {method} {endpoint}: HTTP {error.code}") from None
                try:
                    delay = float(error.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1
                time.sleep(max(0.1, min(delay, 10)))

    def snapshot(self, case: str, trading: bool = False) -> dict[str, Any]:
        """Collect a short coherent snapshot and reject stale case transitions.

        :param case: ``etf`` or ``volatility``.
        :param trading: Include orders and limits required by pre-trade checks.
        :returns: Snapshot suitable for strategy analysis.
        """
        state = self.get("case")
        result = {"case": state, "securities": self.get("securities")}
        if case == "etf":
            result["books"] = {t: self.get("securities/book", ticker=t, limit=100)
                               for t in ("BULL", "BEAR", "RITC", "USD")}
            result["tenders"] = self.get("tenders")
        else:
            result["news"] = self.get("news")
        if trading:
            result["orders"] = self.get("orders", status="OPEN")
            result["limits"] = self.get("limits")
        end = self.get("case")
        if (end.get("period") != state.get("period") or end["tick"] < state["tick"]
                or end["tick"] - state["tick"] > 2 or end["status"] != state["status"]):
            raise RuntimeError("Case changed or snapshot took more than two ticks; retry with fresh data")
        result["case"] = end
        return result
