"""Command-line entry point: ``optiondesk <command>``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from . import __version__
from .backtest import run_backtest
from .broker.base import Broker
from .broker.paper import PaperBroker
from .bus import Event, EventBus
from .config import Config, ConfigError, Secrets, StrategyConfig, load_config
from .data.base import MarketData
from .data.simulated import CsvMarketData, SyntheticMarketData, write_sample_csv
from .engine import Engine
from .journal import Journal

log = logging.getLogger("optiondesk")


def _demo_config() -> Config:
    config = Config(
        symbols=["SPY", "QQQ"],
        strategies=[StrategyConfig("ema_cross"), StrategyConfig("opening_range")],
    )
    config.validate()
    return config


def _last_weekday_before(day: date, count: int) -> date:
    start = day
    found = 0
    while found < count:
        start -= timedelta(days=1)
        if start.weekday() < 5:
            found += 1
    return start


def _print_event(event: Event) -> None:
    if event.topic in ("position.opened", "trade.closed", "order.error", "exit.unfilled"):
        d = event.data
        if event.topic == "position.opened":
            text = f"OPEN  {d['quantity']} x {d['contract']} @ {d['price']:.2f}  [{d['strategy']}]"
        elif event.topic == "trade.closed":
            text = (
                f"CLOSE {d['quantity']} x {d['contract']} @ {d['exit_price']:.2f}"
                f"  P&L {d['pnl']:+.2f}  ({d['reason']})"
            )
        else:
            text = f"{event.topic}: {d}"
        log.info("%s  %s", event.time.strftime("%Y-%m-%d %H:%M"), text)


# ── commands ──


def cmd_demo(args: argparse.Namespace) -> int:
    config = _demo_config()
    start = _last_weekday_before(date.today(), args.days)
    data = SyntheticMarketData(config.symbols, start=start, days=args.days, seed=args.seed)
    bus = EventBus()
    if args.verbose:
        bus.subscribe("*", _print_event)
    report = asyncio.run(run_backtest(config, data, bus))
    print(report.render())
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data: MarketData
    if args.csv:
        data = CsvMarketData(args.csv, implied_vol=config.synthetic.implied_vol)
    else:
        s = config.synthetic
        start = (
            date.fromisoformat(args.start) if args.start else _last_weekday_before(date.today(), args.days)
        )
        data = SyntheticMarketData(
            config.symbols,
            start=start,
            days=args.days,
            start_price=s.start_price,
            annual_vol=s.annual_vol,
            implied_vol=s.implied_vol,
            seed=s.seed,
        )
    bus = EventBus()
    if args.verbose:
        bus.subscribe("*", _print_event)
    report = asyncio.run(run_backtest(config, data, bus))
    print(report.render())
    return 0


def cmd_sample_data(args: argparse.Namespace) -> int:
    start = date.fromisoformat(args.start) if args.start else _last_weekday_before(date.today(), args.days)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    rows = write_sample_csv(args.output, symbols, start, args.days, seed=args.seed)
    print(f"wrote {rows} bars to {args.output}")
    return 0


def cmd_schwab_login(args: argparse.Namespace) -> int:
    from .schwab import SchwabClient, code_from_redirect

    secrets = Secrets.from_env()
    client = SchwabClient(
        secrets.schwab_client_id or "", secrets.schwab_client_secret or "", secrets.schwab_token_path
    )
    print("1. Open this URL, log in to Schwab and approve access:\n")
    print("   " + client.authorize_url(secrets.schwab_redirect_uri) + "\n")
    print("2. Your browser will be sent to your redirect URI, which may show an error page.")
    print("   That's expected. Copy the full address from the address bar and paste it here.\n")
    redirected = input("Redirected URL: ")

    async def finish() -> None:
        try:
            await client.exchange_code(code_from_redirect(redirected), secrets.schwab_redirect_uri)
        finally:
            await client.close()

    asyncio.run(finish())
    print(f"\nLogged in. Tokens saved to {secrets.schwab_token_path} (readable only by you).")
    print("Schwab requires logging in again every 7 days.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.host:
        config.dashboard.host = args.host
    if args.port:
        config.dashboard.port = args.port
    secrets = Secrets.from_env()
    if config.mode == "live" and not secrets.live_trading_confirmed:
        print(
            "Refusing to start: mode = 'live' places real orders with real money.\n"
            "If you have tested in paper mode and accept the risk, set\n"
            "  OPTIONDESK_LIVE_TRADING=yes-i-understand",
            file=sys.stderr,
        )
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(config, secrets))
    return 0


async def _run(config: Config, secrets: Secrets) -> None:
    data: MarketData
    broker: Broker
    client = None
    if config.data == "schwab" or config.mode == "live":
        from .data.schwab import SchwabMarketData
        from .schwab import SchwabClient

        client = SchwabClient(
            secrets.schwab_client_id or "", secrets.schwab_client_secret or "", secrets.schwab_token_path
        )
        days_left = client.refresh_token_days_left()
        if days_left is None:
            raise SystemExit("Not logged in to Schwab. Run `optiondesk schwab-login` first.")
        log.info("Schwab login valid for %.1f more days", days_left)
        data = SchwabMarketData(
            client,
            config.symbols,
            config.contracts,
            bar_seconds=config.bar_seconds,
            poll_seconds=config.poll_seconds,
        )
    else:
        s = config.synthetic
        data = SyntheticMarketData(
            config.symbols,
            start=_last_weekday_before(date.today(), 5),
            days=5,
            start_price=s.start_price,
            annual_vol=s.annual_vol,
            implied_vol=s.implied_vol,
            seed=s.seed,
            speed=s.speed,
        )

    if config.mode == "live":
        from .broker.schwab import SchwabBroker

        assert client is not None
        live = SchwabBroker(
            client,
            account_number=_env("SCHWAB_ACCOUNT_NUMBER"),
            commission_per_contract=config.execution.commission_per_contract,
            fill_timeout=config.execution.fill_timeout_seconds,
        )
        await live.connect()
        existing = await live.option_positions()
        if existing:
            log.warning(
                "The account already holds %d option position(s). OptionDesk does not manage "
                "positions it did not open in this session.",
                len(existing),
            )
        broker = live
        log.warning("LIVE TRADING: orders will be sent to Schwab.")
    else:
        broker = PaperBroker(config.execution.commission_per_contract)

    bus = EventBus()
    journal = Journal(config.journal_path)
    bus.subscribe("*", journal.record)
    bus.subscribe("*", _print_event)
    engine = Engine(config, data, broker, bus=bus)

    tasks = [asyncio.create_task(engine.run(), name="engine")]
    if config.dashboard.enabled:
        import uvicorn

        from .api import create_app

        app = create_app(engine, api_key=secrets.dashboard_api_key, journal=journal)
        server = uvicorn.Server(
            uvicorn.Config(app, host=config.dashboard.host, port=config.dashboard.port, log_level="warning")
        )
        tasks.append(asyncio.create_task(server.serve(), name="dashboard"))
        log.info("Dashboard: http://%s:%d", config.dashboard.host, config.dashboard.port)
    try:
        engine_task = tasks[0]
        await engine_task
        if config.data == "synthetic":
            log.info("Synthetic data finished; dashboard stays up until Ctrl+C.")
        if len(tasks) > 1:
            await tasks[1]
    finally:
        for task in tasks:
            task.cancel()
        if engine.portfolio.positions:
            log.warning(
                "Stopping with %d open position(s); they are NOT closed automatically.",
                len(engine.portfolio.positions),
            )
        await broker.close()
        await data.close()
        journal.close()


def _env(name: str) -> str | None:
    import os

    return os.environ.get(name) or None


# ── parser ──


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optiondesk", description="Intraday options trading platform.")
    parser.add_argument("--version", action="version", version=f"optiondesk {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="backtest the example strategies on synthetic data (no setup needed)")
    demo.add_argument("--days", type=int, default=10)
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("-v", "--verbose", action="store_true", help="print every trade")
    demo.set_defaults(func=cmd_demo)

    bt = sub.add_parser("backtest", help="backtest a config on a CSV of bars or synthetic data")
    bt.add_argument("--config", required=True, type=Path)
    bt.add_argument("--csv", type=Path, help="bars CSV (timestamp,symbol,open,high,low,close,volume)")
    bt.add_argument("--days", type=int, default=10, help="synthetic days when no --csv is given")
    bt.add_argument("--start", help="first synthetic day, YYYY-MM-DD")
    bt.add_argument("-v", "--verbose", action="store_true")
    bt.set_defaults(func=cmd_backtest)

    run = sub.add_parser("run", help="run the engine (paper by default) with the dashboard")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--host", help="dashboard bind address (overrides the config)")
    run.add_argument("--port", type=int, help="dashboard port (overrides the config)")
    run.set_defaults(func=cmd_run)

    login = sub.add_parser("schwab-login", help="authorize OptionDesk with your Schwab account")
    login.set_defaults(func=cmd_schwab_login)

    sample = sub.add_parser("sample-data", help="write synthetic bars to a CSV")
    sample.add_argument("output", type=Path)
    sample.add_argument("--symbols", default="SPY")
    sample.add_argument("--days", type=int, default=10)
    sample.add_argument("--start")
    sample.add_argument("--seed", type=int, default=7)
    sample.set_defaults(func=cmd_sample_data)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, ValueError) as exc:  # bad settings, strategy params or plugins
        print(f"config error: {exc}", file=sys.stderr)
        return 2
