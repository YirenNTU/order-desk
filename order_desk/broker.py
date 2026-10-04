"""Serialized broker adapters. Real Shioaji access is lazy and production-locked."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
from concurrent.futures import Future
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from order_desk.models import BrokerPosition, OrderIntent, Quote


class BrokerError(RuntimeError):
    """Broker connection or order operation failed."""


@dataclass(frozen=True)
class BrokerOrderResult:
    broker_order_id: str
    status: str
    filled_shares: int = 0


class BrokerAdapter(Protocol):
    def connect(self) -> None: ...
    def close(self) -> None: ...
    def health(self) -> Mapping[str, Any]: ...
    def positions(self) -> list[BrokerPosition]: ...
    def available_cash(self) -> float: ...
    def settlements(self) -> list[Mapping[str, Any]]: ...
    def quotes(self, tickers: list[str]) -> dict[str, Quote]: ...
    def place_order(self, intent: OrderIntent) -> BrokerOrderResult: ...
    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult: ...
    def reconcile_orders(self) -> list[Mapping[str, Any]]: ...


class FakeBroker:
    """Deterministic in-memory broker used by tests and dry-runs."""

    def __init__(
        self,
        quotes: Mapping[str, Quote] | None = None,
        positions: Mapping[str, int] | None = None,
        cash: float = 10_000_000.0,
        *,
        auto_fill: bool = True,
        fail_after_accept: bool = False,
    ):
        self._quotes = dict(quotes or {})
        self._positions = dict(positions or {})
        self._cash = float(cash)
        self.auto_fill = auto_fill
        self.fail_after_accept = fail_after_accept
        self.connected = False
        self.orders: dict[str, dict[str, Any]] = {}

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def health(self) -> Mapping[str, Any]:
        return {"connected": self.connected, "simulation": True, "broker": "fake"}

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(ticker=ticker, shares=shares)
            for ticker, shares in sorted(self._positions.items())
            if shares
        ]

    def available_cash(self) -> float:
        return self._cash

    def settlements(self) -> list[Mapping[str, Any]]:
        return []

    def set_quotes(self, values: Mapping[str, Quote]) -> None:
        self._quotes.update(values)

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        missing = sorted(set(tickers) - set(self._quotes))
        if missing:
            raise BrokerError(f"fake broker missing quotes: {', '.join(missing)}")
        return {ticker: self._quotes[ticker] for ticker in tickers}

    def place_order(self, intent: OrderIntent) -> BrokerOrderResult:
        if intent.client_order_id in self.orders:
            current = self.orders[intent.client_order_id]
            return BrokerOrderResult(
                current["broker_order_id"], current["status"], current["filled_shares"]
            )
        broker_order_id = f"F{len(self.orders) + 1:08d}"
        status = "Filled" if self.auto_fill else "Submitted"
        filled = intent.shares if self.auto_fill else 0
        self.orders[intent.client_order_id] = {
            **asdict(intent),
            "broker_order_id": broker_order_id,
            "status": status,
            "filled_shares": filled,
        }
        if self.auto_fill:
            sign = 1 if intent.action == "Buy" else -1
            current = self._positions.get(intent.ticker, 0)
            if sign < 0 and current < intent.shares:
                raise BrokerError("fake broker rejected oversell")
            self._positions[intent.ticker] = current + sign * intent.shares
            notional = intent.limit_price * intent.shares
            self._cash += -notional if sign > 0 else notional
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise TimeoutError("simulated timeout after broker acceptance")
        return BrokerOrderResult(broker_order_id, status, filled)

    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        for order in self.orders.values():
            if order["broker_order_id"] == broker_order_id:
                if order["status"] == "Submitted":
                    order["status"] = "Cancelled"
                return BrokerOrderResult(
                    broker_order_id, order["status"], order["filled_shares"]
                )
        raise BrokerError("order not found")

    def fill_order(self, client_order_id: str, shares: int | None = None) -> None:
        order = self.orders[client_order_id]
        target = int(order["shares"])
        new_filled = target if shares is None else min(target, int(shares))
        previous = int(order["filled_shares"])
        delta = new_filled - previous
        if delta < 0:
            raise BrokerError("fake fills cannot move backward")
        if delta:
            sign = 1 if order["action"] == "Buy" else -1
            current = self._positions.get(order["ticker"], 0)
            if sign < 0 and current < delta:
                raise BrokerError("fake broker rejected oversell")
            self._positions[order["ticker"]] = current + sign * delta
            self._cash += -order["limit_price"] * delta if sign > 0 else order["limit_price"] * delta
        order["filled_shares"] = new_filled
        order["status"] = "Filled" if new_filled == target else "PartFilled"

    def reconcile_orders(self) -> list[Mapping[str, Any]]:
        return [dict(value) for value in self.orders.values()]


def _object_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        if isinstance(result, dict):
            return result
    if hasattr(value, "model_dump"):
        result = value.model_dump()
        if isinstance(result, dict):
            return result
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": str(value)}


class ShioajiBroker:
    """One Shioaji session. Call only through :class:`BrokerWorker`."""

    PRODUCTION_CONFIRMATION = "YES_I_ACCEPT_REAL_ORDERS"

    def __init__(
        self,
        mode: str = "simulation",
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ):
        if mode not in {"simulation", "production"}:
            raise BrokerError("Shioaji mode must be simulation or production")
        if mode == "production" and os.getenv("TRADING_ALLOW_PRODUCTION") != self.PRODUCTION_CONFIRMATION:
            raise BrokerError("production is locked by TRADING_ALLOW_PRODUCTION")
        self.mode = mode
        self.event_sink = event_sink
        self.api: Any = None
        self.account: Any = None
        self._trades: dict[str, Any] = {}
        self._trade_intents: dict[str, OrderIntent] = {}

    def connect(self) -> None:
        try:
            import shioaji as sj
        except ImportError as exc:
            raise BrokerError("shioaji is not installed") from exc
        api_key = os.getenv("SJ_API_KEY")
        secret_key = os.getenv("SJ_SEC_KEY")
        if not api_key or not secret_key:
            raise BrokerError("SJ_API_KEY and SJ_SEC_KEY are required")
        self.api = sj.Shioaji(simulation=self.mode != "production")
        accounts = self.api.login(
            api_key=api_key,
            secret_key=secret_key,
            subscribe_trade=True,
        )
        self.account = self.api.stock_account
        if self.account is None:
            raise BrokerError("no SinoPac stock account is available")
        actual_account = str(getattr(self.account, "account_id", ""))
        expected_account = os.getenv("SJ_ACCOUNT_ID", "")
        if self.mode == "production" and not expected_account:
            raise BrokerError("production requires an explicit SJ_ACCOUNT_ID")
        if expected_account and actual_account != expected_account:
            raise BrokerError("logged-in stock account does not match SJ_ACCOUNT_ID")
        if self.mode == "production":
            if not bool(getattr(self.account, "signed", False)):
                raise BrokerError("stock account has not completed API signing/testing")
            ca_path = os.getenv("SJ_CA_PATH")
            ca_password = os.getenv("SJ_CA_PASSWD")
            if not ca_path or not ca_password:
                raise BrokerError("production requires SJ_CA_PATH and SJ_CA_PASSWD")
            try:
                from cryptography.hazmat.primitives.serialization.pkcs12 import (
                    load_key_and_certificates,
                )

                _, certificate, _ = load_key_and_certificates(
                    Path(ca_path).read_bytes(), ca_password.encode("utf-8")
                )
            except Exception as exc:
                raise BrokerError("CA file cannot be opened with SJ_CA_PASSWD") from exc
            if certificate is None:
                raise BrokerError("CA file contains no certificate")
            expires = certificate.not_valid_after_utc
            if expires <= datetime.now(timezone.utc):
                raise BrokerError("CA certificate has expired")
            if not self.api.activate_ca(
                ca_path=ca_path,
                ca_passwd=ca_password,
                person_id=getattr(self.account, "person_id", None),
            ):
                raise BrokerError("CA activation failed")
        self.api.set_order_callback(self._order_callback)

    def _order_callback(self, state: Any, message: Any) -> None:
        if self.event_sink is None:
            return
        payload = _object_dict(message)
        event_id = hashlib.sha256(
            json.dumps(
                {"state": str(state), "message": payload},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        self.event_sink(
            {
                "event_id": event_id,
                "event_type": str(state),
                "payload": payload,
            }
        )

    def close(self) -> None:
        if self.api is not None:
            self.api.logout()
        self.api = None

    def health(self) -> Mapping[str, Any]:
        account_id = str(getattr(self.account, "account_id", "")) if self.account else ""
        return {
            "connected": self.api is not None,
            "simulation": self.mode != "production",
            "broker": "shioaji",
            "account_suffix": account_id[-4:] if account_id else "",
            "signed": bool(getattr(self.account, "signed", False)) if self.account else False,
        }

    def positions(self) -> list[BrokerPosition]:
        if self.api is None:
            raise BrokerError("broker is not connected")
        import shioaji as sj

        values = self.api.list_positions(account=self.account, unit=sj.Unit.Share)
        return [
            BrokerPosition(ticker=str(value.code), shares=int(value.quantity))
            for value in values
            if str(
                getattr(
                    getattr(value, "direction", "Buy"),
                    "value",
                    getattr(value, "direction", "Buy"),
                )
            ).lower()
            == "buy"
        ]

    def available_cash(self) -> float:
        if self.api is None:
            raise BrokerError("broker is not connected")
        balance = self.api.account_balance(account=self.account)
        error = str(getattr(balance, "errmsg", ""))
        if error:
            raise BrokerError(f"account balance unavailable: {error}")
        return float(balance.acc_balance)

    def settlements(self) -> list[Mapping[str, Any]]:
        if self.api is None:
            raise BrokerError("broker is not connected")
        values = self.api.settlements(account=self.account)
        return [_object_dict(value) for value in values]

    def _stock_contract(self, ticker: str):
        try:
            contract = self.api.Contracts.Stocks[str(ticker)]
        except Exception as exc:
            raise BrokerError(f"contract not found: {ticker}") from exc
        if contract is None or getattr(contract, "limit_up", None) in (None, 0):
            raise BrokerError(f"contract not found: {ticker}")
        return contract

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        if self.api is None:
            raise BrokerError("broker is not connected")
        contracts = [self._stock_contract(ticker) for ticker in tickers]
        snapshots = self.api.snapshots(contracts)
        result: dict[str, Quote] = {}
        for contract, snapshot in zip(contracts, snapshots):
            bid = float(snapshot.buy_price)
            ask = float(snapshot.sell_price)
            last = float(snapshot.close)
            if bid <= 0 and ask <= 0 and last > 0:
                bid = ask = last
            elif bid <= 0 < ask:
                bid = last if last > 0 else ask
            elif ask <= 0 < bid:
                ask = last if last > 0 else bid
            result[str(contract.code)] = Quote(
                ticker=str(contract.code),
                bid=bid,
                ask=ask,
                last=last,
                limit_up=float(contract.limit_up),
                limit_down=float(contract.limit_down),
            )
        return result

    def place_order(self, intent: OrderIntent) -> BrokerOrderResult:
        if self.api is None:
            raise BrokerError("broker is not connected")
        import shioaji as sj

        if self.mode == "production" and os.getenv("TRADING_ALLOW_PRODUCTION") != self.PRODUCTION_CONFIRMATION:
            raise BrokerError("production lock changed; refusing order")
        contract = self._stock_contract(intent.ticker)
        order = sj.StockOrder(
            action=sj.Action.Buy if intent.action == "Buy" else sj.Action.Sell,
            price=intent.limit_price,
            quantity=intent.broker_quantity,
            price_type=sj.StockPriceType.LMT,
            order_type=sj.OrderType.ROD,
            order_lot=(
                sj.StockOrderLot.Common
                if intent.order_lot == "Common"
                else sj.StockOrderLot.IntradayOdd
            ),
            order_cond=sj.StockOrderCond.Cash,
            account=self.account,
            custom_field=intent.client_order_id[:6],
        )
        trade = self.api.place_order(contract, order)
        broker_id = str(trade.order.id)
        self._trades[broker_id] = trade
        self._trade_intents[broker_id] = intent
        return BrokerOrderResult(
            broker_order_id=broker_id,
            status=str(trade.status.status).split(".")[-1],
            filled_shares=int(getattr(trade.status, "deal_quantity", 0))
            * (1000 if intent.order_lot == "Common" else 1),
        )

    def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        trade = self._trades.get(broker_order_id)
        if trade is None:
            raise BrokerError("cannot cancel unknown in-process trade; reconcile first")
        self.api.cancel_order(trade)
        self.api.update_status(trade=trade)
        intent = self._trade_intents[broker_order_id]
        multiplier = 1000 if intent.order_lot == "Common" else 1
        return BrokerOrderResult(
            broker_order_id,
            str(trade.status.status).split(".")[-1],
            int(getattr(trade.status, "deal_quantity", 0)) * multiplier,
        )

    def reconcile_orders(self) -> list[Mapping[str, Any]]:
        if self.api is None:
            raise BrokerError("broker is not connected")
        self.api.update_status(self.account)
        results = []
        for trade in self.api.list_trades():
            results.append(
                {
                    "broker_order_id": str(trade.order.id),
                    "custom_field": str(getattr(trade.order, "custom_field", "")),
                    "status": str(trade.status.status).split(".")[-1],
                    "filled_quantity": int(getattr(trade.status, "deal_quantity", 0)),
                    "ticker": str(trade.contract.code),
                }
            )
        return results


class BrokerWorker:
    """Serialize every broker call onto one worker thread."""

    def __init__(self, adapter: BrokerAdapter):
        self.adapter = adapter
        self._queue: queue.Queue[tuple[str, tuple[Any, ...], dict[str, Any], Future[Any]] | None] = (
            queue.Queue()
        )
        self._thread = threading.Thread(target=self._run, name="broker-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            method, args, kwargs, future = item
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(getattr(self.adapter, method)(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)

    def call(self, method: str, *args: Any, timeout: float = 65, **kwargs: Any) -> Any:
        future: Future[Any] = Future()
        self._queue.put((method, args, kwargs, future))
        return future.result(timeout=timeout)

    def close(self) -> None:
        try:
            self.call("close", timeout=10)
        finally:
            self._queue.put(None)
            self._thread.join(timeout=10)
