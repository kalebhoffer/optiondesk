from __future__ import annotations

import json
import logging
import stat
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from optiondesk.broker.base import snap_to_tick
from optiondesk.broker.paper import PaperBroker
from optiondesk.broker.schwab import SchwabBroker
from optiondesk.data.bars import BarAggregator
from optiondesk.data.schwab import parse_chain
from optiondesk.models import Order, Right, Side
from optiondesk.schwab import SchwabAuthError, SchwabClient, Tokens, TokenStore, code_from_redirect

from .helpers import MONDAY, contract, et

NOON = et(MONDAY, 12)

# ── bars ──


def test_aggregator_builds_bars_and_volume_deltas() -> None:
    agg = BarAggregator(60)
    t = et(MONDAY, 10)
    assert agg.update("SPY", 100, 1_000, t) == []
    assert agg.update("SPY", 102, 1_500, t + timedelta(seconds=20)) == []
    assert agg.update("SPY", 99, 1_700, t + timedelta(seconds=40)) == []
    done = agg.update("SPY", 101, 2_000, t + timedelta(seconds=65))
    assert len(done) == 1
    b = done[0]
    assert (b.start, b.open, b.high, b.low, b.close, b.volume) == (t, 100, 102, 99, 99, 700)
    assert agg.flush()[0].close == 101


def test_aggregator_resets_volume_each_day_and_ignores_bad_prices() -> None:
    agg = BarAggregator(60)
    agg.update("SPY", 100, 5_000_000, et(MONDAY, 15, 59))
    agg.update(
        "SPY",
        0,
        0,
        et(
            MONDAY,
            15,
            59,
        ),
    )
    done = agg.update("SPY", 101, 1_000, et(MONDAY + timedelta(days=1), 9, 30))
    assert done[0].volume == 0
    assert agg.flush()[0].volume == 0  # first snapshot of a new day has no delta


# ── paper broker ──


@pytest.mark.parametrize(
    ("side", "limit", "expected"),
    [
        (Side.BUY_TO_OPEN, None, 2.10),
        (Side.SELL_TO_CLOSE, None, 2.00),
        (Side.BUY_TO_OPEN, 2.07, 2.07),
        (Side.BUY_TO_OPEN, 2.50, 2.10),  # never pay more than the ask
        (Side.BUY_TO_OPEN, 2.02, None),  # below mid: no fill
        (Side.SELL_TO_CLOSE, 2.03, 2.03),
        (Side.SELL_TO_CLOSE, 2.08, None),
    ],
)
async def test_paper_fills(side: Side, limit: float | None, expected: float | None) -> None:
    broker = PaperBroker(commission_per_contract=0.5)
    fill = await broker.submit(Order(contract(bid=2.00, ask=2.10), side, 3, limit), NOON)
    if expected is None:
        assert fill is None
    else:
        assert fill is not None and fill.price == pytest.approx(expected)
        assert fill.quantity == 3 and fill.commission == pytest.approx(1.5)


def test_tick_snapping() -> None:
    assert snap_to_tick(1.12, round_up=True) == 1.15
    assert snap_to_tick(1.12, round_up=False) == 1.10
    assert snap_to_tick(1.15, round_up=False) == 1.15
    assert snap_to_tick(4.04, round_up=True) == 4.10
    assert snap_to_tick(0.01, round_up=False) == 0.05


# ── Schwab: tokens and client ──


def tokens(expires_in: float = 1800) -> Tokens:
    now = time.time()
    return Tokens("access-1", "refresh-1", now + expires_in, now)


def test_token_file_is_private(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "nested" / "t.json")
    store.save(tokens())
    mode = stat.S_IMODE((tmp_path / "nested" / "t.json").stat().st_mode)
    assert mode == 0o600
    loaded = store.load()
    assert loaded is not None and loaded.access_token == "access-1"
    assert "access-1" not in repr(loaded)


def test_code_from_redirect() -> None:
    url = "https://127.0.0.1:8182/?code=C0.abc%40xyz&session=s1"
    assert code_from_redirect(url) == "C0.abc@xyz"
    with pytest.raises(SchwabAuthError):
        code_from_redirect("https://127.0.0.1:8182/?error=denied")


