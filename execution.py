"""Serial execution with durable intent journal and confirmed terminal fills."""
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping


class Executor:
    def __init__(self, client: Any, journal: str | Path) -> None:
        """Open a single-process execution journal.

        :param client: RIT API client.
        :param journal: Durable JSONL journal path.
        :raises RuntimeError: If the prior journal has unresolved activity.
        """
        self.client = client
        self.path = Path(journal)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive process lock prevents two runners from sharing an account journal.
        self.lock = self.path.with_suffix(".lock")
        self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            if self.path.exists():
                events = [json.loads(line) for line in self.path.read_text().splitlines()]
                if events and events[-1]["event"] not in ("filled", "tender_confirmed", "reconciled"):
                    raise RuntimeError("Unresolved execution journal; inspect account and reconcile before restart")
        except BaseException:
            self.close()
            raise

    def log(self, event: str, **fields: Any) -> None:
        """Flush intent to disk before contacting the exchange, including on process failure."""
        with self.path.open("a") as f:
            f.write(json.dumps({"time": time.time(), "event": event, **fields}) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def order(self, ticker: str, quantity: int) -> Mapping[str, Any]:
        """Submit once, then poll by ID. Partial or ambiguous fills stop the runner.

        A request timeout does NOT imply rejection. Leaving an unresolved intent
        in the journal prevents an accidental duplicate order after restart.
        """
        self.log("intent", ticker=ticker, quantity=quantity)
        result = self.client.request("POST", "orders", ticker=ticker, type="MARKET",
                                     action="BUY" if quantity > 0 else "SELL", quantity=abs(quantity))
        order_id = result["order_id"]
        self.log("submitted", order_id=order_id)
        for _ in range(12):
            order = self.client.get(f"orders/{order_id}")
            if order["quantity_filled"] == abs(quantity) and order["status"] != "OPEN":
                self.log("filled", order_id=order_id, quantity=quantity)
                return order
            if order["status"] != "OPEN":
                self.log("incomplete", order_id=order_id, filled=order["quantity_filled"])
                raise RuntimeError("Incomplete fill; halted for reconciliation")
            time.sleep(.25)
        self.client.request("DELETE", f"orders/{order_id}")
        self.log("cancel_requested", order_id=order_id)
        raise RuntimeError("Order did not complete; cancellation requested, reconcile before restart")

    def tender(self, offer: Mapping[str, Any], position_before: int) -> None:
        """Accept a fixed offer once and verify its inventory effect before unwinding."""
        self.log("tender_intent", tender_id=offer["tender_id"])
        self.client.request("POST", f"tenders/{offer['tender_id']}", price=offer["price"])
        expected = position_before + offer["quantity"] * (1 if offer["action"] == "BUY" else -1)
        positions = {s["ticker"]: s["position"] for s in self.client.get("securities")}
        if positions[offer["ticker"]] != expected:
            raise RuntimeError("Tender position is not confirmed; reconcile before restart")
        self.log("tender_confirmed", tender_id=offer["tender_id"])

    def close(self) -> None:
        """Release the exclusive journal lock.

        The journal remains as the durable audit record.
        """
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            self.lock.unlink()

    @staticmethod
    def reconcile(journal: str | Path, snapshot: Mapping[str, Any]) -> None:
        """Explicit recovery command: capture actual positions after resolving open orders.

        This acknowledges current state; it does not pretend the previous order
        failed or erase its audit trail. Resume ETF with --flatten-only after a
        partial basket; volatility naturally hedges existing inventory first.
        """
        if snapshot.get("orders") != []:
            raise RuntimeError("Resolve all open orders before acknowledging reconciliation")
        path = Path(journal)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(".lock")
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with path.open("a") as output:
                output.write(json.dumps({"event": "reconciled", "time": time.time(),
                                         "case": snapshot["case"],
                                         "positions": {s["ticker"]: s["position"] for s in snapshot["securities"]}}) + "\n")
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(fd)
            lock.unlink()
