#!/usr/bin/env python3
"""Quant-style US stock/ETF momentum scanner and paper-trading simulator.

IMPORTANT: This program never connects to a broker and never places real orders.
It uses public Yahoo Finance data via yfinance for educational/research use.
Run after the US market close for the cleanest daily-bar signals.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:  # friendlier message if dependencies have not been installed
    yf = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "bot_data"
STATE_FILE = DATA_DIR / "paper_state.json"
TRADES_FILE = DATA_DIR / "paper_trades.csv"
LATEST_SCAN_FILE = DATA_DIR / "latest_scan.csv"
LATEST_SCAN_META_FILE = DATA_DIR / "latest_scan_meta.json"

# ---------- Beginner-editable configuration ----------
INITIAL_CASH = 10_000.00          # virtual USD only
MAX_POSITIONS = 5
RISK_PER_TRADE = 0.005             # risk budget is 0.5% of equity per position
MAX_POSITION_WEIGHT = 0.20         # no position larger than 20% of equity
STOP_ATR_MULTIPLE = 2.5
TRAIL_ATR_MULTIPLE = 3.0
ONE_WAY_COST_BPS = 15              # estimated spread/slippage/fees per side, not a broker quote
ONE_WAY_COST = ONE_WAY_COST_BPS / 10_000
MIN_PRICE = 5.00
MIN_AVG_DOLLAR_VOLUME = 2_000_000 # average daily dollar turnover, last 20 sessions
MIN_ATR_PCT = 0.008                # deliberately seeks some volatility
MAX_ATR_PCT = 0.12                 # avoid the most chaotic/illiquid price action
MIN_SCREEN_HISTORY = 130
MAX_CANDIDATES = 500
DOWNLOAD_BATCH_SIZE = 75

# Stable fallback universe, plus dynamic Yahoo screen results to surface less-followed names.
# No symbol list can discover every listed company. Screen results change over time.
STARTER_SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "SMH", "XBI", "XLF", "XLE", "XLK", "XLY",
    "XLI", "XLV", "XLP", "ARKK", "NVDA", "AMD", "PLTR", "SOFI", "HOOD",
    "RKLB", "HIMS", "IONQ", "ASTS", "CAVA", "CELH", "APP", "DUOL", "CRWD",
    "SMCI", "CLSK", "COIN", "UBER", "DKNG", "TOST", "RDDT", "SOUN", "PATH",
    "TEM", "ALAB", "VRT", "AVGO", "TSLA", "MSTR", "AFRM", "PINS", "RBLX",
]
SCREENS = [
    "small_cap_gainers",
    "day_gainers",
    "growth_technology_stocks",
    "most_actives",
    "aggressive_small_caps",
    "top_etfs_us",
]


def require_dependencies() -> None:
    if yf is None:
        raise SystemExit(
            "Missing packages. Run: python -m pip install -r requirements.txt"
        )


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def valid_symbol(symbol: str) -> bool:
    symbol = str(symbol or "").strip().upper()
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,7}", symbol))


def discover_candidates() -> tuple[list[str], dict[str, str]]:
    """Use Yahoo Finance's public screener plus a small fallback watchlist."""
    require_dependencies()
    symbols: list[str] = []
    names: dict[str, str] = {}
    sources_ok = 0
    for screen_name in SCREENS:
        try:
            response = yf.screen(screen_name, count=100)
            quotes = response.get("quotes", []) if isinstance(response, dict) else []
            found = 0
            for quote in quotes:
                symbol = str(quote.get("symbol", "")).upper().strip()
                if not valid_symbol(symbol):
                    continue
                # Yahoo screen data is being used for discovery, not as a buy signal.
                if symbol not in symbols:
                    symbols.append(symbol)
                names[symbol] = str(quote.get("shortName") or quote.get("longName") or symbol)
                found += 1
            if found:
                sources_ok += 1
                print(f"  Screener {screen_name}: {found} symbols")
            else:
                print(f"  Screener {screen_name}: returned no symbols")
        except Exception as exc:
            print(f"  Warning: screener {screen_name} unavailable ({type(exc).__name__})")

    for symbol in STARTER_SYMBOLS:
        if symbol not in symbols:
            symbols.append(symbol)
        names.setdefault(symbol, symbol)

    # Preserve a mix of screens before applying the cap, so one large screen cannot crowd out others.
    symbols = symbols[:MAX_CANDIDATES]
    print(f"Candidate universe: {len(symbols)} symbols ({sources_ok}/{len(SCREENS)} screens responded)")
    if sources_ok == 0:
        print("Note: live screen discovery failed; using the built-in fallback watchlist only.")
    return symbols, names


def _normalise_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(col[0]) for col in frame.columns]
    frame.columns = [str(c).title() for c in frame.columns]
    wanted = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in frame.columns]
    frame = frame[wanted]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame.dropna(subset=[c for c in ["Close"] if c in frame.columns])


def _ticker_frame(raw: pd.DataFrame, symbol: str, requested_count: int) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(map(str, raw.columns.get_level_values(0)))
        level1 = set(map(str, raw.columns.get_level_values(1)))
        if symbol in level0:
            frame = raw[symbol].copy()
        elif symbol in level1:
            frame = raw.xs(symbol, axis=1, level=1).copy()
        else:
            return pd.DataFrame()
    elif requested_count == 1:
        frame = raw.copy()
    else:
        # Some yfinance versions return flat columns when only one symbol succeeds.
        return pd.DataFrame()
    return _normalise_columns(frame)