def make_client(tmp_path: Path, handler, saved: Tokens | None = None) -> SchwabClient:  # type: ignore[no-untyped-def]
    path = tmp_path / "t.json"
    if saved:
        TokenStore(path).save(saved)
    return SchwabClient("cid", "csecret", path, transport=httpx.MockTransport(handler))


def test_client_requires_credentials(tmp_path: Path) -> None:
    with pytest.raises(SchwabAuthError, match="SCHWAB_CLIENT_ID"):
        SchwabClient("", "", tmp_path / "t.json")


async def test_not_logged_in(tmp_path: Path) -> None:
    client = make_client(tmp_path, lambda r: httpx.Response(200))
    with pytest.raises(SchwabAuthError, match="schwab-login"):
        await client.get_json("/x")


async def test_refreshes_expiring_token_and_on_401(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []
    issued = iter(["access-2", "access-3"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth/token":
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            calls.append(f"token:{form['grant_type']}")
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": next(issued), "expires_in": 1800})
        calls.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer access-2":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    caplog.set_level(logging.DEBUG)
    client = make_client(tmp_path, handler, saved=tokens(expires_in=10))  # about to expire
    assert await client.get_json("/marketdata/v1/quotes") == {"ok": True}
    assert calls == ["token:refresh_token", "Bearer access-2", "token:refresh_token", "Bearer access-3"]
    saved = TokenStore(tmp_path / "t.json").load()
    assert saved is not None and saved.access_token == "access-3" and saved.refresh_token == "refresh-1"
    for secret in ("csecret", "access-2", "access-3", "refresh-1"):
        assert secret not in caplog.text


async def test_failed_refresh_never_echoes_body(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"error":"invalid_grant","refresh_token":"refresh-1"}')

    client = make_client(tmp_path, handler, saved=tokens(expires_in=0))
    with pytest.raises(SchwabAuthError) as info:
        await client.get_json("/x")
    assert "refresh-1" not in str(info.value)


async def test_expired_refresh_token(tmp_path: Path) -> None:
    old = Tokens("a", "r", time.time() - 10, time.time() - 8 * 86400)
    client = make_client(tmp_path, lambda r: httpx.Response(200), saved=old)
    with pytest.raises(SchwabAuthError, match="7-day"):
        await client.get_json("/x")


async def test_exchange_code_saves_tokens(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"grant_type=authorization_code" in request.content
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 1800})

    client = make_client(tmp_path, handler)
    assert "client_id=cid" in client.authorize_url("https://127.0.0.1:8182")
    await client.exchange_code("code", "https://127.0.0.1:8182")
    days = client.refresh_token_days_left()
    assert days is not None and 6.99 < days <= 7


# ── Schwab: chains ──


def test_parse_chain() -> None:
    data = {
        "callExpDateMap": {
            "2026-05-08:4": {
                "500.0": [
                    {
                        "symbol": "SPY   260508C00500000",
                        "bid": 2.0,
                        "ask": 2.1,
                        "delta": 0.45,
                        "volatility": 18.5,
                        "openInterest": 1200,
                        "totalVolume": 300,
                    }
                ],
                "505.0": [{"symbol": "SPY   260508C00505000", "bid": 0, "ask": 0.05, "delta": 0.1}],
                "510.0": [{"symbol": "SPY   260508C00510000", "bid": 1, "ask": 1.1, "delta": "NaN"}],
            }
        },
        "putExpDateMap": {
            "2026-05-08:4": {
                "495.0": [{"symbol": "SPY   260508P00495000", "bid": 1.5, "ask": 1.6, "delta": -0.35}]
            }
        },
    }
    chain = parse_chain("SPY", data)
    assert [c.symbol for c in chain] == ["SPY260508C00500000", "SPY260508P00495000"]
    call = chain[0]
    assert call.right is Right.CALL and call.strike == 500 and call.implied_vol == pytest.approx(0.185)
    assert call.open_interest == 1200
    assert parse_chain("SPY", "garbage") == []


# ── Schwab: broker ──


class FakeSchwab:
    def __init__(self, statuses: list[dict[str, object]]) -> None:
        self.statuses = statuses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/accountNumbers"):
            return httpx.Response(200, json=[{"accountNumber": "123", "hashValue": "HASH"}])
        if request.method == "POST" and path.endswith("/orders"):
            return httpx.Response(201, headers={"Location": f"{request.url}/9876"})
        if request.method == "DELETE":
            return httpx.Response(200)
        if "/orders/9876" in path:
            return httpx.Response(
                200, json=self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            )
        return httpx.Response(404)


async def make_broker(tmp_path: Path, fake: FakeSchwab, timeout: float = 5) -> SchwabBroker:
    broker = SchwabBroker(make_client(tmp_path, fake, saved=tokens()), fill_timeout=timeout, poll_interval=0)
    await broker.connect()
    return broker


def test_order_body() -> None:
    entry = SchwabBroker.order_body(Order(contract(), Side.BUY_TO_OPEN, 2, 2.07))
    assert entry["orderType"] == "LIMIT" and entry["price"] == "2.10"
    leg = entry["orderLegCollection"][0]
    assert leg["instruction"] == "BUY_TO_OPEN" and leg["quantity"] == 2
    assert leg["instrument"] == {"symbol": "SPY   260508C00500000", "assetType": "OPTION"}
    exit_ = SchwabBroker.order_body(Order(contract(), Side.SELL_TO_CLOSE, 2))
    assert exit_["orderType"] == "MARKET" and "price" not in exit_


async def test_schwab_fill(tmp_path: Path) -> None:
    filled = {
        "status": "FILLED",
        "orderActivityCollection": [
            {"executionLegs": [{"price": 2.05, "quantity": 1}, {"price": 2.10, "quantity": 1}]}
        ],
    }
    fake = FakeSchwab([{"status": "WORKING"}, filled])
    broker = await make_broker(tmp_path, fake)
    fill = await broker.submit(Order(contract(), Side.BUY_TO_OPEN, 2, 2.07), NOON)
    assert fill is not None and fill.quantity == 2 and fill.price == pytest.approx(2.075)
    assert fill.broker_order_id == "9876"
    post = next(r for r in fake.requests if r.method == "POST")
    assert post.url.path == "/trader/v1/accounts/HASH/orders"
    assert json.loads(post.content)["price"] == "2.10"
    assert not any(r.method == "DELETE" for r in fake.requests)


async def test_schwab_timeout_cancels_and_keeps_partial_fill(tmp_path: Path) -> None:
    partial = {
        "status": "CANCELED",
        "orderActivityCollection": [{"executionLegs": [{"price": 2.0, "quantity": 1}]}],
    }
    fake = FakeSchwab([{"status": "WORKING"}])
    broker = await make_broker(tmp_path, fake, timeout=0)
    fake.statuses = [{"status": "WORKING"}, partial]
    fill = await broker.submit(Order(contract(), Side.BUY_TO_OPEN, 3, 2.07), NOON)
    assert any(r.method == "DELETE" for r in fake.requests)
    assert fill is not None and fill.quantity == 1


async def test_schwab_rejection(tmp_path: Path) -> None:
    fake = FakeSchwab([{"status": "REJECTED"}])
    broker = await make_broker(tmp_path, fake)
    assert await broker.submit(Order(contract(), Side.BUY_TO_OPEN, 1, 2.07), NOON) is None


async def test_schwab_account_selection(tmp_path: Path) -> None:
    def two_accounts(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"accountNumber": "1", "hashValue": "A"}, {"accountNumber": "2", "hashValue": "B"}]
        )

    broker = SchwabBroker(make_client(tmp_path, two_accounts, saved=tokens()))
    with pytest.raises(Exception, match="SCHWAB_ACCOUNT_NUMBER"):
        await broker.connect()
    broker.account_number = "2"
    await broker.connect()
    assert broker.account_hash == "B"
