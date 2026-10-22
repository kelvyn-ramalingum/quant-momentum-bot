# Quant Momentum Bot — local research + Alpaca paper trading

This beginner-friendly project scans US stocks and ETFs for price momentum and possible breakout setups. It includes two separate simulation paths:

1. `quant_momentum_bot.py paper` is the original local simulator with its own virtual $10,000 ledger.
2. `alpaca_paper.py` connects to an **Alpaca paper-only account** for broker-simulated orders, balances, holdings, open orders and latest IEX trades. This connector hard-codes `paper=True`; it does not support live order routing.

For the Alpaca experience, use the commands in the Alpaca section below. Do not run both paper simulators expecting them to share a portfolio; they are separate ledgers.

## Strategy in brief

- Discovers candidate shares from Yahoo Finance screens plus a fallback watchlist. Screens may fail or change, and cannot discover every listed company.
- Downloads adjusted daily OHLCV history and ranks 20-, 60- and 120-session momentum, strength versus SPY, volume expansion, closeness to a prior 20-session high and ATR-based volatility.
- Flags possible breakouts when a stock is near its prior 20-day high on elevated volume, while applying price, liquidity, trend and volatility filters.
- Uses SPY above/below its 200-day moving average as a simple risk-on/risk-off filter.
- Keeps signal generation separate from the broker connector. The scanner writes `bot_data/latest_scan.csv` plus `bot_data/latest_scan_meta.json`.

This is a learning/research project, not a profitable or institutional-grade system. It does not currently ingest news, SEC filings, fundamentals, analyst estimates or full-market order-book data.

## Install (Windows)

1. Install Python 3.11 or newer from <https://www.python.org/downloads/> and enable the option to add Python to PATH.
2. Extract the project and open PowerShell or Command Prompt in the `quant_momentum_bot` folder.
3. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks activation, use Command Prompt and run `.venv\Scripts\activate.bat` instead.

4. Install dependencies:

   ```powershell
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

5. Verify the offline logic. These tests do not access the network or submit orders:

   ```powershell
   python quant_momentum_bot.py self-test
   python alpaca_paper.py self-test
   ```

## Connect Alpaca paper trading (no live broker orders)

Alpaca offers a Paper Only account for simulated trading. Create/use a paper-only account and follow Alpaca's current account terms. Do not provide false information or attempt to bypass account restrictions. Official instructions: <https://docs.alpaca.markets/us/docs/paper-trading>.

1. Open Alpaca's paper trading dashboard: <https://app.alpaca.markets/>
2. Select the **Paper Trading / Paper Only** environment, then generate its paper API key and secret. Paper keys are different from live keys.
3. Copy `.env.example` to a new file named `.env` in this project folder. In PowerShell:

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

4. Paste the paper key and secret into `.env`:

   ```text
   APCA_API_KEY_ID=YOUR_PAPER_KEY_ID
   APCA_API_SECRET_KEY=YOUR_PAPER_SECRET_KEY
   APCA_API_BASE_URL=https://paper-api.alpaca.markets
   ```

   Keep the secret private. Never paste it into a chat, screenshot, GitHub repository or shared file. `.env` is excluded by `.gitignore`. Do not put live account keys in this project.

5. Test that the API connection works and view your account:

   ```powershell
   python alpaca_paper.py check
   ```

   This connects to Alpaca's paper endpoint and prints account information; it does not place orders.

## Daily workflow

Run after the US regular market has closed if you want signals from the latest completed daily candle.

### 1. Preview the current candidates and proposed orders (safe, no orders sent)

```powershell
python alpaca_paper.py plan
```

This refreshes the scanner, includes current Alpaca-held positions in the scan, prints a potential buy/sell plan and saves it to `bot_data/alpaca_last_plan.csv`. Review it before doing anything else.

### 2. Submit the plan to Alpaca's paper account

```powershell
python alpaca_paper.py plan --submit
```

The `--submit` flag is required to send simulated orders. It sends market DAY orders into Alpaca's paper-trading system only. If run after market close, an order may remain pending until the next session; not every order is guaranteed to fill. Review statuses in the next command and in the Alpaca dashboard.

The current risk controls are long-only, cash-sized (not margin/buying-power sized), maximum five held positions, 0.5% equity risk budget per trade, 10% maximum position weight and a $2,000 maximum estimated notional per new entry. Entries are ranked by momentum with breakout-watch candidates prioritised. Exits are triggered when the daily close is at/below an ATR-based initial/trailing threshold or below the 20-day average. These controls reduce some risks; they do not prevent losses or guarantee fills.

### 3. See your account, holdings and orders

```powershell
python alpaca_paper.py status
```

You can also use Alpaca's web dashboard: <https://app.alpaca.markets/>. Account equity, virtual cash, holdings, order status and simulated trading history are tracked there.

### 4. View latest available prices for specific symbols

```powershell
python alpaca_paper.py prices AAPL NVDA SOFI SPY
```

The connector prints the latest available IEX trade and its timestamp in UK time. Paper-only accounts are entitled to IEX market data; IEX represents one exchange, not a consolidated view of every US venue. Some symbols may have no recent IEX trade in the response. The stock discovery scan itself continues to use Yahoo Finance/yfinance data, so the displayed scan close and a latest IEX trade can differ.

### Other commands

```text
python quant_momentum_bot.py scan          # scanner only, no broker connection
python quant_momentum_bot.py backtest      # exploratory historical research
python quant_momentum_bot.py status        # local simulator status, not Alpaca
python alpaca_paper.py check               # test paper credentials/account
python alpaca_paper.py plan                # refresh and preview, no orders
python alpaca_paper.py plan --submit       # explicitly submit paper orders
python alpaca_paper.py status              # Alpaca paper portfolio and orders
python alpaca_paper.py prices AAPL NVDA    # latest available IEX trade prices
```

## Data, fills and limitations

- **Scanner data:** `yfinance` is an open-source community package accessing Yahoo Finance's public APIs. Data can be delayed, rate-limited, incomplete or changed without notice, and Yahoo data is intended for personal use: <https://ranaroussi.github.io/yfinance/>.
- **Alpaca market data:** the Paper Only entitlement uses IEX data. It is not equivalent to consolidated all-exchange quotes. The connector requests latest IEX trades for its `prices` command.
- **Order simulation:** Alpaca simulates paper fills rather than routing orders to a live exchange. Paper trading may not reproduce market impact, latency, queue position, price improvement, regulatory charges or dividends. See <https://docs.alpaca.markets/us/docs/paper-trading>.
- **Historical testing:** the basic backtest reuses the currently discovered stock universe for historical dates, which creates survivorship and universe-selection bias. Its results are not reliable evidence of a tradable edge.
- **Volatile/small-cap shares:** prices can gap, trading can halt, spreads can widen, and exits may fill at worse prices than the scan close. Stops are rules, not guaranteed loss limits.
- **No continuous background loop:** the project is run manually once per session. It does not run by itself when the computer is off.

## Files created

- `bot_data/latest_scan.csv`: latest scanner output.
- `bot_data/latest_scan_meta.json`: date and SPY regime used for the scan.
- `bot_data/alpaca_last_plan.csv`: last proposed order plan.
- `bot_data/alpaca_managed_state.json`: local metadata used to track high-water marks and stop levels for positions managed by this strategy.
- `bot_data/paper_state.json` / `paper_trades.csv`: local simulator's independent ledger and trade log.
- `bot_data/backtest_equity.csv`: exploratory local backtest equity series.

Back up `bot_data` if you want to preserve local records. Never upload `.env`.
