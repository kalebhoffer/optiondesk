"""Run the live engine over historical or synthetic bars and summarize the results."""

from __future__ import annotations

from dataclasses import dataclass, field

from .broker.paper import PaperBroker
from .bus import EventBus
from .config import Config
from .data.base import MarketData
from .engine import Engine
from .models import ClosedTrade


@dataclass
class Report:
    starting_capital: float
    ending_equity: float
    trades: list[ClosedTrade]
    max_drawdown_pct: float
    rejections: dict[str, int] = field(default_factory=dict)

    @property
    def total_pnl(self) -> float:
        return self.ending_equity - self.starting_capital

    @property
    def return_pct(self) -> float:
        return self.total_pnl / self.starting_capital * 100

    @property
    def win_rate(self) -> float:
        return sum(t.pnl > 0 for t in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        wins = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl <= 0)
        return wins / losses if losses else float("inf") if wins else 0.0

    def by_strategy(self) -> dict[str, tuple[int, float]]:
        out: dict[str, tuple[int, float]] = {}
        for t in self.trades:
            n, pnl = out.get(t.strategy, (0, 0.0))
            out[t.strategy] = (n + 1, pnl + t.pnl)
        return out

    def by_exit(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.trades:
            out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
        return out

    def render(self) -> str:
        lines = [
            "Backtest results",
            "================",
            f"Starting capital   ${self.starting_capital:>12,.2f}",
            f"Ending equity      ${self.ending_equity:>12,.2f}",
            f"Total P&L          ${self.total_pnl:>12,.2f}  ({self.return_pct:+.2f}%)",
            f"Max drawdown        {self.max_drawdown_pct:>12.2f}%",
            f"Trades              {len(self.trades):>12}",
            f"Win rate            {self.win_rate:>12.1%}",
            f"Profit factor       {self.profit_factor:>12.2f}",
            "",
            "By strategy:",
        ]
        for name, (n, pnl) in sorted(self.by_strategy().items()):
            lines.append(f"  {name:<18} {n:>4} trades  ${pnl:>10,.2f}")
        lines.append("")
        lines.append("Exit reasons:")
        for reason, n in sorted(self.by_exit().items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason:<18} {n:>4}")
        if self.rejections:
            lines.append("")
            lines.append("Signals not taken:")
            for reason, n in sorted(self.rejections.items(), key=lambda kv: -kv[1])[:8]:
                lines.append(f"  {n:>4}  {reason}")
        lines.append("")
        lines.append("Option prices are modeled with Black-Scholes at a fixed implied volatility;")
        lines.append("treat these numbers as a pipeline check, not evidence of an edge.")
        return "\n".join(lines)


def _max_drawdown_pct(curve: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - 1)
    return -worst * 100


async def run_backtest(config: Config, data: MarketData, bus: EventBus | None = None) -> Report:
    bus = bus or EventBus()
    rejections: dict[str, int] = {}

    def count(event: object) -> None:
        reason = str(getattr(event, "data", {}).get("reason", "unknown"))
        rejections[reason] = rejections.get(reason, 0) + 1

    bus.subscribe("signal.rejected", count)
    engine = Engine(config, data, PaperBroker(config.execution.commission_per_contract), bus=bus)
    await engine.run()
    await engine.flatten("backtest_end")
    return Report(
        starting_capital=config.account.starting_capital,
        ending_equity=engine.portfolio.equity,
        trades=list(engine.portfolio.closed),
        max_drawdown_pct=_max_drawdown_pct([e for _, e in engine.equity_curve]),
        rejections=rejections,
    )
