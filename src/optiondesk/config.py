"""Configuration.

Settings live in a TOML file (see ``config/example.toml``). Secrets never do:
broker credentials and API keys are read from environment variables only, so
a config file can be committed or shared without leaking anything.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,9}(\.[A-Z])?$")
_SECRET_HINTS = ("secret", "password", "token", "api_key", "apikey", "client_id")


class ConfigError(ValueError):
    pass


@dataclass
class AccountConfig:
    starting_capital: float = 25_000.0


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: float = 2.0  # premium at risk per trade, % of equity
    max_open_positions: int = 3
    max_total_exposure_pct: float = 20.0  # all open premium, % of equity
    daily_loss_limit_pct: float = 5.0  # stop opening trades after this drawdown
    cooldown_minutes: float = 15.0  # per symbol, after any exit
    no_entries_first_minutes: float = 5.0
    no_entries_last_minutes: float = 30.0


@dataclass
class SizingConfig:
    method: Literal["fixed_fraction", "kelly"] = "fixed_fraction"
    kelly_fraction: float = 0.25  # fraction of full Kelly to use
    kelly_min_trades: int = 20  # trades a strategy needs before Kelly applies
    max_contracts: int = 10


@dataclass
class ContractConfig:
    target_delta: float = 0.40
    min_dte: int = 1
    max_dte: int = 14
    max_spread_pct: float = 0.10
    min_open_interest: int = 100


@dataclass
class ExitConfig:
    stop_loss_pct: float = 0.30
    take_profit_pct: float = 0.60
    trail_activate_pct: float = 0.25  # start trailing once up this much
    trail_pct: float = 0.15  # exit if the mark falls this far from its high
    max_hold_minutes: float = 180.0
    flatten_minutes_before_close: float = 15.0


@dataclass
class ExecutionConfig:
    commission_per_contract: float = 0.65
    entry_limit_offset: float = 0.25  # limit = mid + offset * half-spread
    fill_timeout_seconds: float = 30.0


@dataclass
class StrategyConfig:
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class SyntheticConfig:
    seed: int = 7
    start_price: float = 500.0
    annual_vol: float = 0.18
    implied_vol: float = 0.20
    speed: float = 0.0  # seconds to wait between bars in `run`; 0 = as fast as possible


@dataclass
class Config:
    mode: Literal["paper", "live"] = "paper"
    data: Literal["synthetic", "schwab"] = "synthetic"
    symbols: list[str] = field(default_factory=lambda: ["SPY"])
    bar_seconds: int = 60
    poll_seconds: float = 5.0
    journal_path: str = "data/optiondesk.db"
    plugins: list[str] = field(default_factory=list)  # modules to import, e.g. your strategies
    account: AccountConfig = field(default_factory=AccountConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    contracts: ContractConfig = field(default_factory=ContractConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategies: list[StrategyConfig] = field(default_factory=list)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    synthetic: SyntheticConfig = field(default_factory=SyntheticConfig)

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ConfigError(f"mode must be 'paper' or 'live', got {self.mode!r}")
        if self.data not in ("synthetic", "schwab"):
            raise ConfigError(f"data must be 'synthetic' or 'schwab', got {self.data!r}")
        if self.mode == "live" and self.data != "schwab":
            raise ConfigError("live mode needs real market data: set data = 'schwab'")
        if not self.symbols:
            raise ConfigError("symbols must list at least one ticker")
        for symbol in self.symbols:
            if not SYMBOL_RE.match(symbol):
                raise ConfigError(f"invalid symbol {symbol!r}")
        if self.account.starting_capital <= 0:
            raise ConfigError("account.starting_capital must be positive")
        if self.bar_seconds <= 0 or self.poll_seconds <= 0:
            raise ConfigError("bar_seconds and poll_seconds must be positive")
        if self.sizing.method not in ("fixed_fraction", "kelly"):
            raise ConfigError("sizing.method must be 'fixed_fraction' or 'kelly'")
        for name in ("max_risk_per_trade_pct", "max_total_exposure_pct", "daily_loss_limit_pct"):
            value = getattr(self.risk, name)
            if not 0 < value <= 100:
                raise ConfigError(f"risk.{name} must be between 0 and 100")
        if not 0 < self.contracts.target_delta < 1:
            raise ConfigError("contracts.target_delta must be between 0 and 1")
        if self.contracts.min_dte > self.contracts.max_dte:
            raise ConfigError("contracts.min_dte cannot exceed contracts.max_dte")
        if not any(s.enabled for s in self.strategies):
            raise ConfigError("enable at least one [[strategies]] entry")


@dataclass(frozen=True)
class Secrets:
    """Credentials, read from the environment only."""

    schwab_client_id: str | None
    schwab_client_secret: str | None
    schwab_redirect_uri: str
    schwab_token_path: Path
    dashboard_api_key: str | None
    live_trading_confirmed: bool

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, dotenv: str | Path = ".env") -> Secrets:
        """Read secrets from ``env`` (default: the process environment).

        When reading the process environment, a ``.env`` file in the working
        directory is loaded first; non-empty variables already set take precedence.
        """
        if env is None:
            env = {**load_dotenv(dotenv), **{k: v for k, v in os.environ.items() if v}}
        token_path = env.get("OPTIONDESK_TOKEN_PATH") or "~/.optiondesk/schwab_tokens.json"
        return cls(
            schwab_client_id=env.get("SCHWAB_CLIENT_ID") or None,
            schwab_client_secret=env.get("SCHWAB_CLIENT_SECRET") or None,
            schwab_redirect_uri=env.get("SCHWAB_REDIRECT_URI") or "https://127.0.0.1:8182",
            schwab_token_path=Path(token_path).expanduser(),
            dashboard_api_key=env.get("OPTIONDESK_API_KEY") or None,
            live_trading_confirmed=env.get("OPTIONDESK_LIVE_TRADING") == "yes-i-understand",
        )

    def __repr__(self) -> str:  # never print credentials
        return "Secrets(<redacted>)"


def load_dotenv(path: str | Path) -> dict[str, str]:
    """Parse simple ``KEY=VALUE`` lines. Blank lines and ``#`` comments are skipped."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_config(path: str | Path) -> Config:
    """Read and validate a TOML config file."""
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    _reject_secrets(raw)
    config: Config = _build(Config, raw, "")
    config.validate()
    return config


def _reject_secrets(raw: dict[str, Any], path: str = "") -> None:
    for key, value in raw.items():
        where = f"{path}{key}"
        if isinstance(value, dict):
            _reject_secrets(value, f"{where}.")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    _reject_secrets(item, f"{where}.{i}.")
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            raise ConfigError(
                f"{where!r} looks like a credential. Secrets belong in environment "
                "variables (see .env.example), never in the config file."
            )


def _build(cls: Any, raw: dict[str, Any], path: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        section = path.rstrip(".") or "top level"
        raise ConfigError(f"unknown setting(s) in [{section}]: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        default = known[name].default_factory
        sample = default() if callable(default) else None
        if name == "strategies":
            kwargs[name] = [_build(StrategyConfig, s, f"strategies.{i}") for i, s in enumerate(value)]
        elif is_dataclass(sample) and isinstance(value, dict):
            kwargs[name] = _build(type(sample), value, f"{path}{name}.")
        else:
            kwargs[name] = value
    return cls(**kwargs)
