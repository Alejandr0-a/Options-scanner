#!/usr/bin/env python3
"""
Fast outcome backfill using yfinance batch downloads.
Replaces the slow one-at-a-time approach in backfill.py.

Downloads all needed ticker prices in batches, then fills in
missing price_Xd and return_Xd checkpoints for all signals.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import yfinance as yf
import pandas as pd

SCRIPT_DIR = Path(__file__).parent
SIGNALS_FILE = SCRIPT_DIR / "signal_database.json"
CHECKPOINTS = [
    (1, "price_1d", "return_1d"),
    (3, "price_3d", "return_3d"),
    (5, "price_5d", "return_5d"),
    (10, "price_10d", "return_10d"),
    (20, "price_20d", "return_20d"),
    (30, "price_30d", "return_30d"),
]


def find_closest_close(df, target_date, max_offset_days=5):
    """Find closing price on or just before target_date."""
    if df is None or df.empty:
        return None
    # Look for exact date or nearest prior trading day
    for offset in range(max_offset_days + 1):
        check_date = target_date - timedelta(days=offset)
        check_str = check_date.strftime('%Y-%m-%d')
        if check_str in df.index:
            val = df.loc[check_str, 'Close']
            if pd.notna(val):
                return round(float(val), 4)
    return None


def main():
    print("=" * 60)
    print("FAST OUTCOME BACKFILL (yfinance)")
    print("=" * 60)

    with open(SIGNALS_FILE) as f:
        data = json.load(f)
    signals = data["signals"]
    print(f"Loaded {len(signals)} signals.")

    today = datetime.now().date()

    # 1. Find all signals needing backfill and collect ticker set
    tickers_needed = set()
    signals_to_fill = []
    for s in signals:
        entry = s.get("entry_price", 0)
        if not entry or entry <= 0:
            continue
        scan_date = datetime.strptime(s["scan_date"], "%Y-%m-%d").date()
        days_elapsed = (today - scan_date).days
        needs_fill = False
        for days, price_key, return_key in CHECKPOINTS:
            if price_key not in s:
                s[price_key] = None
            if return_key not in s:
                s[return_key] = None
            if days_elapsed >= days and s[price_key] is None:
                needs_fill = True
        if needs_fill:
            tickers_needed.add(s["ticker"])
            signals_to_fill.append(s)

    if not signals_to_fill:
        print("All checkpoints already filled. Nothing to do.")
        return

    print(f"Signals needing backfill: {len(signals_to_fill)}")
    print(f"Unique tickers to fetch: {len(tickers_needed)}")

    # 2. Determine date range for price fetch
    all_scan_dates = [datetime.strptime(s["scan_date"], "%Y-%m-%d").date() for s in signals_to_fill]
    earliest = min(all_scan_dates) - timedelta(days=5)  # buffer before first signal
    latest = max(all_scan_dates) + timedelta(days=35)    # buffer after last signal for 30d checkpoint
    if latest > today:
        latest = today

    fetch_start = earliest.strftime('%Y-%m-%d')
    fetch_end = (latest + timedelta(days=1)).strftime('%Y-%m-%d')
    print(f"Fetching prices from {fetch_start} to {fetch_end}")

    # 3. Batch download prices
    ticker_list = sorted(tickers_needed)
    price_data = {}  # ticker -> DataFrame with 'Close' column, indexed by date string
    batch_size = 50
    failed_tickers = []

    for i in range(0, len(ticker_list), batch_size):
        batch = ticker_list[i:i + batch_size]
        batch_str = ' '.join(batch)
        try:
            df = yf.download(batch_str, start=fetch_start, end=fetch_end,
                             group_by='ticker', progress=False, threads=True)
            if len(batch) == 1:
                ticker = batch[0]
                if not df.empty and 'Close' in df.columns:
                    tdf = df[['Close']].dropna().copy()
                    tdf.index = tdf.index.strftime('%Y-%m-%d')
                    price_data[ticker] = tdf
            else:
                for ticker in batch:
                    try:
                        tdf = df[ticker][['Close']].dropna().copy()
                        if not tdf.empty:
                            tdf.index = tdf.index.strftime('%Y-%m-%d')
                            price_data[ticker] = tdf
                    except (KeyError, TypeError):
                        failed_tickers.append(ticker)
        except Exception as e:
            print(f"  Batch error: {e}")
            failed_tickers.extend(batch)

        fetched = min(i + batch_size, len(ticker_list))
        if fetched % 200 == 0 or fetched == len(ticker_list):
            print(f"  Downloaded {fetched}/{len(ticker_list)} tickers ({len(price_data)} successful)")

    print(f"Price data for {len(price_data)}/{len(tickers_needed)} tickers "
          f"({len(failed_tickers)} failed)")

    # 4. Fill in checkpoints
    filled = 0
    errors = 0
    for s in signals_to_fill:
        ticker = s["ticker"]
        if ticker not in price_data:
            continue

        df = price_data[ticker]
        scan_date = datetime.strptime(s["scan_date"], "%Y-%m-%d").date()
        days_elapsed = (today - scan_date).days
        entry = s["entry_price"]

        for days, price_key, return_key in CHECKPOINTS:
            if days_elapsed >= days and s[price_key] is None:
                target_date = scan_date + timedelta(days=days)
                price = find_closest_close(df, target_date)
                if price:
                    s[price_key] = price
                    s[return_key] = round((price - entry) / entry * 100, 2)
                    filled += 1
                else:
                    errors += 1

    # 5. Update outcomes
    outcomes_set = 0
    for s in signals:
        if s.get("price_30d") is not None and s.get("outcome") is None:
            s["outcome"] = "win" if s.get("return_30d", 0) > 0 else "loss"
            outcomes_set += 1

    # 6. Also backfill peak_return and drawdown
    peaks_set = 0
    for s in signals:
        if s.get("peak_return") is not None:
            continue
        returns = []
        for days, _, return_key in CHECKPOINTS:
            r = s.get(return_key)
            if r is not None:
                returns.append((f"{days}d", r))
        if returns:
            best = max(returns, key=lambda x: x[1])
            s["peak_return"] = best[1]
            s["peak_checkpoint"] = best[0]
            # Drawdown from peak to 30d (or latest)
            latest_return = returns[-1][1]
            s["drawdown_from_peak"] = round(latest_return - best[1], 2)
            peaks_set += 1

    print(f"\nFilled {filled} checkpoints ({errors} lookup failures)")
    print(f"Outcomes set: {outcomes_set}")
    print(f"Peak/drawdown computed: {peaks_set}")

    # 7. Save
    with open(SIGNALS_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"\nSaved to {SIGNALS_FILE}")

    # 8. Summary
    has_1d = sum(1 for s in signals if s.get('return_1d') is not None)
    has_5d = sum(1 for s in signals if s.get('return_5d') is not None)
    has_30d = sum(1 for s in signals if s.get('return_30d') is not None)
    print(f"\nCoverage after backfill:")
    print(f"  1d returns: {has_1d}/{len(signals)}")
    print(f"  5d returns: {has_5d}/{len(signals)}")
    print(f"  30d returns: {has_30d}/{len(signals)}")


if __name__ == "__main__":
    main()
