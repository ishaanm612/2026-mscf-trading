"""Read-only transport following the official REST/DMA starter scripts."""
import base64
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Client:
    def __init__(self):
        self.url = os.environ.get("RIT_API_URL", "http://localhost:9999/v1").rstrip("/")
        mode = os.environ.get("RIT_API_MODE", "rest")
        if mode == "dma":
            credentials = f"{os.environ['RIT_USERNAME']}:{os.environ['RIT_PASSWORD']}"
            self.headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
        elif mode == "rest":
            self.headers = {"X-API-Key": os.environ["RIT_API_KEY"]}
        else:
            raise ValueError("RIT_API_MODE must be rest or dma")

    def get(self, endpoint, **params):
        request = Request(f"{self.url}/{endpoint}?{urlencode(params)}", headers=self.headers)
        for attempt in range(3):
            try:
                with urlopen(request, timeout=5) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code != 429 or attempt == 2:
                    raise RuntimeError(f"RIT GET {endpoint}: HTTP {error.code}") from None
                try:
                    delay = float(error.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1
                time.sleep(max(0.1, min(delay, 10)))

    def snapshot(self, case):
        state = self.get("case")
        result = {"case": state, "securities": self.get("securities")}
        if case == "etf":
            result["books"] = {t: self.get("securities/book", ticker=t, limit=100)
                               for t in ("BULL", "BEAR", "RITC", "USD")}
            result["tenders"] = self.get("tenders")
        else:
            result["news"] = self.get("news")
        return result
