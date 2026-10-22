Quant Momentum Bot

A Python research project that turns a simple momentum idea into a complete, testable pipeline: scan, size, plan, and (optionally) execute against a paper-trading account.

The signal is deliberately basic. What I wanted to study was everything around it: how risk is sized, how regimes gate entries, and how strategy logic is kept apart from order execution.

What it does

The bot looks for stocks with strong price momentum, screens them for liquidity and volatility, and produces a proposed trade plan. It never trades blind: by default it only prints the plan for review. Orders go to Alpaca's paper-trading environment, so the full execution path gets exercised without real capital at risk.

Design decisions

Regime gate. New positions are only allowed when SPY is trading above its 200-day moving average. In a weak broad market the bot simply stands aside.

Volatility-aware sizing. Position size comes from account equity and the distance to an ATR-based stop, so a choppier stock automatically receives a smaller allocation. Per-position weight limits and available cash then cap the result.

Rule-based position management. Existing holdings are checked against ATR stop levels and the 20-day moving average on every run.

Signal and execution kept separate. Strategy output is a plan, not an action. Nothing is sent to the broker unless --submit is passed explicitly.

Persistent state. Position information is saved locally between runs so management rules have continuity.

Stack
Python, pandas, NumPy
Alpaca Trading API and market-data SDK
Yahoo Finance daily bars
Git / GitHub
Pipeline
Compute momentum, liquidity and volatility metrics across the stock universe
Check the SPY regime
Rank eligible candidates, cross-checking current holdings and open orders
Size each proposed trade against the configured risk limits and cash on hand
Print the trade plan for review
Optionally submit the plan to the paper account
Inspect simulated positions and account status
Getting started

Create a virtual environment:

bash
python -m venv .venv

Activate it and install dependencies (Windows Command Prompt):

cmd
.venv\Scripts\activate
python -m pip install -r requirements.txt

Copy .env.example to .env and add paper-trading credentials only. Never commit .env or share your keys.

Run the offline self-tests:

bash
python quant_momentum_bot.py self-test
python alpaca_paper.py self-test

Verify the broker connection:

bash
python alpaca_paper.py check

Preview a trade plan (no orders sent):

bash
python alpaca_paper.py plan

View the simulated account:

bash
python alpaca_paper.py status

Submit a plan you have reviewed:

bash
python alpaca_paper.py plan --submit

This sends simulated orders, so use it only against the paper environment and only after reading the plan.

Known limitations

This is an experiment, not a validated strategy, and I'd rather be upfront about where it falls short:

Fills vs planned prices. Market orders can execute away from the price used to size the trade, so the order-notional limit is based on the planning price and is not a hard cap on what actually fills.
Stops are not broker-side. Stop and trailing-stop conditions are evaluated only when the plan runs. They are not live protective orders held at the broker.
No validation yet. There is no backtest, transaction-cost model, slippage analysis or out-of-sample testing, so no conclusions about performance can be drawn.
Paper results prove little. Simulated trading says nothing reliable about future profitability.
Roadmap

The next step is a proper backtesting layer with realistic cost and slippage assumptions, followed by out-of-sample checks. Suggestions on validation methodology are welcome via issues or pull requests.

Purpose and disclaimer

I built this to learn how a market rule becomes reproducible software, with the emphasis on risk controls, data handling and clean separation between signals and execution.

For educational and research use only. It is configured for paper trading, is not financial advice, and makes no claim of profitability.