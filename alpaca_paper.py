#!/usr/bin/env python3
"""Paper-only Alpaca execution connector for the Quant Momentum Bot.

The connector deliberately constructs TradingClient(..., paper=True). It never
supports live order routing. Default command 'plan' previews decisions and sends
no orders. Actual paper orders require the explicit '--submit' switch.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
except ImportError:
    DataFeed = StockHistoricalDataClient = StockLatestTradeRequest = None
    TradingClient = OrderSide = QueryOrderStatus = TimeInForce = None
    GetOrdersRequest = MarketOrderRequest = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "bot_data"
SCAN_FILE = DATA_DIR / "latest_scan.csv"
SCAN_META_FILE = DATA_DIR / "latest_scan_meta.json"
LOCAL_STATE_FILE = DATA_DIR / "alpaca_managed_state.json"
PLAN_FILE = DATA_DIR / "alpaca_last_plan.csv"

# Conservative beginner-friendly limits. These are paper-strategy settings, not
# a claim that these values will make the strategy profitable.
MAX_POSITIONS = 5
RISK_PER_TRADE = 0.005             # 0.5% of account equity risk budget per position
MAX_POSITION_WEIGHT = 0.10         # no more than 10% of equity per position
MAX_ORDER_NOTIONAL_USD = 2_000.00  # additional absolute cap per new entry
STOP_ATR_MULTIPLE = 2.5
TRAIL_ATR_MULTIPLE = 3.0
CASH_RESERVE_FRACTION = 0.02


def require_sdk() -> None:
    if TradingClient is None or StockHistoricalDataClient is None:
        raise SystemExit(
            "Missing Alpaca dependencies. Activate your project's virtual environment, then run:\n"
            "  python -m pip install -r requirements.txt"
        )


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def alpaca_symbol(symbol: str) -> str:
    """Convert common Yahoo dotted/dashed class-share notation for API matching."""
    return str(symbol).strip().upper().replace("-", ".")


def yahoo_symbol(symbol: str) -> str:
    return str(symbol).strip().upper().replace(".", "-")


def valid_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,7}", str(symbol or "").strip().upper()))


def load_environment() -> None:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")


def make_clients() -> tuple[Any, Any]:
    """Create clients with paper trading hard-coded; live endpoint is rejected."""
    require_sdk()
    load_environment()
    base_url = os.getenv("APCA_API_BASE_URL", "").strip().rstrip("/")
    if base_url and base_url != "https://paper-api.alpaca.markets":
        raise SystemExit(
            "Safety stop: APCA_API_BASE_URL must be unset or exactly "
            "https://paper-api.alpaca.markets. This project is paper-only."
        )
    key = os.getenv("APCA_API_KEY_ID", "").strip()
    secret = os.getenv("APCA_API_SECRET_KEY", "").strip()
    if not key or not secret:
        raise SystemExit(
            "Alpaca API keys are missing. Copy .env.example to .env and enter the PAPER-TRADING keys only."
        )
    trading = TradingClient(key, secret, paper=True)
    market_data = StockHistoricalDataClient(key, secret)
    return trading, market_data


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def load_scan() -> tuple[pd.DataFrame, dict[str, Any]]:
    if not SCAN_FILE.exists():
        raise SystemExit(f"No scan file found at {SCAN_FILE}. Run 'python alpaca_paper.py plan' first.")
    if not SCAN_META_FILE.exists():
        raise SystemExit(f"No scan metadata found at {SCAN_META_FILE}. Update the scanner, then run again.")
    df = pd.read_csv(SCAN_FILE)
    if "symbol" not in df.columns:
        raise SystemExit("Scan file is missing its symbol column. Run the scanner again.")
    df["symbol_alpaca"] = df["symbol"].map(alpaca_symbol)
    for field in ("eligible", "breakout_watch"):
        if field in df.columns:
            if df[field].dtype != bool:
                df[field] = df[field].astype(str).str.strip().str.lower().isin(["true", "1", "yes"])
        else:
            df[field] = False
    with SCAN_META_FILE.open("r", encoding="utf-8") as f:
        meta = json.load(f)
    return df, meta


def local_state() -> dict[str, Any]:
    ensure_data_dir()
    if not LOCAL_STATE_FILE.exists():
        return {"managed_positions": {}, "updated_utc": None}
    try:
        with LOCAL_STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("managed_positions", {})
        return state
    except (OSError, json.JSONDecodeError):
        raise SystemExit(f"Cannot read {LOCAL_STATE_FILE}. Back it up before editing or removing it.")


def save_local_state(state: dict[str, Any]) -> None:
    ensure_data_dir()
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    tmp = LOCAL_STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(LOCAL_STATE_FILE)


def print_account(trading: Any) -> None:
    account = trading.get_account()
    print("ALPACA PAPER ACCOUNT")
    print(f"Account status: {getattr(account, 'status', 'unknown')}")
    print(f"Equity:         ${fnum(getattr(account, 'equity', 0)):,.2f}")
    print(f"Cash:           ${fnum(getattr(account, 'cash', 0)):,.2f}")
    print(f"Buying power:   ${fnum(getattr(account, 'buying_power', 0)):,.2f} (bot sizes using cash, not margin)")
    print(f"Portfolio value:${fnum(getattr(account, 'portfolio_value', 0)):,.2f}")
    print(f"Day-trading mode: {getattr(account, 'pattern_day_trader', 'n/a')}")


def show_status(trading: Any) -> None:
    print_account(trading)
    positions = trading.get_all_positions()
    print("\nOPEN POSITIONS")
    if not positions:
        print("No positions currently open.")
    else:
        print(f"{'SYMBOL':<9} {'QTY':>9} {'AVG ENTRY':>12} {'LAST/MARK':>12} {'MKT VALUE':>13} {'UNREAL. P/L':>13}")
        for p in sorted(positions, key=lambda x: x.symbol):
            print(
                f"{p.symbol:<9} {fnum(p.qty):>9.3f} ${fnum(p.avg_entry_price):>10,.2f} "
                f"${fnum(p.current_price):>10,.2f} ${fnum(p.market_value):>11,.2f} "
                f"${fnum(p.unrealized_pl):>+11,.2f}"
            )
    orders = trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    print("\nOPEN ORDERS")
    if not orders:
        print("No open orders.")
    else:
        for o in orders:
            print(f"{o.symbol:<8} {str(o.side).split('.')[-1].upper():<5} qty={o.qty} status={o.status} id={o.id}")
    print("\nThe Alpaca web dashboard also shows your paper account and simulated order history.")


def show_prices(market_data: Any, symbols: list[str]) -> None:
    clean = list(dict.fromkeys(alpaca_symbol(s) for s in symbols if valid_symbol(alpaca_symbol(s))))
    if not clean:
        raise SystemExit("Provide one or more stock symbols, e.g. python alpaca_paper.py prices AAPL NVDA SPY")
    request = StockLatestTradeRequest(symbol_or_symbols=clean, feed=DataFeed.IEX)
    trades = market_data.get_stock_latest_trade(request)
    print("LATEST AVAILABLE IEX TRADES — timestamps are shown in London time")
    print(f"{'SYMBOL':<10} {'PRICE':>12} {'TRADE TIME (UK)':>25}")
    for symbol in clean:
        trade = trades.get(symbol)
        if trade is None:
            print(f"{symbol:<10} {'unavailable':>12} {'-':>25}")
            continue
        ts = getattr(trade, "timestamp", None)
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_text = ts.astimezone(ZoneInfo("Europe/London")).strftime("%Y-%m-%d %H:%M:%S")
        else:
            ts_text = "unknown"
        print(f"{symbol:<10} ${fnum(getattr(trade, 'price', 0)):>11,.2f} {ts_text:>25}")
    print("Note: the free paper-only entitlement uses IEX data, not a consolidated all-exchange feed.")


def refresh_scan(trading: Any) -> None:
    positions = trading.get_all_positions()
    extras = []
    for p in positions:
        sym = yahoo_symbol(p.symbol)
        if valid_symbol(sym) and sym not in extras:
            extras.append(sym)
    command = [sys.executable, str(ROOT / "quant_momentum_bot.py"), "scan"]
    if extras:
        command += ["--include", *extras]
    print("Refreshing the momentum scanner and saving its daily candidate table...")
    subprocess.run(command, cwd=ROOT, check=True)


def order_rows(trading: Any) -> list[Any]:
    return list(trading.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)))


def make_plan(trading: Any, dry_run: bool = True) -> list[dict[str, Any]]:
    refresh_scan(trading)
    scan, meta = load_scan()
    session_date = str(meta.get("session_date", datetime.now().date().isoformat()))
    bull = bool(meta.get("market_regime_bullish", False))
    scan_by_symbol = {str(r["symbol_alpaca"]): r for _, r in scan.iterrows()}
    account = trading.get_account()
    positions = list(trading.get_all_positions())
    pos_by_symbol = {alpaca_symbol(p.symbol): p for p in positions}
    open_orders = order_rows(trading)
    symbols_with_open_orders = {alpaca_symbol(o.symbol) for o in open_orders}
    pending_buys = sum(1 for o in open_orders if str(o.side).lower().endswith("buy"))
    managed = local_state()
    managed_positions: dict[str, Any] = managed.setdefault("managed_positions", {})
    actions: list[dict[str, Any]] = []

    print("\nALPACA PAPER TRADING PLAN")
    print(f"Scanner session: {session_date} | SPY regime: {'RISK-ON' if bull else 'RISK-OFF'}")
    print("Orders are only submitted if you rerun with the explicit --submit switch.")

    # Plan exits first. Existing open orders prevent duplicate orders.
    for norm_symbol, position in sorted(pos_by_symbol.items()):
        row = scan_by_symbol.get(norm_symbol)
        actual_symbol = position.symbol
        if row is None:
            print(f"  HOLD {actual_symbol}: no scan row available; not auto-selling an unmanaged/unknown symbol.")
            continue
        if norm_symbol in symbols_with_open_orders:
            print(f"  SKIP {actual_symbol}: an order for this symbol is already open.")
            continue
        price = fnum(row.get("price"))
        atr = fnum(row.get("atr14"))
        sma20 = fnum(row.get("sma20"))
        avg_entry = fnum(getattr(position, "avg_entry_price", 0))
        if price <= 0 or atr <= 0 or sma20 <= 0 or avg_entry <= 0:
            print(f"  HOLD {actual_symbol}: incomplete price/ATR data; no automatic action.")
            continue
        m = managed_positions.setdefault(actual_symbol, {})
        if fnum(m.get("entry_price")) <= 0:
            m["entry_price"] = avg_entry
        if fnum(m.get("initial_atr")) <= 0:
            m["initial_atr"] = atr
        if fnum(m.get("initial_stop")) <= 0:
            m["initial_stop"] = avg_entry - STOP_ATR_MULTIPLE * fnum(m.get("initial_atr"), atr)
        m["high_water"] = max(fnum(m.get("high_water"), price), price)
        m["last_scan_price"] = price
        m["last_session_date"] = session_date
        initial_stop = fnum(m.get("initial_stop"), avg_entry - STOP_ATR_MULTIPLE * atr)
        trailing_stop = fnum(m.get("high_water"), price) - TRAIL_ATR_MULTIPLE * atr
        stop_level = max(initial_stop, trailing_stop)
        reason = None
        if price <= stop_level:
            reason = f"close ${price:.2f} at/below ATR stop ${stop_level:.2f}"
        elif price < sma20:
            reason = f"close ${price:.2f} below 20-day average ${sma20:.2f}"
        if reason:
            actions.append({"action": "SELL", "symbol": actual_symbol, "qty": abs(fnum(position.qty)), "price": price, "reason": reason, "score": fnum(row.get("score"))})
            print(f"  SELL {actual_symbol}: {abs(fnum(position.qty)):g} shares — {reason}")
        else:
            print(f"  HOLD {actual_symbol}: close ${price:.2f}, protective threshold ${stop_level:.2f}")

    # Remove stale metadata for positions already closed on Alpaca.
    actual_positions = set(pos_by_symbol)
    for saved_symbol in list(managed_positions):
        if alpaca_symbol(saved_symbol) not in actual_positions:
            del managed_positions[saved_symbol]

    # Cash-based long-only sizing: no margin, leverage or short-selling.
    equity = fnum(getattr(account, "equity", 0))
    cash = fnum(getattr(account, "cash", 0))
    reserved_open_buy_dollars = 0.0
    for order in open_orders:
        side = str(order.side).lower()
        if side.endswith("buy"):
            qty = fnum(getattr(order, "qty", 0))
            limit_price = fnum(getattr(order, "limit_price", 0))
            row = scan_by_symbol.get(alpaca_symbol(order.symbol))
            price_for_reserve = limit_price or (fnum(row.get("price")) if row is not None else 0.0)
            reserved_open_buy_dollars += qty * price_for_reserve
    remaining_cash = max(0.0, cash - reserved_open_buy_dollars) * (1 - CASH_RESERVE_FRACTION)
    existing_count = len(pos_by_symbol) + pending_buys
    slots = max(0, MAX_POSITIONS - existing_count)

    if not bull:
        print("  No new entries: SPY is not above its 200-day average.")
    elif slots <= 0:
        print(f"  No new entries: at the {MAX_POSITIONS}-position limit (including open buy orders).")
    else:
        candidates = scan[scan["eligible"]].copy()
        if candidates.empty:
            print("  No new entries: no stocks passed the momentum/liquidity/volatility filters.")
        else:
            candidates["score"] = pd.to_numeric(candidates["score"], errors="coerce").fillna(-math.inf)
            candidates["breakout_watch"] = candidates["breakout_watch"].astype(bool)
            candidates = candidates.sort_values(["breakout_watch", "score"], ascending=[False, False])
            already_in = set(pos_by_symbol)
            for _, row in candidates.iterrows():
                if slots <= 0:
                    break
                symbol = str(row["symbol_alpaca"])
                if symbol in already_in or symbol in symbols_with_open_orders:
                    continue
                if not valid_symbol(symbol):
                    continue
                price = fnum(row.get("price"))
                atr = fnum(row.get("atr14"))
                if price < 5 or atr <= 0:
                    continue
                stop_distance = STOP_ATR_MULTIPLE * atr
                if stop_distance <= 0:
                    continue
                risk_qty = math.floor((equity * RISK_PER_TRADE) / stop_distance)
                weight_qty = math.floor(min(equity * MAX_POSITION_WEIGHT, MAX_ORDER_NOTIONAL_USD) / price)
                cash_qty = math.floor(remaining_cash / price)
                qty = min(risk_qty, weight_qty, cash_qty)
                if qty < 1:
                    continue
                notional = qty * price
                breakout_text = "breakout watch" if bool(row.get("breakout_watch")) else "ranked momentum"
                actions.append({"action": "BUY", "symbol": symbol, "qty": qty, "price": price, "reason": breakout_text, "score": fnum(row.get("score")), "atr14": atr})
                print(f"  BUY {symbol}: {qty} shares (~${notional:,.2f}), score {fnum(row.get('score')):.1f}, {breakout_text}")
                remaining_cash = max(0.0, remaining_cash - notional)
                slots -= 1
                already_in.add(symbol)
    if not actions:
        print("No orders in this plan.")

    ensure_data_dir()
    pd.DataFrame(actions, columns=["action", "symbol", "qty", "price", "reason", "score", "atr14"]).to_csv(PLAN_FILE, index=False)
    save_local_state(managed)
    print(f"Plan saved to: {PLAN_FILE}")
    if not dry_run:
        submit_plan(trading, actions, session_date)
    return actions


def submit_plan(trading: Any, actions: list[dict[str, Any]], session_date: str) -> None:
    if not actions:
        print("No paper orders to submit.")
        return
    print("\nSUBMITTING SIMULATED ORDERS TO ALPACA PAPER ACCOUNT")
    for item in actions:
        side_text = str(item["action"]).upper()
        side = OrderSide.BUY if side_text == "BUY" else OrderSide.SELL
        symbol = str(item["symbol"])
        qty = float(item["qty"])
        # A deterministic client ID reduces accidental duplicate submissions if the same plan is rerun.
        safe_symbol = re.sub(r"[^A-Z0-9]", "", symbol.upper())
        client_id = f"qm-{session_date.replace('-', '')}-{safe_symbol}-{side_text.lower()}"[:48]
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_id,
        )
        try:
            response = trading.submit_order(order_data=request)
            print(f"  SUBMITTED PAPER {side_text} {symbol} qty={qty:g} status={response.status} order_id={response.id}")
        except Exception as exc:
            print(f"  FAILED {side_text} {symbol}: {type(exc).__name__}: {exc}")
    print("Check 'python alpaca_paper.py status' and the Alpaca paper dashboard to confirm fills.")


def self_test() -> None:
    assert alpaca_symbol("BRK-B") == "BRK.B"
    assert yahoo_symbol("BRK.B") == "BRK-B"
    assert valid_symbol("AAPL") and valid_symbol("BRK.B") and not valid_symbol("AAPL;rm")
    assert math.floor((10_000 * RISK_PER_TRADE) / (2.5 * 2)) == 10
    print("All offline Alpaca connector self-tests passed. No network calls or orders were made.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-only Alpaca connector for the Quant Momentum Bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="test API authentication and show paper account summary")
    sub.add_parser("status", help="show paper account, positions and open orders")
    p_prices = sub.add_parser("prices", help="show latest available IEX trade prices")
    p_prices.add_argument("symbols", nargs="+", help="ticker symbols, e.g. AAPL NVDA SPY")
    p_plan = sub.add_parser("plan", help="refresh momentum scan and preview proposed orders; sends no orders")
    p_plan.add_argument("--submit", action="store_true", help="submit the displayed plan as Alpaca PAPER orders")
    sub.add_parser("self-test", help="run offline tests; no API calls or orders")
    args = parser.parse_args()

    if args.command == "self-test":
        self_test()
        return
    trading, market_data = make_clients()
    if args.command == "check":
        print_account(trading)
        print("\nConnection successful. The client is hard-coded to paper=True.")
    elif args.command == "status":
        show_status(trading)
    elif args.command == "prices":
        show_prices(market_data, args.symbols)
    elif args.command == "plan":
        make_plan(trading, dry_run=not args.submit)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(130)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Scanner failed with exit code {exc.returncode}. No new orders were submitted.")
