from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from optiondesk.api import create_app
from optiondesk.backtest import run_backtest
from optiondesk.broker.paper import PaperBroker
from optiondesk.bus import Event, EventBus
from optiondesk.cli import main
from optiondesk.data.simulated import CsvMarketData, OptionModel, SyntheticMarketData, write_sample_csv
from optiondesk.engine import Engine
from optiondesk.journal import Journal
from optiondesk.models import Bar, Direction, OptionContract, Signal
from optiondesk.strategies.base import Strategy

from .helpers import MONDAY, bar, config, et


class ScriptedData:
    """Bars from a list; options modeled like the simulator, with optional quote outages."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.model = OptionModel(implied_vol=0.2)
        self.quotes_down = False

    async def bars(self) -> AsyncIterator[Bar]:
        for b in self._bars:
            self.model.update(b)
            yield b

    async def option_chain(self, symbol: str, now: datetime) -> list[OptionContract]:
        return self.model.chain(symbol, now)

    async def refresh(self, contracts: Sequence[OptionContract], now: datetime) -> dict[str, OptionContract]:
        if self.quotes_down:
            raise RuntimeError("quotes unavailable")
        return self.model.refresh(contracts, now)

    async def close(self) -> None:
        return None


class SignalAt(Strategy):
    name = "scripted"

    def __init__(self, at: set[int], direction: Direction = Direction.LONG) -> None:
        self.params = {}
        self.at = at
        self.direction = direction
        self.seen = 0

    def on_bar(self, b: Bar) -> Signal | None:
        self.seen += 1
        if self.seen in self.at:
            return Signal(self.name, b.symbol, self.direction, b.start, b.close, "scripted")
        return None


class Boom(Strategy):
    name = "boom"

    def __init__(self) -> None:
        self.params = {}

    def on_bar(self, b: Bar) -> Signal | None:
        raise RuntimeError("bug in strategy")


def trending(n: int, start: float = 500.0, step: float = 0.0, minute: int = 0) -> list[Bar]:
    t0 = et(MONDAY, 10) + timedelta(minutes=minute)
    return [bar("SPY", t0 + timedelta(minutes=i), start + i * step) for i in range(n)]


def collect(bus: EventBus) -> list[Event]:
    events: list[Event] = []
    bus.subscribe("*", events.append)
    return events


async def run(
    bars: list[Bar], strategies: list[Strategy], **cfg: Any
) -> tuple[Engine, list[Event], ScriptedData]:
    data = ScriptedData(bars)
    bus = EventBus()
    events = collect(bus)
    engine = Engine(config(**cfg), data, PaperBroker(), bus=bus, strategies=strategies)
    await engine.run()
    return engine, events, data


async def test_signal_to_position_to_take_profit() -> None:
    engine, events, _ = await run(trending(60, step=0.4), [SignalAt({2})])
    topics = [e.topic for e in events]
    assert topics[0] == "engine.started" and topics[-1] == "engine.stopped"
    assert "position.opened" in topics
    closed = [e for e in events if e.topic == "trade.closed"]
    assert closed and closed[0].data["reason"] in ("take_profit", "trailing_stop")
    assert closed[0].data["pnl"] > 0
    assert engine.portfolio.cash > engine.portfolio.starting_capital
    opened = next(e for e in events if e.topic == "position.opened")
    assert "C" in opened.data["contract"][-9:]  # a call for a long signal


async def test_short_signal_buys_put_and_stops_out() -> None:
    _, events, _ = await run(trending(60, step=0.4), [SignalAt({2}, Direction.SHORT)])
    opened = next(e for e in events if e.topic == "position.opened")
    assert "P" in opened.data["contract"][-9:]
    closed = next(e for e in events if e.topic == "trade.closed")
    assert closed.data["reason"] == "stop_loss" and closed.data["pnl"] < 0


async def test_risk_rejection_is_published() -> None:
    _, events, _ = await run(trending(5, minute=-28), [SignalAt({1})])  # 09:32, too soon
    rejected = [e for e in events if e.topic == "signal.rejected"]
    assert rejected and "too soon" in rejected[0].data["reason"]


async def test_no_contract_when_rules_too_strict() -> None:
    from optiondesk.config import ContractConfig

    _, events, _ = await run(trending(5), [SignalAt({1})], contracts=ContractConfig(min_open_interest=10**9))
    assert any("no contract" in e.data.get("reason", "") for e in events)


async def test_strategy_errors_do_not_stop_trading() -> None:
    engine, events, _ = await run(trending(10, step=0.4), [Boom(), SignalAt({2})])
    assert any(e.topic == "position.opened" for e in events)
    assert engine.last_bar_time is not None


async def test_no_exit_on_stale_quotes() -> None:
    data = ScriptedData(trending(40, step=-0.5))
    engine = Engine(config(), data, PaperBroker(), strategies=[SignalAt({2})])
    gen = data.bars()
    for _ in range(3):
        await engine.on_bar(await gen.__anext__())
    assert len(engine.portfolio.positions) == 1
    data.quotes_down = True
    async for b in gen:
        await engine.on_bar(b)
    assert len(engine.portfolio.positions) == 1  # price fell hard, but no quotes -> no blind exit


async def test_flatten() -> None:
    data = ScriptedData(trending(5))
    engine = Engine(config(), data, PaperBroker(), strategies=[SignalAt({2})])
    await engine.run()
    assert engine.portfolio.positions
    await engine.flatten("manual")
    assert not engine.portfolio.positions
    assert engine.portfolio.closed[-1].exit_reason == "manual"


# ── backtest, journal, simulator ──


async def test_backtest_is_deterministic() -> None:
    cfg = config(symbols=["SPY", "QQQ"])
    one = await run_backtest(cfg, SyntheticMarketData(cfg.symbols, start=MONDAY, days=3, seed=3))
    two = await run_backtest(cfg, SyntheticMarketData(cfg.symbols, start=MONDAY, days=3, seed=3))
    assert one.total_pnl == two.total_pnl and len(one.trades) == len(two.trades)
    assert not any(t.exit_reason == "backtest_end" for t in one.trades)  # all flat by end of day
    text = one.render()
    assert "Backtest results" in text and "pipeline check" in text


async def test_csv_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "bars.csv"
    rows = write_sample_csv(path, ["SPY"], MONDAY, 1)
    assert rows == 390
    bars = [b async for b in CsvMarketData(path).bars()]
    assert len(bars) == 390 and bars[0].start == et(MONDAY, 9, 30)


def test_csv_bad_row(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    path = tmp_path / "bad.csv"
    path.write_text("timestamp,symbol,open,high,low,close,volume\nnot-a-date,SPY,1,1,1,1,1\n")

    async def read() -> None:
        [b async for b in CsvMarketData(path).bars()]

    with pytest.raises(ValueError, match=r"bad\.csv:2"):
        asyncio.run(read())


def test_journal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    journal = Journal(tmp_path / "j" / "x.db")
    journal.record(Event("signal", et(MONDAY, 10), {"symbol": "SPY"}))
    journal.record(Event("fill", et(MONDAY, 10, 1), {"price": 2.0}))
    assert [r["topic"] for r in journal.recent()] == ["fill", "signal"]
    assert journal.recent(topic="signal")[0]["data"] == {"symbol": "SPY"}


async def test_bus_isolates_failing_handlers() -> None:
    bus = EventBus()
    seen: list[str] = []

    def bad(e: Event) -> None:
        raise RuntimeError("x")

    async def good(e: Event) -> None:
        seen.append(e.topic)

    bus.subscribe("a", bad)
    bus.subscribe("*", good)
    await bus.publish(Event("a", et(MONDAY, 10)))
    assert seen == ["a"]


# ── dashboard ──


async def _engine_with_trade() -> Engine:
    data = ScriptedData(trending(30, step=0.4))
    engine = Engine(config(), data, PaperBroker(), strategies=[SignalAt({2})])
    await engine.run()
    return engine


async def test_dashboard_without_key() -> None:
    engine = await _engine_with_trade()
    client = TestClient(create_app(engine))
    status = client.get("/api/status").json()
    assert status["mode"] == "paper" and status["equity"] > 0
    assert isinstance(client.get("/api/positions").json(), list)
    assert client.get("/api/trades").json()
    assert "OptionDesk" in client.get("/").text
    assert client.get("/health").json()["ok"]


async def test_dashboard_key_and_controls() -> None:
    engine = await _engine_with_trade()
    client = TestClient(create_app(engine, api_key="s3cret"))
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/status", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/api/status?key=s3cret").status_code == 200
    assert client.get("/health").status_code == 200  # health stays open for monitors

    # Controls need the key and the anti-CSRF header.
    assert client.post("/api/halt?key=s3cret").status_code == 403
    ok = client.post("/api/halt?key=s3cret", headers={"X-OptionDesk": "1"})
    assert ok.status_code == 200 and engine.risk.halted_reason
    client.post("/api/resume?key=s3cret", headers={"X-OptionDesk": "1"})
    assert engine.risk.halted_reason is None


# ── CLI ──


def test_cli_demo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo", "--days", "2"]) == 0
    assert "Backtest results" in capsys.readouterr().out


def test_cli_refuses_live_without_confirmation(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OPTIONDESK_LIVE_TRADING", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "live.toml"
    cfg.write_text('mode = "live"\ndata = "schwab"\n[[strategies]]\nname = "ema_cross"\n')
    assert main(["run", "--config", str(cfg)]) == 2
    assert "real money" in capsys.readouterr().err


def test_cli_config_error(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = tmp_path / "bad.toml"
    cfg.write_text('api_key = "x"\n')
    assert main(["backtest", "--config", str(cfg)]) == 2
    assert "credential" in capsys.readouterr().err


def test_plugins_register_custom_strategies(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "my_plugin_strats.py").write_text(
        "from optiondesk.strategies import Strategy, register\n"
        "@register('plugin_noop')\n"
        "class Noop(Strategy):\n"
        "    def on_bar(self, bar):\n"
        "        return None\n"
    )
    cfg = tmp_path / "p.toml"
    cfg.write_text('plugins = ["my_plugin_strats"]\n[[strategies]]\nname = "plugin_noop"\n')
    monkeypatch.chdir(tmp_path)
    assert main(["backtest", "--config", str(cfg), "--days", "1"]) == 0
    assert "Trades                         0" in capsys.readouterr().out


def test_missing_plugin_is_a_config_error(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    cfg = tmp_path / "p.toml"
    cfg.write_text('plugins = ["does_not_exist_xyz"]\n[[strategies]]\nname = "ema_cross"\n')
    assert main(["backtest", "--config", str(cfg), "--days", "1"]) == 2
    assert "could not import plugin" in capsys.readouterr().err
