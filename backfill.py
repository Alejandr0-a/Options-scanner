"""
Backfill Script - One-time enrichment of old signals.

Fills in missing data for signals that were captured before enrichment was added:
- market_cap (from Yahoo Finance)
- cap_category
- Corrects approximate outcome prices with actual historical closes
- Adds +1d and +3d fields to old signals

Run manually: python backfill.py
Or via GitHub Actions: workflow_dispatch
"""

import os
import json
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

SCRIPT_DIR = Path(__file__).parent
SIGNALS_FILE = SCRIPT_DIR / "tracked_signals.json"
REQUEST_DELAY = 0.4

# UW API (optional - for sector/earnings backfill)
API_TOKEN = os.environ.get("UW_API_TOKEN", "")
BASE_URL = "https://api.unusualwhales.com"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"} if API_TOKEN else {}


def get_stock_price(ticker):
    """Get current stock price from Yahoo."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        result = data.get("chart", {}).get("result", [])
        if result:
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            for c in reversed(closes):
                if c:
                    return round(c, 4)
    except Exception:
        pass
    return None


def get_historical_close(ticker, target_date_str):
    """Get closing price for a specific date."""
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
    except Exception:
        return None


def _get_yahoo_crumb():
    """Get Yahoo Finance crumb and cookie opener for authenticated requests."""
    try:
        import http.cookiejar
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req1 = urllib.request.Request("https://fc.yahoo.com", headers={"User-Agent": "Mozilla/5.0"})
        try:
            opener.open(req1, timeout=10)
        except Exception:
            pass
        req2 = urllib.request.Request(
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        crumb = opener.open(req2, timeout=10).read().decode()
        return crumb, opener
    except Exception:
        return None, None


_yahoo_crumb = None
_yahoo_opener = None


def get_yahoo_quote(ticker):
    """Get quote data from Yahoo Finance v7 API."""
    global _yahoo_crumb, _yahoo_opener
    if _yahoo_crumb is None:
        _yahoo_crumb, _yahoo_opener = _get_yahoo_crumb()
    if not _yahoo_crumb or not _yahoo_opener:
        return None
    try:
        url = f"https://query2.finance.yahoo.com/v7/finance/quote?symbols={ticker}&crumb={_yahoo_crumb}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(_yahoo_opener.open(req, timeout=15).read().decode())
        results = data.get("quoteResponse", {}).get("result", [])
        if results:
            return results[0]
    except Exception:
        _yahoo_crumb, _yahoo_opener = _get_yahoo_crumb()
    return None


def get_market_cap_yahoo(ticker):
    """Get market cap from Yahoo Finance."""
    quote = get_yahoo_quote(ticker)
    if quote:
        mc = quote.get("marketCap")
        if mc and float(mc) > 0:
            return float(mc)
    return None


def categorize_market_cap(market_cap):
    if market_cap is None:
        return None
    if market_cap >= 200_000_000_000:
        return "mega"
    elif market_cap >= 10_000_000_000:
        return "large"
    elif market_cap >= 2_000_000_000:
        return "mid"
    elif market_cap >= 300_000_000:
        return "small"
    else:
        return "micro"


def get_ticker_info_uw(ticker):
    """Fetch sector/earnings from UW API (if token available)."""
    if not API_TOKEN or not httpx:
        return None
    try:
        resp = httpx.get(
            f"{BASE_URL}/api/stock/{ticker}/info",
            headers=HEADERS,
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get("data", {})
    except Exception:
        pass
    return None


def backfill_market_cap(signals):
    """Backfill market_cap for signals missing it."""
    tickers_needing_mc = set()
    for s in signals:
        if not s.get("market_cap"):
            tickers_needing_mc.add(s["ticker"])

    if not tickers_needing_mc:
        print("  All signals already have market_cap.")
        return 0

    print(f"  Fetching market cap for {len(tickers_needing_mc)} tickers...")
    mc_cache = {}
    for i, ticker in enumerate(tickers_needing_mc):
        mc = get_market_cap_yahoo(ticker)
        mc_cache[ticker] = mc
        time.sleep(REQUEST_DELAY)
        if (i + 1) % 25 == 0:
            print(f"    ...{i+1}/{len(tickers_needing_mc)}")

    updated = 0
    for s in signals:
        if not s.get("market_cap"):
            mc = mc_cache.get(s["ticker"])
            if mc:
                s["market_cap"] = mc
                s["cap_category"] = categorize_market_cap(mc)
                updated += 1

    filled = sum(1 for t, mc in mc_cache.items() if mc)
    print(f"  Got market cap for {filled}/{len(tickers_needing_mc)} tickers. Updated {updated} signals.")
    return updated


def backfill_enrichment(signals):
    """Backfill sector/earnings for signals missing them (requires UW API token)."""
    if not API_TOKEN:
        print("  Skipping enrichment backfill (no UW_API_TOKEN).")
        return 0

    tickers_needing = set()
    for s in signals:
        if not s.get("sector"):
            tickers_needing.add(s["ticker"])

    if not tickers_needing:
        print("  All signals already have sector data.")
        return 0

    print(f"  Fetching enrichment for {len(tickers_needing)} tickers...")
    info_cache = {}
    for i, ticker in enumerate(tickers_needing):
        info = get_ticker_info_uw(ticker)
        info_cache[ticker] = info
        time.sleep(REQUEST_DELAY)
        if (i + 1) % 25 == 0:
            print(f"    ...{i+1}/{len(tickers_needing)}")

    updated = 0
    for s in signals:
        if not s.get("sector"):
            info = info_cache.get(s["ticker"])
            if info:
                s["sector"] = info.get("sector") or s.get("sector")
                s["industry"] = info.get("industry") or s.get("industry")
                s["next_earnings"] = info.get("next_earnings_date") or s.get("next_earnings")
                if s.get("next_earnings"):
                    try:
                        ed = datetime.strptime(s["next_earnings"], "%Y-%m-%d")
                        s["days_to_earnings"] = (ed - datetime.now()).days
                    except Exception:
                        pass
                updated += 1

    print(f"  Updated enrichment for {updated} signals.")
    return updated


def backfill_outcome_prices(signals):
    """Correct outcome prices to use actual historical closes."""
    today = datetime.now().date()
    checkpoints = [
        (1, "price_1d", "return_1d"),
        (3, "price_3d", "return_3d"),
        (5, "price_5d", "return_5d"),
        (10, "price_10d", "return_10d"),
        (20, "price_20d", "return_20d"),
        (30, "price_30d", "return_30d"),
    ]

    # Find signals that need new checkpoints (+1d, +3d) or corrections
    needs_work = []
    for s in signals:
        scan_date = datetime.strptime(s["scan_date"], "%Y-%m-%d").date()
        days_elapsed = (today - scan_date).days
        entry = s.get("entry_price", 0)
        if not entry or entry <= 0:
            continue

        for days, price_key, return_key in checkpoints:
            # Initialize missing keys
            if price_key not in s:
                s[price_key] = None
            if return_key not in s:
                s[return_key] = None

            if days_elapsed >= days and s[price_key] is None:
                needs_work.append((s, days, price_key, return_key))

    if not needs_work:
        print("  All outcome checkpoints are filled.")
        return 0

    print(f"  Found {len(needs_work)} missing outcome checkpoints to fill...")

    price_cache = {}
    updated_signals = set()
    errors = 0

    for i, (s, days, price_key, return_key) in enumerate(needs_work):
        scan_date = datetime.strptime(s["scan_date"], "%Y-%m-%d")
        target_date = scan_date + timedelta(days=days)
        target_str = target_date.strftime("%Y-%m-%d")
        ticker = s["ticker"]
        cache_key = f"{ticker}_{target_str}"

        if cache_key not in price_cache:
            days_ago = (today - target_date.date()).days
            if days_ago <= 1:
                price = get_stock_price(ticker)
            else:
                price = get_historical_close(ticker, target_str)
            time.sleep(REQUEST_DELAY)
            price_cache[cache_key] = price

        price = price_cache[cache_key]
        if price:
            entry = s["entry_price"]
            s[price_key] = round(price, 4)
            s[return_key] = round((price - entry) / entry * 100, 2)
            updated_signals.add(id(s))
        else:
            errors += 1

        if (i + 1) % 50 == 0:
            print(f"    ...{i+1}/{len(needs_work)} ({errors} errors)")

    # Update outcomes for signals with 30d data
    for s in signals:
        if s.get("price_30d") is not None and s.get("outcome") is None:
            s["outcome"] = "win" if s["return_30d"] > 0 else "loss"

    print(f"  Filled {len(needs_work) - errors} checkpoints across {len(updated_signals)} signals ({errors} errors).")
    return len(updated_signals)


def main():
    print("=" * 60)
    print("SIGNAL BACKFILL")
    print("=" * 60)

    if not SIGNALS_FILE.exists():
        print("No signal history found.")
        return

    with open(SIGNALS_FILE) as f:
        data = json.load(f)

    signals = data["signals"]
    print(f"\nLoaded {len(signals)} signals.\n")

    # 1. Market cap backfill
    print("[1/3] Market Cap Backfill")
    mc_count = backfill_market_cap(signals)

    # 2. Sector/earnings backfill
    print("\n[2/3] Enrichment Backfill (sector, earnings)")
    enrich_count = backfill_enrichment(signals)

    # 3. Outcome prices
    print("\n[3/3] Outcome Price Backfill (historical closes)")
    outcome_count = backfill_outcome_prices(signals)

    # Save
    total_updates = mc_count + enrich_count + outcome_count
    if total_updates > 0:
        with open(SIGNALS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"\nSaved {total_updates} total updates.")
    else:
        print("\nNo updates needed.")

    print("\nDone.")


if __name__ == "__main__":
    main()
