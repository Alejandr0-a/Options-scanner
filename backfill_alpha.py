"""
Backfill SPY alpha data for all existing signals.
Fetches SPY prices for all unique dates, then computes alpha for every signal
that already has return data.
"""
import json
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SIGNALS_FILE = Path(__file__).parent / "signal_database.json"

def get_historical_close(ticker, target_date_str):
    """Get closing price for a specific date using Yahoo Finance."""
    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d")
        start = target - timedelta(days=3)
        end = target + timedelta(days=3)
        period1 = int(start.timestamp())
        period2 = int(end.timestamp())

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?period1={period1}&period2={period2}&interval=1d"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        timestamps = result[0].get("timestamp", [])
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])

        if not timestamps or not closes:
            return None

        target_ts = int(target.timestamp())
        best_close = None
        best_diff = float("inf")

        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            diff = abs(ts - target_ts)
            if ts <= target_ts + 86400 and diff < best_diff:
                best_diff = diff
                best_close = close

        return round(best_close, 4) if best_close else None
    except Exception as e:
        print(f"  Error fetching {ticker} for {target_date_str}: {e}")
        return None


def get_spy_bulk(dates):
    """Fetch SPY closes for a date range in one API call."""
    if not dates:
        return {}

    sorted_dates = sorted(dates)
    start = datetime.strptime(sorted_dates[0], "%Y-%m-%d") - timedelta(days=3)
    end = datetime.strptime(sorted_dates[-1], "%Y-%m-%d") + timedelta(days=3)
    period1 = int(start.timestamp())
    period2 = int(end.timestamp())

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/SPY"
        f"?period1={period1}&period2={period2}&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"Error fetching bulk SPY data: {e}")
        return {}

    result = data.get("chart", {}).get("result", [])
    if not result:
        return {}

    timestamps = result[0].get("timestamp", [])
    closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])

    if not timestamps or not closes:
        return {}

    # Build date -> close mapping
    ts_closes = {}
    for ts, close in zip(timestamps, closes):
        if close is not None:
            dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            ts_closes[dt] = round(close, 4)

    # Match each requested date to closest available close
    spy_prices = {}
    for date_str in dates:
        target = datetime.strptime(date_str, "%Y-%m-%d")
        best_close = None
        best_diff = float("inf")
        for dt_str, close in ts_closes.items():
            dt = datetime.strptime(dt_str, "%Y-%m-%d")
            diff = abs((dt - target).days)
            if dt <= target + timedelta(days=1) and diff < best_diff:
                best_diff = diff
                best_close = close
        if best_close:
            spy_prices[date_str] = best_close

    return spy_prices


def backfill():
    print("Loading signal database...")
    with open(SIGNALS_FILE) as f:
        data = json.load(f)

    signals = data["signals"]
    print(f"Total signals: {len(signals)}")

    # Collect all unique dates we need SPY prices for
    all_dates = set()
    checkpoints = [1, 3, 5, 10, 20, 30]

    for signal in signals:
        scan_date = signal.get("scan_date")
        if scan_date:
            all_dates.add(scan_date)
            base = datetime.strptime(scan_date, "%Y-%m-%d")
            for days in checkpoints:
                if signal.get(f"return_{days}d") is not None:
                    target = (base + timedelta(days=days)).strftime("%Y-%m-%d")
                    all_dates.add(target)

    print(f"Unique dates needing SPY prices: {len(all_dates)}")
    print(f"Date range: {min(all_dates)} to {max(all_dates)}")

    # Fetch SPY prices in bulk (one API call covers ~2 months)
    # Yahoo Finance chart API can handle large ranges
    print("\nFetching SPY prices in bulk...")
    spy_prices = get_spy_bulk(all_dates)
    print(f"Got SPY prices for {len(spy_prices)} dates")

    # Fill any gaps with individual fetches
    missing = all_dates - set(spy_prices.keys())
    if missing:
        print(f"Fetching {len(missing)} missing dates individually...")
        for i, date_str in enumerate(sorted(missing)):
            price = get_historical_close("SPY", date_str)
            if price:
                spy_prices[date_str] = price
            time.sleep(0.3)
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(missing)} done")

    print(f"Final SPY price coverage: {len(spy_prices)} dates")

    # Now backfill all signals
    updated = 0
    alpha_filled = 0

    for signal in signals:
        scan_date = signal.get("scan_date")
        if not scan_date:
            continue

        entry = signal.get("entry_price")
        if not entry or entry <= 0:
            continue

        changed = False

        # Set SPY entry price
        if signal.get("spy_entry_price") is None:
            spy_entry = spy_prices.get(scan_date)
            if spy_entry:
                signal["spy_entry_price"] = spy_entry
                changed = True

        spy_entry = signal.get("spy_entry_price")
        if not spy_entry:
            continue

        base = datetime.strptime(scan_date, "%Y-%m-%d")

        for days in checkpoints:
            spy_ret_key = f"spy_return_{days}d"
            alpha_key = f"alpha_{days}d"
            return_key = f"return_{days}d"

            # Initialize missing keys
            if spy_ret_key not in signal:
                signal[spy_ret_key] = None
            if alpha_key not in signal:
                signal[alpha_key] = None

            sig_ret = signal.get(return_key)
            if sig_ret is None:
                continue

            # Calculate SPY return
            if signal.get(spy_ret_key) is None:
                target = (base + timedelta(days=days)).strftime("%Y-%m-%d")
                spy_price = spy_prices.get(target)
                if spy_price:
                    spy_ret = round((spy_price - spy_entry) / spy_entry * 100, 2)
                    signal[spy_ret_key] = spy_ret
                    signal[alpha_key] = round(sig_ret - spy_ret, 2)
                    alpha_filled += 1
                    changed = True

            # Calculate alpha if SPY return exists but alpha doesn't
            elif signal.get(alpha_key) is None and signal.get(spy_ret_key) is not None:
                signal[alpha_key] = round(sig_ret - signal[spy_ret_key], 2)
                alpha_filled += 1
                changed = True

        if changed:
            updated += 1

    print(f"\nUpdated {updated} signals")
    print(f"Alpha values filled: {alpha_filled}")

    # Quick stats
    for days in [5, 10, 30]:
        alphas = [s[f"alpha_{days}d"] for s in signals if s.get(f"alpha_{days}d") is not None]
        if alphas:
            wins = sum(1 for a in alphas if a > 0)
            print(f"\nAlpha {days}d: {len(alphas)} signals")
            print(f"  Beat SPY: {wins/len(alphas)*100:.1f}%")
            print(f"  Avg alpha: {sum(alphas)/len(alphas):+.2f}%")
            print(f"  Best: {max(alphas):+.1f}% | Worst: {min(alphas):+.1f}%")

    # Save
    print("\nSaving...")
    with open(SIGNALS_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    print("Done!")


if __name__ == "__main__":
    backfill()
