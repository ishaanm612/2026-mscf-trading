"""Minimal RIT REST transport for the volatility bot.

Adapted from client.py in the partner repo
(https://github.com/ishaanm612/2026-mscf-trading), trimmed to the handful of
helpers this bot actually uses. One deliberate difference: a 429 (rate-limited)
response is retried for POSTs too, because 429 means the exchange rejected the
request before processing it, so a retry cannot double-trade.
"""
import base64
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class Client:
    def __init__(self, url=None, api_key=None):
        """RIT_USERNAME/RIT_PASSWORD selects DMA basic auth (remote server);
        otherwise RIT_API_KEY is used against a local RIT client."""
        self.url = (url or os.environ.get("RIT_API_URL", "http://localhost:9999/v1")).rstrip("/")
        username = os.environ.get("RIT_USERNAME")
        if username:
            credentials = f"{username}:{os.environ.get('RIT_PASSWORD', '')}"
            self.headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
        else:
            self.headers = {"X-API-Key": api_key or os.environ.get("RIT_API_KEY", "Rotman")}

    def get(self, endpoint, **params):
        return self.request("GET", endpoint, **params)

    def request(self, method, endpoint, **params):
        request = Request(f"{self.url}/{endpoint}?{urlencode(params)}",
                          headers=self.headers, method=method)
        for attempt in range(5):
            try:
                with urlopen(request, timeout=5) as response:
                    body = response.read()
                    return json.loads(body) if body else {}
            except HTTPError as error:
                if error.code != 429 or attempt == 4:
                    raise RuntimeError(f"RIT {method} {endpoint}: HTTP {error.code}") from None
                try:
                    delay = float(error.headers.get("Retry-After", "0.5"))
                except (TypeError, ValueError):
                    delay = 0.5
                time.sleep(max(0.1, min(delay, 5)))
