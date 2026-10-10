"""Domain models kept independent from Shioaji and the web framework."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


OrderSide = Literal["Buy", "Sell"]
OrderLot = Literal["Common", "IntradayOdd"]


@dataclass(frozen=True)
class Quote:
    ticker: str
    bid: float
    ask: float
    last: float
    limit_up: float
    limit_down: float


@dataclass(frozen=True)
class BrokerPosition:
    ticker: str
    shares: int


@dataclass(frozen=True)
class ExecutionPolicy:
    lot_mode: Literal["common", "odd"] = "common"
    limit_offset_bps: int = 0
    cash_reserve_twd: float = 0.0
    max_order_twd: float = 500_000.0
    max_daily_twd: float = 2_000_000.0
    max_position_twd: float = 1_000_000.0
    max_turnover: float = 2.0
    allow_sell_proceeds_for_buys: bool = False
    cash_check: bool = False
    order_ttl_seconds: int = 120


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    ticker: str
    action: OrderSide
    shares: int
    broker_quantity: int
    order_lot: OrderLot
    limit_price: float
    estimated_notional: float


@dataclass(frozen=True)
class OrderPlan:
    bundle_id: str
    revision: int
    mode: Literal["dry_run", "simulation", "production"]
    data_as_of: str
    targets: dict[str, int]
    sleeve_targets: dict[str, dict[str, int]]
    managed_before: dict[str, int]
    external_positions: dict[str, int]
    orders: tuple[OrderIntent, ...]
    estimated_buy_twd: float
    estimated_sell_twd: float
    total_budget_twd: float
    cash_reserve_twd: float
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["orders"] = [asdict(order) for order in self.orders]
        value["warnings"] = list(self.warnings)
        return value

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