def download_history(symbols: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    """Download adjusted daily OHLCV in batches to reduce API pressure."""
    require_dependencies()
    unique = list(dict.fromkeys(s for s in symbols if valid_symbol(s)))
    result: dict[str, pd.DataFrame] = {}
    for start in range(0, len(unique), DOWNLOAD_BATCH_SIZE):
        batch = unique[start:start + DOWNLOAD_BATCH_SIZE]
        try:
            raw = yf.download(
                tickers=batch,
                period=period,
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
                actions=False,
                multi_level_index=True,
            )
            for symbol in batch:
                frame = _ticker_frame(raw, symbol, len(batch))
                if not frame.empty and len(frame) >= 25:
                    result[symbol] = frame
        except Exception as exc:
            print(f"  Warning: price download batch failed ({type(exc).__name__}); continuing")
        print(f"  Downloaded batch {min(start + len(batch), len(unique))}/{len(unique)} symbols; usable histories: {len(result)}")
    return result


def latest_completed_date(benchmark: pd.DataFrame) -> pd.Timestamp:
    if benchmark.empty:
        raise RuntimeError("Could not download SPY benchmark history. Check your internet connection or data source.")
    # Avoid using a still-forming daily candle if the script is run during US market hours.
    latest = pd.Timestamp(benchmark.index.max()).tz_localize(None).normalize()
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.hour < 16 and latest.date() == now_et.date():
        # The daily candle may be incomplete before the regular market close.
        all_dates = pd.to_datetime(benchmark.index).tz_localize(None).normalize().unique()
        earlier = [d for d in all_dates if d.date() < now_et.date()]
        if earlier:
            latest = pd.Timestamp(earlier[-1])
            print("Warning: US market has not closed yet; using the previous available SPY session.")
    return latest


def calculate_features(frame: pd.DataFrame, benchmark: pd.DataFrame, asof: pd.Timestamp) -> dict[str, Any] | None:
    """Create lag-aware momentum, volatility, liquidity and breakout features."""
    if frame.empty or "Close" not in frame.columns:
        return None
    b = frame.copy()
    b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
    b = b.loc[b.index <= asof].dropna(subset=["Close"])
    if len(b) < MIN_SCREEN_HISTORY:
        return None
    if not all(c in b.columns for c in ["High", "Low", "Volume", "Open"]):
        return None

    close = pd.to_numeric(b["Close"], errors="coerce")
    high = pd.to_numeric(b["High"], errors="coerce")
    low = pd.to_numeric(b["Low"], errors="coerce")
    volume = pd.to_numeric(b["Volume"], errors="coerce")
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr14 = true_range.rolling(14).mean()
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    prior_high20 = high.shift(1).rolling(20).max()
    prior_avg_volume20 = volume.rolling(20).mean().shift(1)
    avg_dollar_volume20 = (close * volume).rolling(20).mean().shift(1)

    last_close = float(close.iloc[-1])
    last_atr = float(atr14.iloc[-1]) if pd.notna(atr14.iloc[-1]) else math.nan
    last_sma20 = float(sma20.iloc[-1]) if pd.notna(sma20.iloc[-1]) else math.nan
    last_sma50 = float(sma50.iloc[-1]) if pd.notna(sma50.iloc[-1]) else math.nan
    last_prior_high = float(prior_high20.iloc[-1]) if pd.notna(prior_high20.iloc[-1]) else math.nan
    last_avg_dollar = float(avg_dollar_volume20.iloc[-1]) if pd.notna(avg_dollar_volume20.iloc[-1]) else math.nan
    avg_vol = float(prior_avg_volume20.iloc[-1]) if pd.notna(prior_avg_volume20.iloc[-1]) else math.nan
    last_volume = float(volume.iloc[-1]) if pd.notna(volume.iloc[-1]) else math.nan

    ret20 = float(close.pct_change(20).iloc[-1]) if len(close) > 20 else math.nan
    ret60 = float(close.pct_change(60).iloc[-1]) if len(close) > 60 else math.nan
    ret120 = float(close.pct_change(120).iloc[-1]) if len(close) > 120 else math.nan
    atr_pct = last_atr / last_close if last_close > 0 and math.isfinite(last_atr) else math.nan
    volume_ratio = last_volume / avg_vol if avg_vol > 0 and math.isfinite(last_volume) else math.nan
    dist_high = last_close / last_prior_high - 1 if last_prior_high > 0 else math.nan

    # Relative strength is stock 60-session return minus SPY's 60-session return over the same dates.
    benchmark = benchmark.copy()
    benchmark.index = pd.to_datetime(benchmark.index).tz_localize(None).normalize()
    benchmark = benchmark.loc[benchmark.index <= asof]
    bench_close = benchmark["Close"].dropna() if "Close" in benchmark.columns else pd.Series(dtype=float)
    stock_ret60 = close.pct_change(60).dropna()
    common = stock_ret60.index.intersection(bench_close.index)
    if len(common) >= 61:
        # Compute both returns on the same date (using each series' aligned 60-session change).
        bret = bench_close.pct_change(60)
        relative_strength = float(stock_ret60.loc[common[-1]] - bret.loc[common[-1]])
    else:
        relative_strength = math.nan

    # Today's close is used for features; execution is modelled at the next session's open.
    return {
        "price": last_close,
        "atr14": last_atr,
        "atr_pct": atr_pct,
        "sma20": last_sma20,
        "sma50": last_sma50,
        "return_20d": ret20,
        "return_60d": ret60,
        "return_120d": ret120,
        "relative_strength_60d": relative_strength,
        "volume_ratio": volume_ratio,
        "avg_dollar_volume_20d": last_avg_dollar,
        "distance_to_prior_20d_high": dist_high,
        "prior_20d_high": last_prior_high,
        "session_date": str(b.index[-1].date()),
    }


def benchmark_regime(benchmark: pd.DataFrame, asof: pd.Timestamp) -> tuple[bool, float, float]:
    b = benchmark.copy()
    b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
    b = b.loc[b.index <= asof]
    close = b["Close"].dropna()
    if len(close) < 200:
        return False, float(close.iloc[-1]) if len(close) else math.nan, math.nan
    price = float(close.iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    return price > sma200, price, sma200


def make_ranked_scan(
    history: dict[str, pd.DataFrame], names: dict[str, str], asof: pd.Timestamp
) -> tuple[pd.DataFrame, bool, float, float]:
    if "SPY" not in history:
        raise RuntimeError("SPY benchmark history is missing; scanner cannot assess market regime.")
    benchmark = history["SPY"]
    rows: list[dict[str, Any]] = []
    for symbol, frame in history.items():
        if symbol == "SPY":
            continue
        try:
            feats = calculate_features(frame, benchmark, asof)
            if feats:
                feats["symbol"] = symbol
                feats["name"] = names.get(symbol, symbol)
                rows.append(feats)
        except (ValueError, TypeError, IndexError, KeyError):
            continue
    if not rows:
        raise RuntimeError("No symbols had enough clean historical data to score. Try again later.")
    df = pd.DataFrame(rows).set_index("symbol")
    numeric = [
        "return_20d", "return_60d", "return_120d", "relative_strength_60d",
        "volume_ratio", "distance_to_prior_20d_high",
    ]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # Cross-sectional percentile ranks are more stable than mixing raw returns and volume multiples.
    df["rank_20d"] = df["return_20d"].rank(pct=True)
    df["rank_60d"] = df["return_60d"].rank(pct=True)
    df["rank_120d"] = df["return_120d"].rank(pct=True)
    df["rank_relative_strength"] = df["relative_strength_60d"].rank(pct=True)
    df["rank_volume"] = df["volume_ratio"].clip(upper=5).rank(pct=True)
    df["rank_near_high"] = (-df["distance_to_prior_20d_high"].abs()).rank(pct=True)
    df["score"] = (
        0.15 * df["rank_20d"]
        + 0.25 * df["rank_60d"]
        + 0.20 * df["rank_120d"]
        + 0.20 * df["rank_relative_strength"]
        + 0.10 * df["rank_volume"]
        + 0.10 * df["rank_near_high"]
    ) * 100
    df["eligible"] = (
        (df["price"] >= MIN_PRICE)
        & (df["avg_dollar_volume_20d"] >= MIN_AVG_DOLLAR_VOLUME)
        & (df["sma50"] < df["price"])
        & (df["return_20d"] > 0)
        & (df["return_60d"] > 0)
        & (df["relative_strength_60d"] > 0)
        & (df["atr_pct"] >= MIN_ATR_PCT)
        & (df["atr_pct"] <= MAX_ATR_PCT)
    )
    df["breakout_watch"] = (
        df["eligible"]
        & (df["distance_to_prior_20d_high"] >= -0.035)
        & (df["distance_to_prior_20d_high"] <= 0.035)
        & (df["volume_ratio"] >= 1.20)
    )
    bull, spy_price, spy_sma200 = benchmark_regime(benchmark, asof)
    df = df.sort_values("score", ascending=False)
    return df, bull, spy_price, spy_sma200


def pretty_percent(value: Any) -> str:
    try:
        if pd.isna(value):
            return "n/a"
        return f"{float(value) * 100:,.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def display_scan(df: pd.DataFrame, asof: pd.Timestamp, bull: bool, spy: float, spy_sma: float) -> None:
    print("\n" + "=" * 88)
    print(f"DAILY MOMENTUM SCAN — US STOCKS & ETFs — data through {asof.date()}")
    print(f"SPY: ${spy:,.2f} | 200-day SMA: ${spy_sma:,.2f} | market regime: {'RISK-ON' if bull else 'RISK-OFF / NOT CONFIRMED'}")
    print("This is a statistical watchlist, not a promise or prediction of future gains.")
    eligible = df[df["eligible"]].copy()
    cols = ["name", "price", "score", "return_20d", "return_60d", "relative_strength_60d", "volume_ratio", "atr_pct", "distance_to_prior_20d_high"]
    if eligible.empty:
        print("\nNo candidates passed all liquidity, trend, momentum and volatility filters today.")
    else:
        print("\nTOP MOMENTUM CANDIDATES (ranked by composite score):")
        view = eligible[cols].head(20).copy()
        view["price"] = view["price"].map(lambda x: f"${x:,.2f}")
        for c in ["return_20d", "return_60d", "relative_strength_60d", "atr_pct", "distance_to_prior_20d_high"]:
            view[c] = view[c].map(pretty_percent)
        view["score"] = view["score"].map(lambda x: f"{x:.1f}")
        view["volume_ratio"] = view["volume_ratio"].map(lambda x: f"{x:.2f}x" if pd.notna(x) else "n/a")
        print(view.rename(columns={
            "name": "Name", "price": "Price", "score": "Score", "return_20d": "20d", "return_60d": "60d",
            "relative_strength_60d": "vs SPY 60d", "volume_ratio": "Vol vs avg", "atr_pct": "ATR%", "distance_to_prior_20d_high": "vs prior 20d high",
        }).to_string())
    breakout = df[df["breakout_watch"]]
    print("\nBREAKOUT WATCH (near prior 20-day high with elevated volume):")
    if breakout.empty:
        print("No candidates met the breakout-watch criteria today.")
    else:
        for symbol, row in breakout.head(15).iterrows():
            print(
                f"  {symbol:7} {str(row['name'])[:24]:24} price=${row['price']:8.2f} "
                f"score={row['score']:5.1f} vs-high={pretty_percent(row['distance_to_prior_20d_high']):>7} "
                f"volume={row['volume_ratio']:.2f}x ATR={pretty_percent(row['atr_pct'])}"
            )
    if not bull:
        print("\nRisk control: the SPY close is not above its 200-day average, so the paper bot will not schedule new longs.")


def save_scan(df: pd.DataFrame) -> None:
    ensure_data_dir()
    output = df.reset_index().sort_values("score", ascending=False)
    output.to_csv(LATEST_SCAN_FILE, index=False)
    print(f"\nFull scan saved to: {LATEST_SCAN_FILE}")


def save_scan_metadata(asof: pd.Timestamp, bull: bool, spy_price: float, spy_sma200: float) -> None:
    """Persist benchmark/date metadata so the Alpaca connector can use the same scan."""
    ensure_data_dir()
    payload = {
        "session_date": str(pd.Timestamp(asof).date()),
        "market_regime_bullish": bool(bull),
        "spy_close": float(spy_price) if math.isfinite(float(spy_price)) else None,
        "spy_sma200": float(spy_sma200) if math.isfinite(float(spy_sma200)) else None,
        "saved_utc": datetime.now(timezone.utc).isoformat(),
    }
    with LATEST_SCAN_META_FILE.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_state() -> dict[str, Any]:
    ensure_data_dir()
    if not STATE_FILE.exists():
        return {
            "cash": INITIAL_CASH,
            "positions": {},
            "pending_orders": [],
            "last_processed_date": None,
            "last_equity": INITIAL_CASH,
            "equity_curve": [],
            "created_utc": datetime.now(timezone.utc).isoformat(),
        }
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("cash", INITIAL_CASH)
        state.setdefault("positions", {})
        state.setdefault("pending_orders", [])
        state.setdefault("last_processed_date", None)
        state.setdefault("last_equity", INITIAL_CASH)
        state.setdefault("equity_curve", [])
        return state
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read paper state {STATE_FILE}: {exc}. Back up the file before editing it.")


def save_state(state: dict[str, Any]) -> None:
    ensure_data_dir()
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def append_trade(trade: dict[str, Any]) -> None:
    ensure_data_dir()
    row = pd.DataFrame([trade])
    row.to_csv(TRADES_FILE, mode="a", index=False, header=not TRADES_FILE.exists())


def marked_equity(state: dict[str, Any], history: dict[str, pd.DataFrame], asof: pd.Timestamp) -> float:
    total = float(state["cash"])
    for symbol, position in state["positions"].items():
        frame = history.get(symbol)
        if frame is None or frame.empty:
            total += float(position["shares"]) * float(position.get("last_mark", position["entry_price"]))
            continue
        b = frame.copy()
        b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
        b = b.loc[b.index <= asof]
        if not b.empty and pd.notna(b["Close"].iloc[-1]):
            price = float(b["Close"].iloc[-1])
            position["last_mark"] = price
        else:
            price = float(position.get("last_mark", position["entry_price"]))
        total += int(position["shares"]) * price
    return total


def size_position(equity: float, cash: float, price: float, atr14: float) -> int:
    """Conservative integer-share sizing using both stop risk and a max weight."""
    if not all(math.isfinite(v) and v > 0 for v in [equity, cash, price, atr14]):
        return 0
    risk_budget = equity * RISK_PER_TRADE
    risk_per_share = STOP_ATR_MULTIPLE * atr14
    shares_by_risk = int(risk_budget / risk_per_share) if risk_per_share > 0 else 0
    shares_by_weight = int((equity * MAX_POSITION_WEIGHT) / price)
    shares_by_cash = int(cash / (price * (1 + ONE_WAY_COST)))
    return max(0, min(shares_by_risk, shares_by_weight, shares_by_cash))


def get_bar(frame: pd.DataFrame, date: pd.Timestamp) -> pd.Series | None:
    b = frame.copy()
    b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
    if date not in b.index:
        return None
    row = b.loc[date]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    return row


def paper_trade(df: pd.DataFrame, history: dict[str, pd.DataFrame], asof: pd.Timestamp, bull: bool) -> None:
    """Run one end-of-day cycle. Fills pending signals at the next available daily OPEN."""
    state = load_state()
    day = str(asof.date())
    previous_day = state.get("last_processed_date")
    if previous_day and day <= previous_day:
        print(f"\nPaper portfolio unchanged: latest market session {day} was already processed (last: {previous_day}).")
        print_status(state, history, asof)
        return

    # Execute any older pending orders at today's Open, with a conservative all-in cost assumption.
    kept_orders: list[dict[str, Any]] = []
    orders = list(state.get("pending_orders", []))
    for order in orders:
        symbol = order.get("symbol", "")
        created = str(order.get("created_date", ""))
        frame = history.get(symbol)
        if frame is None or frame.empty:
            if (pd.Timestamp(day) - pd.Timestamp(created)).days <= 7:
                kept_orders.append(order)
            continue
        bar = get_bar(frame, asof)
        if bar is None or not pd.notna(bar.get("Open", np.nan)):
            if (pd.Timestamp(day) - pd.Timestamp(created)).days <= 7:
                kept_orders.append(order)
            continue
        if not created or day <= created:
            kept_orders.append(order)
            continue
        if (pd.Timestamp(day) - pd.Timestamp(created)).days > 7:
            print(f"Expired pending {order.get('side')} order for {symbol}; no usable next-session fill.")
            continue
        open_price = float(bar["Open"])
        if open_price <= 0 or not math.isfinite(open_price):
            kept_orders.append(order)
            continue

        side = order.get("side")
        if side == "sell":
            position = state["positions"].get(symbol)
            if not position:
                continue
            shares = int(position["shares"])
            fill_price = open_price * (1 - ONE_WAY_COST)
            proceeds = shares * fill_price
            state["cash"] += proceeds
            entry_price = float(position["entry_price"])
            realised = (fill_price - entry_price) * shares
            append_trade({
                "date": day, "symbol": symbol, "side": "SELL", "shares": shares,
                "fill_price": round(fill_price, 4), "reason": order.get("reason", "signal exit"),
                "realised_pnl_usd": round(realised, 2), "score_at_signal": order.get("score", ""),
                "cost_assumption_bps_per_side": ONE_WAY_COST_BPS,
            })
            del state["positions"][symbol]
            print(f"  PAPER SELL {symbol}: {shares} shares at ${fill_price:.2f}; realised P/L ${realised:+.2f}")
        elif side == "buy":
            if symbol in state["positions"] or len(state["positions"]) >= MAX_POSITIONS:
                continue
            atr = float(order.get("atr14", 0) or 0)
            eq_now = marked_equity(state, history, asof)
            fill_price = open_price * (1 + ONE_WAY_COST)
            shares = size_position(eq_now, float(state["cash"]), fill_price, atr)
            if shares <= 0:
                continue
            cost = shares * fill_price
            if cost > float(state["cash"]):
                continue
            state["cash"] -= cost
            stop_price = fill_price - STOP_ATR_MULTIPLE * atr
            state["positions"][symbol] = {
                "shares": shares,
                "entry_price": fill_price,
                "entry_date": day,
                "entry_atr14": atr,
                "initial_stop": stop_price,
                "high_watermark": float(bar.get("Close", open_price)),
                "last_mark": float(bar.get("Close", open_price)),
                "score_at_entry": float(order.get("score", 0) or 0),
            }
            append_trade({
                "date": day, "symbol": symbol, "side": "BUY", "shares": shares,
                "fill_price": round(fill_price, 4), "reason": order.get("reason", "momentum entry"),
                "realised_pnl_usd": "", "score_at_signal": order.get("score", ""),
                "cost_assumption_bps_per_side": ONE_WAY_COST_BPS,
            })
            print(f"  PAPER BUY  {symbol}: {shares} shares at ${fill_price:.2f}; initial stop ${stop_price:.2f}")

    state["pending_orders"] = kept_orders

    # Mark positions at today's close; queue exits for the next available session's open.
    queued_sells = {o.get("symbol") for o in state["pending_orders"] if o.get("side") == "sell"}
    for symbol, position in list(state["positions"].items()):
        frame = history.get(symbol)
        if frame is None or frame.empty:
            continue
        features = calculate_features(frame, history["SPY"], asof)
        bar = get_bar(frame, asof)
        if not features or bar is None or not pd.notna(bar.get("Close", np.nan)):
            continue
        close = float(bar["Close"])
        position["last_mark"] = close
        position["high_watermark"] = max(float(position.get("high_watermark", close)), close)
        atr = float(position.get("entry_atr14", features["atr14"]))
        initial_stop = float(position.get("initial_stop", close - STOP_ATR_MULTIPLE * atr))
        trailing_stop = float(position["high_watermark"]) - TRAIL_ATR_MULTIPLE * atr
        stop = max(initial_stop, trailing_stop)
        if symbol not in queued_sells and (close <= stop or close < float(features["sma20"])):
            reason = "ATR stop/trailing stop" if close <= stop else "close below 20-day average"
            state["pending_orders"].append({
                "symbol": symbol, "side": "sell", "created_date": day,
                "reason": reason, "score": position.get("score_at_entry", ""),
            })
            queued_sells.add(symbol)
            print(f"  EXIT SIGNAL {symbol}: close ${close:.2f}; {reason}; queued for next session open")

    # New buy signals are created after the close and cannot fill until a later session's open.
    pending_buy_symbols = {o.get("symbol") for o in state["pending_orders"] if o.get("side") == "buy"}
    pending_sell_symbols = {o.get("symbol") for o in state["pending_orders"] if o.get("side") == "sell"}
    if bull:
        slots = MAX_POSITIONS - len(state["positions"]) - len(pending_buy_symbols)
        if slots > 0:
            candidates = df[(df["eligible"])].sort_values("score", ascending=False)
            for symbol, row in candidates.iterrows():
                if slots <= 0:
                    break
                if symbol in state["positions"] or symbol in pending_buy_symbols or symbol in pending_sell_symbols:
                    continue
                state["pending_orders"].append({
                    "symbol": symbol, "side": "buy", "created_date": day,
                    "reason": "ranked daily momentum signal",
                    "atr14": float(row["atr14"]), "score": float(row["score"]),
                })
                pending_buy_symbols.add(symbol)
                slots -= 1
                print(f"  BUY SIGNAL {symbol}: score {row['score']:.1f}; queued for next session open")
    else:
        print("  Risk-off: no new long entries queued because SPY is not above its 200-day average.")

    equity = marked_equity(state, history, asof)
    previous_equity = float(state.get("last_equity", INITIAL_CASH))
    state["last_equity"] = equity
    state["last_processed_date"] = day
    state.setdefault("equity_curve", []).append({"date": day, "equity": round(equity, 2), "cash": round(float(state["cash"]), 2), "positions": len(state["positions"])})
    state["equity_curve"] = state["equity_curve"][-1500:]
    save_state(state)
    daily_pnl = equity - previous_equity
    print(f"\nPaper ledger updated for {day}. Equity ${equity:,.2f} | cash ${state['cash']:,.2f} | change vs prior saved mark ${daily_pnl:+,.2f}")
    print("No real orders were sent. Fills are simulated from daily opening prices plus the configured cost assumption.")
    print_status(state, history, asof)


def print_status(state: dict[str, Any], history: dict[str, pd.DataFrame] | None = None, asof: pd.Timestamp | None = None) -> None:
    cash = float(state.get("cash", INITIAL_CASH))
    equity = cash
    lines: list[tuple[str, int, float, float]] = []
    for symbol, position in state.get("positions", {}).items():
        price = float(position.get("last_mark", position.get("entry_price", 0)))
        if history is not None and asof is not None and symbol in history:
            bar = get_bar(history[symbol], asof)
            if bar is not None and pd.notna(bar.get("Close", np.nan)):
                price = float(bar["Close"])
        shares = int(position["shares"])
        market_value = shares * price
        equity += market_value
        unrealised = (price - float(position["entry_price"])) * shares
        lines.append((symbol, shares, price, unrealised))
    print("\nPAPER PORTFOLIO")
    print(f"  Virtual cash: ${cash:,.2f}")
    print(f"  Marked equity: ${equity:,.2f}")
    print(f"  Open positions: {len(lines)}/{MAX_POSITIONS}")
    if lines:
        for symbol, shares, price, pnl in lines:
            print(f"    {symbol:7} {shares:5} shares | mark ${price:8.2f} | unrealised P/L ${pnl:+9.2f}")
    else:
        print("    (none)")
    pending = state.get("pending_orders", [])
    if pending:
        print("  Pending next-session orders:")
        for order in pending:
            print(f"    {order.get('side', '').upper():4} {order.get('symbol', ''):7} created {order.get('created_date', '')} — {order.get('reason', '')}")
    curve = state.get("equity_curve", [])
    if len(curve) >= 2:
        start = float(curve[0]["equity"])
        change = equity / start - 1 if start else 0
        peak = max(float(p["equity"]) for p in curve)
        dd = equity / peak - 1 if peak else 0
        print(f"  Since first saved mark: {pretty_percent(change)} | drawdown from saved peak: {pretty_percent(dd)}")
    print(f"  State file: {STATE_FILE}")
    print(f"  Trade log:  {TRADES_FILE}")


def run_backtest(history: dict[str, pd.DataFrame], df: pd.DataFrame, asof: pd.Timestamp, days: int = 252) -> None:
    """Exploratory daily-rebalanced, long-only cross-sectional backtest with next-open fills.

    Caveat: it uses today's discovered universe for old dates, which causes universe/survivorship
    bias. Treat results as a code sanity check only, not a reliable estimate of future performance.
    """
    if "SPY" not in history:
        raise RuntimeError("SPY history required for backtest")
    benchmark = history["SPY"].copy()
    benchmark.index = pd.to_datetime(benchmark.index).tz_localize(None).normalize()
    dates = benchmark.index.drop_duplicates().sort_values()
    close_panel: dict[str, pd.Series] = {}
    open_panel: dict[str, pd.Series] = {}
    feature_map: dict[str, pd.DataFrame] = {}
    for symbol, frame in history.items():
        if symbol == "SPY" or len(frame) < 130:
            continue
        b = frame.copy()
        b.index = pd.to_datetime(b.index).tz_localize(None).normalize()
        b = b[~b.index.duplicated(keep="last")].reindex(dates)
        close = b["Close"].astype(float)
        high = b["High"].astype(float)
        low = b["Low"].astype(float)
        vol = b["Volume"].astype(float)
        prev = close.shift(1)
        tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        avg_dollar = (close * vol).rolling(20).mean().shift(1)
        prior_vol = vol.rolling(20).mean().shift(1)
        prior_high = high.shift(1).rolling(20).max()
        ret20, ret60, ret120 = close.pct_change(20), close.pct_change(60), close.pct_change(120)
        bench_close = benchmark["Close"].reindex(dates).astype(float)
        relative = ret60 - bench_close.pct_change(60)
        vol_ratio = vol / prior_vol
        dist_high = close / prior_high - 1
        feats = pd.DataFrame({
            "price": close, "open": b["Open"].astype(float), "sma20": close.rolling(20).mean(),
            "sma50": close.rolling(50).mean(), "atr_pct": atr / close, "avg_dollar": avg_dollar,
            "ret20": ret20, "ret60": ret60, "ret120": ret120, "relative": relative,
            "volume_ratio": vol_ratio, "distance_high": dist_high,
        }, index=dates)
        feature_map[symbol] = feats
        close_panel[symbol] = close
        open_panel[symbol] = b["Open"].astype(float)

    if not feature_map:
        print("Not enough historical data for a backtest.")
        return
    features = {k: pd.DataFrame({s: v[k] for s, v in feature_map.items()}, index=dates) for k in [
        "price", "open", "sma20", "sma50", "atr_pct", "avg_dollar", "ret20", "ret60", "ret120", "relative", "volume_ratio", "distance_high"
    ]}
    score = (
        .15 * features["ret20"].rank(axis=1, pct=True)
        + .25 * features["ret60"].rank(axis=1, pct=True)
        + .20 * features["ret120"].rank(axis=1, pct=True)
        + .20 * features["relative"].rank(axis=1, pct=True)
        + .10 * features["volume_ratio"].clip(upper=5).rank(axis=1, pct=True)
        + .10 * (-features["distance_high"].abs()).rank(axis=1, pct=True)
    )
    eligible = (
        (features["price"] >= MIN_PRICE)
        & (features["avg_dollar"] >= MIN_AVG_DOLLAR_VOLUME)
        & (features["price"] > features["sma50"])
        & (features["ret20"] > 0)
        & (features["ret60"] > 0)
        & (features["relative"] > 0)
        & (features["atr_pct"] >= MIN_ATR_PCT)
        & (features["atr_pct"] <= MAX_ATR_PCT)
    ).fillna(False)
    spy_close = benchmark["Close"].reindex(dates)
    spy_sma200 = spy_close.rolling(200).mean()
    open_prices = pd.DataFrame(open_panel, index=dates)
    open_returns = open_prices.shift(-1).div(open_prices).sub(1)

    first = max(201, len(dates) - max(1, days) - 1)
    equity = INITIAL_CASH
    equity_rows: list[tuple[pd.Timestamp, float]] = []
    previous_weights: dict[str, float] = {}
    daily_returns: list[float] = []
    for i in range(first, len(dates) - 1):
        signal_idx = i - 1  # signals use prior close; trades are filled at today's open
        if pd.isna(spy_sma200.iloc[signal_idx]) or spy_close.iloc[signal_idx] <= spy_sma200.iloc[signal_idx]:
            target: dict[str, float] = {}
        else:
            row_score = score.iloc[signal_idx]
            row_eligible = eligible.iloc[signal_idx]
            candidates = row_score[row_eligible].dropna().sort_values(ascending=False)
            chosen = list(candidates.index[:MAX_POSITIONS])
            target = {symbol: 1.0 / len(chosen) for symbol in chosen} if chosen else {}

        all_syms = set(previous_weights) | set(target)
        turnover = sum(abs(target.get(sym, 0.0) - previous_weights.get(sym, 0.0)) for sym in all_syms)
        equity *= max(0.0, 1.0 - ONE_WAY_COST * turnover)
        ret_row = open_returns.iloc[i]
        gross_return = 0.0
        for symbol, weight in target.items():
            r = ret_row.get(symbol, np.nan)
            if pd.notna(r) and math.isfinite(float(r)):
                gross_return += weight * float(r)
        gross_return = max(gross_return, -0.95)
        equity *= 1 + gross_return
        # Update weights for relative price drift before next day's rebalance.
        multipliers = {s: max(0.01, 1 + float(ret_row.get(s, 0.0))) for s in target}
        denom = sum(target[s] * multipliers[s] for s in target)
        previous_weights = ({s: target[s] * multipliers[s] / denom for s in target} if denom > 0 else {})
        daily_returns.append(gross_return - ONE_WAY_COST * turnover)
        equity_rows.append((dates[i], equity))

    if not equity_rows:
        print("Not enough rows remain after the 200-day warm-up to run this backtest.")
        return
    eq = pd.Series([v for _, v in equity_rows], index=[d for d, _ in equity_rows], dtype=float)
    daily = eq.pct_change().dropna()
    total_return = eq.iloc[-1] / INITIAL_CASH - 1
    ann_return = (eq.iloc[-1] / INITIAL_CASH) ** (252 / max(1, len(eq))) - 1 if eq.iloc[-1] > 0 else -1.0
    running_peak = eq.cummax()
    max_dd = (eq / running_peak - 1).min()
    sharpe = (daily.mean() / daily.std(ddof=1) * math.sqrt(252)) if len(daily) > 1 and daily.std(ddof=1) > 0 else math.nan
    print("\nEXPLORATORY HISTORICAL BACKTEST (not a forecast)")
    print(f"Window: {eq.index[0].date()} to {eq.index[-1].date()} ({len(eq)} open-to-open holding periods)")
    print(f"Start virtual cash: ${INITIAL_CASH:,.2f}")
    print(f"End equity:         ${eq.iloc[-1]:,.2f}")
    print(f"Total return:       {pretty_percent(total_return)}")
    print(f"Annualised return:  {pretty_percent(ann_return)}")
    print(f"Maximum drawdown:   {pretty_percent(max_dd)}")
    print(f"Daily Sharpe ratio: {sharpe:.2f}" if math.isfinite(sharpe) else "Daily Sharpe ratio: n/a")
    print(f"Estimated one-way cost assumption: {ONE_WAY_COST_BPS} bps per traded dollar")
    print("Critical caveat: this historical run reuses today's dynamically discovered stock universe.")
    print("That causes survivorship/universe-selection bias. It does not prove that the strategy has an edge.")
    ensure_data_dir()
    pd.DataFrame({"date": eq.index.astype(str), "equity_usd": eq.values}).to_csv(DATA_DIR / "backtest_equity.csv", index=False)
    print(f"Equity series saved to: {DATA_DIR / 'backtest_equity.csv'}")


def self_test() -> None:
    """Offline tests: works without downloading any data."""
    qty = size_position(equity=10_000, cash=10_000, price=50, atr14=2)
    assert qty == 10, f"Expected risk budget to cap position at 10 shares, got {qty}"
    assert size_position(10_000, 100, 50, 2) == 1, "Cash constraint should cap position at 1 share after modeled costs"
    assert size_position(10_000, 10_000, 0, 2) == 0, "Zero price should be rejected by sizing checks"
    assert valid_symbol("AAPL") and valid_symbol("BRK-B") and not valid_symbol("AAPL;rm -rf")
    # Synthetic rising prices verify that feature code returns momentum metrics without network access.
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=300)
    close = pd.Series(np.linspace(20, 60, len(idx)), index=idx)
    frame = pd.DataFrame({
        "Open": close * .995, "High": close * 1.01, "Low": close * .99,
        "Close": close, "Volume": pd.Series(500_000, index=idx),
    }, index=idx)
    spy_close = pd.Series(np.linspace(100, 130, len(idx)), index=idx)
    benchmark = pd.DataFrame({"Open": spy_close, "High": spy_close * 1.01, "Low": spy_close * .99, "Close": spy_close, "Volume": 1_000_000}, index=idx)
    feat = calculate_features(frame, benchmark, idx[-1])
    assert feat and feat["return_20d"] > 0 and feat["atr14"] > 0, "Synthetic feature test failed"
    print("All offline self-tests passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily US equity momentum scanner and simulated paper-trading bot")
    parser.add_argument("command", nargs="?", choices=["scan", "paper", "backtest", "status", "reset", "self-test"], default="scan")
    parser.add_argument("--days", type=int, default=252, help="Approximate historical holding periods for backtest (default: 252)")
    parser.add_argument("--confirm-reset", action="store_true", help="Required to reset the virtual paper ledger")
    parser.add_argument("--include", nargs="*", default=[], help="Extra symbols to include in the scan (used to cover broker-held positions)")
    args = parser.parse_args()

    if args.command == "self-test":
        self_test()
        return
    if args.command == "status":
        state = load_state()
        print_status(state)
        return
    if args.command == "reset":
        if not args.confirm_reset:
            raise SystemExit("Reset not performed. Re-run with: python quant_momentum_bot.py reset --confirm-reset")
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        print("Virtual paper state reset. Existing trade log and scan files were kept.")
        return

    require_dependencies()
    symbols, names = discover_candidates()
    for extra in args.include:
        extra_symbol = str(extra).strip().upper()
        if valid_symbol(extra_symbol):
            if extra_symbol not in symbols:
                symbols.append(extra_symbol)
            names.setdefault(extra_symbol, extra_symbol)
    all_symbols = list(dict.fromkeys([*symbols, "SPY"]))
    if args.command == "paper" and STATE_FILE.exists():
        state = load_state()
        for symbol in state.get("positions", {}):
            if symbol not in all_symbols:
                all_symbols.append(symbol)
        for order in state.get("pending_orders", []):
            symbol = order.get("symbol")
            if symbol and symbol not in all_symbols:
                all_symbols.append(symbol)
    print(f"Downloading up to two years of adjusted daily OHLCV for {len(all_symbols)} symbols...")
    history = download_history(all_symbols, period="2y")
    if "SPY" not in history:
        raise SystemExit("Could not load SPY benchmark prices. Check the internet connection, then try again.")
    asof = latest_completed_date(history["SPY"])
    df, bull, spy_price, spy_sma200 = make_ranked_scan(history, names, asof)
    display_scan(df, asof, bull, spy_price, spy_sma200)
    save_scan(df)
    save_scan_metadata(asof, bull, spy_price, spy_sma200)
    if args.command == "paper":
        print("\nProcessing one simulated paper-trading session...")
        paper_trade(df, history, asof, bull)
    elif args.command == "backtest":
        run_backtest(history, df, asof, days=max(60, args.days))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(130)
