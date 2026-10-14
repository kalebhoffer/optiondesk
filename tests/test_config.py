from __future__ import annotations

from pathlib import Path

import pytest

from optiondesk.config import ConfigError, Secrets, load_config, load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.toml"
    path.write_text(text)
    return path


MINIMAL = '[[strategies]]\nname = "ema_cross"\n'


def test_example_config_loads() -> None:
    cfg = load_config(ROOT / "config" / "example.toml")
    assert cfg.mode == "paper"
    assert [s.name for s in cfg.strategies] == ["ema_cross", "opening_range"]
    assert cfg.risk.max_open_positions == 3
    assert cfg.strategies[0].params == {"fast": 9, "slow": 21}


def test_defaults_fill_missing_sections(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.risk.daily_loss_limit_pct == 5.0
    assert cfg.symbols == ["SPY"]


@pytest.mark.parametrize(
    "text",
    [
        'schwab_client_secret = "abc"\n' + MINIMAL,
        '[account]\napi_key = "abc"\n' + MINIMAL,
        '[dashboard]\npassword = "x"\n' + MINIMAL,
        '[[strategies]]\nname = "ema_cross"\nparams = { api_key = "x" }\n',
    ],
)
def test_secrets_in_config_are_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="credential"):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('colour = "red"\n' + MINIMAL, "unknown setting"),
        ("[risk]\nmax_open = 3\n" + MINIMAL, "unknown setting"),
        ('mode = "yolo"\n' + MINIMAL, "mode"),
        ('mode = "live"\n' + MINIMAL, "real market data"),
        ('symbols = ["spy"]\n' + MINIMAL, "invalid symbol"),
        ("symbols = []\n" + MINIMAL, "at least one"),
        ("[risk]\ndaily_loss_limit_pct = 0\n" + MINIMAL, "between 0 and 100"),
        ("[contracts]\nmin_dte = 10\nmax_dte = 5\n" + MINIMAL, "min_dte"),
        ("", "at least one"),
    ],
)
def test_invalid_configs(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_dot_tickers_are_valid(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, 'symbols = ["BRK.B", "SPY"]\n' + MINIMAL))
    assert cfg.symbols == ["BRK.B", "SPY"]


def test_secrets_from_env() -> None:
    s = Secrets.from_env(
        {
            "SCHWAB_CLIENT_ID": "id",
            "SCHWAB_CLIENT_SECRET": "shh",
            "OPTIONDESK_API_KEY": "k",
            "OPTIONDESK_TOKEN_PATH": "/tmp/t.json",
        }
    )
    assert s.schwab_client_secret == "shh"
    assert str(s.schwab_token_path) == "/tmp/t.json"
    assert not s.live_trading_confirmed
    assert "shh" not in repr(s) and "redacted" in repr(s)


@pytest.mark.parametrize(
    ("value", "confirmed"), [("yes-i-understand", True), ("yes", False), ("1", False), ("", False)]
)
def test_live_trading_needs_exact_phrase(value: str, confirmed: bool) -> None:
    assert Secrets.from_env({"OPTIONDESK_LIVE_TRADING": value}).live_trading_confirmed is confirmed


def test_dotenv_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text('# comment\nSCHWAB_CLIENT_ID="from-file"\nexport OPTIONDESK_API_KEY=k1\n\nBROKEN LINE\n')
    assert load_dotenv(env) == {"SCHWAB_CLIENT_ID": "from-file", "OPTIONDESK_API_KEY": "k1"}
    monkeypatch.setenv("OPTIONDESK_API_KEY", "from-env")
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    s = Secrets.from_env(dotenv=env)
    assert s.schwab_client_id == "from-file"
    assert s.dashboard_api_key == "from-env"  # real environment wins


def test_missing_dotenv_is_fine(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "nope") == {}
