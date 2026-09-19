"""Serial execution with durable intent journal and confirmed terminal fills."""
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping
from client import RITReadError


def _read_events(path: Path) -> list[dict[str, Any]]:
    """Decode compact or pretty-printed consecutive JSON journal records.

    Earlier runners wrote one indented JSON object after another.  Both formats
    describe the same append-only journal and must remain readable for safe
    restart checks; an incomplete trailing object is rejected by the caller.

    :param path: Journal path to decode.
    :returns: Complete journal records in write order.
    :raises ValueError: If the journal contains malformed or non-object data.
    """

    decoder = json.JSONDecoder()
    content, offset, events = path.read_text(), 0, []
    while offset < len(content):
        while offset < len(content) and content[offset].isspace():
            offset += 1
        if offset >= len(content):
            break
        event, offset = decoder.raw_decode(content, offset)
        if not isinstance(event, dict):
            raise ValueError("Execution journal contains a non-object record")
        events.append(event)
    return events


class Executor:
    def __init__(self, client: Any, journal: str | Path) -> None:
        """Open a single-process execution journal.

        :param client: RIT API client.
        :param journal: Durable JSONL journal path.
        :raises RuntimeError: If the prior journal has unresolved activity.
        """
        self.client = client
        self.unresolved_intent = False
        self.path = Path(journal)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive process lock prevents two runners from sharing an account journal.
        self.lock = self.path.with_suffix(".lock")
        self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            if self.path.exists():
                events = _read_events(self.path)
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
        started = time.monotonic()
        self.log("intent", ticker=ticker, quantity=quantity)
        self.unresolved_intent = True
        result = self.client.request("POST", "orders", ticker=ticker, type="MARKET",
                                     action="BUY" if quantity > 0 else "SELL", quantity=abs(quantity))
        order_id = result["order_id"]
        self.log("submitted", order_id=order_id)
        for _ in range(12):
            order = self.client.get(f"orders/{order_id}")
            if order["quantity_filled"] == abs(quantity) and order["status"] != "OPEN":
                self.log("filled", order_id=order_id, ticker=ticker, quantity=quantity,
                         duration_seconds=time.monotonic() - started, order=dict(order))
                self.unresolved_intent = False
                return order
            if order["status"] != "OPEN":
                self.log("incomplete", order_id=order_id, filled=order["quantity_filled"])
                raise RuntimeError("Incomplete fill; halted for reconciliation")
            time.sleep(.25)
        self.client.request("DELETE", f"orders/{order_id}")
        self.log("cancel_requested", order_id=order_id)
        raise RuntimeError("Order did not complete; cancellation requested, reconcile before restart")

    def tender(self, offer: Mapping[str, Any], position_before: int) -> None:
        """Accept once, then allow bounded read-only settlement confirmation.

        DMA can acknowledge acceptance before securities reflects the tender.
        Poll only an unchanged pre-tender position; a partial/unexpected change,
        session boundary, read failure, or timeout leaves the intent unresolved.
        Neither acceptance nor an ambiguous mutation is ever retried.
        """
        case = self.client.get("case")
        if (case["status"] != "ACTIVE" or case["tick"] >= 299
                or offer.get("period", case.get("period")) != case.get("period")
                or case["tick"] > offer.get("expires", case["tick"])):
            raise RITReadError("Tender preflight expired; no acceptance submitted")
        started = time.monotonic()
        expected = position_before + offer["quantity"] * (1 if offer["action"] == "BUY" else -1)
        self.log("tender_intent", tender_id=offer["tender_id"], ticker=offer["ticker"],
                 action=offer["action"], quantity=offer["quantity"], price=offer["price"],
                 position_before=position_before, expected_position=expected, case=case)
        self.unresolved_intent = True
        response = self.client.request("POST", f"tenders/{offer['tender_id']}", price=offer["price"])
        self.log("tender_submitted", tender_id=offer["tender_id"], response=response)
        last_tick = case["tick"]
        for attempt in range(12):
            before = self.client.get("case")
            positions = {s["ticker"]: s["position"] for s in self.client.get("securities")}
            after = self.client.get("case")
            for marker in (before, after):
                if (marker.get("period") != case.get("period") or marker["tick"] < last_tick
                        or marker["status"] != "ACTIVE"):
                    self.log("tender_confirmation_failed", tender_id=offer["tender_id"],
                             reason="session changed", case=marker, positions=positions)
                    raise RuntimeError("Session changed during tender confirmation; reconcile before restart")
                last_tick = marker["tick"]
            observed = positions.get(offer["ticker"])
            if observed == expected:
                self.log("tender_confirmed", tender_id=offer["tender_id"], position=observed,
                         attempts=attempt + 1, duration_seconds=time.monotonic() - started, case=after)
                self.unresolved_intent = False
                return
            if observed != position_before:
                self.log("tender_confirmation_failed", tender_id=offer["tender_id"],
                         reason="unexpected position", observed_position=observed, expected_position=expected)
                raise RuntimeError("Unexpected tender position; reconcile before restart")
            if attempt < 11:
                time.sleep(.25)
        self.log("tender_confirmation_failed", tender_id=offer["tender_id"],
                 reason="confirmation timeout", observed_position=observed, expected_position=expected)
        raise RuntimeError("Tender position is not confirmed; reconcile before restart")

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
