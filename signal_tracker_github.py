"""
Signal Tracker - GitHub Actions Version
Runs every 30 minutes during market hours (9:30 AM - 5:00 PM ET)
Commits results back to the repository.
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
    print("Run: pip install httpx")
    exit(1)

# Configuration - Token from GitHub Secrets
API_TOKEN = os.environ.get("UW_API_TOKEN")
if not API_TOKEN:
    print("ERROR: UW_API_TOKEN environment variable not set")
    exit(1)

BASE_URL = "https://api.unusualwhales.com"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}

OUTPUT_DIR = Path(__file__).parent
SIGNALS_FILE = OUTPUT_DIR / "tracked_signals.json"

# Quality thresholds
MIN_SCORE = 70
MIN_ASK_PCT = 80
MIN_VOI_RATIO = 5.0

# Excluded tickers
EXCLUDED = {
    "SPY", "QQQ", "IWM", "DIA", "VIX", "VXX", "UVXY", "SQQQ", "TQQQ",
    "GLD", "SLV", "USO", "TLT", "XLF", "XLE", "ARKK", "COIN", "MARA", "MSTR",
}


def load_signals():
    """Load existing tracked signals"""
    if SIGNALS_FILE.exists():
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    return {"signals": [], "last_scan": None}


def save_signals(data):
    """Save signals to file"""
    with open(SIGNALS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_stock_price(ticker):
    """Get current stock price from Yahoo"""
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
                    return c
    except:
        pass
    return None


def fetch_flow_alerts():
    """Fetch current flow alerts from Unusual Whales"""
    try:
        resp = httpx.get(
            f"{BASE_URL}/api/option-trades/flow-alerts",
            headers=HEADERS,
            params={"limit": 500},
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception as e:
        print(f"Error fetching alerts: {e}")
    return []


def calculate_score(alert):
    """Calculate quality score for an alert"""
    score = 0
    flags = []

    # 1. Aggression (% at ask)
    total_ask_prem = float(alert.get("total_ask_side_prem", 0) or 0)
    total_bid_prem = float(alert.get("total_bid_side_prem", 0) or 0)
    total_prem = total_ask_prem + total_bid_prem
    ask_pct = (total_ask_prem / total_prem * 100) if total_prem > 0 else 0

    if ask_pct >= 95:
        score += 35
        flags.append(f"Very aggressive ({ask_pct:.0f}%)")
    elif ask_pct >= 85:
        score += 25
        flags.append(f"Aggressive ({ask_pct:.0f}%)")

    # 2. V/OI ratio
    voi_str = alert.get("volume_oi_ratio", "0")
    voi = float(voi_str) if voi_str else 0

    if voi >= 20:
        score += 35
        flags.append(f"Extreme V/OI ({voi:.1f}x)")
    elif voi >= 10:
        score += 30
        flags.append(f"Very high V/OI ({voi:.1f}x)")
    elif voi >= 5:
        score += 20
        flags.append(f"High V/OI ({voi:.1f}x)")

    # 3. DTE
    try:
        expiry = datetime.strptime(alert.get("expiry", "2099-12-31"), "%Y-%m-%d")
        dte = (expiry - datetime.now()).days
    except:
        dte = 999

    if dte <= 7:
        score += 30
        flags.append(f"Very short DTE ({dte}d)")
    elif dte <= 14:
        score += 20
        flags.append(f"Short DTE ({dte}d)")
    elif dte <= 21:
        score += 10
        flags.append(f"Near-term ({dte}d)")

    # 4. OTM %
    strike = float(alert.get("strike", 0) or 0)
    spot = float(alert.get("underlying_price", 0) or 0)
    opt_type = alert.get("type", "").lower()

    if spot > 0 and strike > 0:
        if opt_type == "call":
            otm_pct = ((strike - spot) / spot * 100) if strike > spot else 0
        else:
            otm_pct = ((spot - strike) / spot * 100) if strike < spot else 0

        if otm_pct >= 15:
            score += 25
            flags.append(f"Deep OTM ({otm_pct:.1f}%)")
        elif otm_pct >= 10:
            score += 20
            flags.append(f"OTM ({otm_pct:.1f}%)")
        elif otm_pct >= 5:
            score += 10
            flags.append(f"Slightly OTM ({otm_pct:.1f}%)")

    # 5. Sweep
    if alert.get("has_sweep"):
        score += 15
        flags.append("Sweep order")

    # 6. Fill count
    fills = int(alert.get("trade_count", 999) or 999)
    if fills <= 5:
        score += 20
        flags.append(f"Block trade ({fills} fills)")
    elif fills <= 10:
        score += 10
        flags.append(f"Few fills ({fills})")

    # 7. Premium size
    premium = float(alert.get("total_premium", 0) or 0)
    if premium >= 500000:
        score += 15
        flags.append(f"Large bet (${premium/1000:.0f}K)")
    elif premium >= 200000:
        score += 10
        flags.append(f"Sizable bet (${premium/1000:.0f}K)")

    return score, flags


def scan_and_save():
    """Scan for new high-quality signals and save them"""
    print("=" * 60)
    print(f"SIGNAL TRACKER - {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    data = load_signals()
    existing_ids = {s.get("alert_id") for s in data["signals"]}

    print("\nFetching flow alerts...")
    alerts = fetch_flow_alerts()
    print(f"Got {len(alerts)} alerts")

    new_signals = []

    for alert in alerts:
        ticker = alert.get("ticker", "")
        opt_type = alert.get("type", "").lower()
        alert_id = alert.get("id", "")

        if ticker in EXCLUDED:
            continue
        if opt_type != "call":
            continue
        if alert_id in existing_ids:
            continue

        score, flags = calculate_score(alert)

        if score < MIN_SCORE:
            continue
        if len(flags) < 2:
            continue

        spot = float(alert.get("underlying_price", 0) or 0)
        if spot <= 0:
            spot = get_stock_price(ticker)

        if not spot:
            continue

        total_ask_prem = float(alert.get("total_ask_side_prem", 0) or 0)
        total_bid_prem = float(alert.get("total_bid_side_prem", 0) or 0)
        total_prem = total_ask_prem + total_bid_prem
        ask_pct_val = (total_ask_prem / total_prem * 100) if total_prem > 0 else 0
        voi_val = float(alert.get("volume_oi_ratio", 0) or 0)

        signal = {
            "alert_id": alert_id,
            "scan_date": datetime.now().strftime("%Y-%m-%d"),
            "scan_time": datetime.now().strftime("%H:%M"),
            "ticker": ticker,
            "strike": float(alert.get("strike", 0) or 0),
            "expiry": alert.get("expiry", ""),
            "premium": float(alert.get("total_premium", 0) or 0),
            "score": score,
            "flags": flags,
            "entry_price": spot,
            "ask_pct": ask_pct_val,
            "voi_ratio": voi_val,
            "price_5d": None,
            "price_10d": None,
            "price_20d": None,
            "price_30d": None,
            "return_5d": None,
            "return_10d": None,
            "return_20d": None,
            "return_30d": None,
            "outcome": None,
        }

        new_signals.append(signal)
        existing_ids.add(alert_id)

    data["signals"].extend(new_signals)
    data["last_scan"] = datetime.now().isoformat()

    print(f"\nNew high-quality signals found: {len(new_signals)}")

    if new_signals:
        print("\n--- NEW SIGNALS ---")
        for s in sorted(new_signals, key=lambda x: x["score"], reverse=True)[:10]:
            print(f"  {s['ticker']:6} Score:{s['score']:3} | ${s['premium']/1000:.0f}K | "
                  f"Ask:{s['ask_pct']:.0f}% | V/OI:{s['voi_ratio']:.1f}x | {s['flags'][0]}")

    # Update existing signals with price data
    print("\nUpdating price data for existing signals...")
    update_count = 0

    for signal in data["signals"]:
        scan_date = datetime.strptime(signal["scan_date"], "%Y-%m-%d")
        days_elapsed = (datetime.now() - scan_date).days
        ticker = signal["ticker"]
        entry = signal["entry_price"]

        if not entry or entry <= 0:
            continue

        needs_update = False

        if days_elapsed >= 5 and signal["price_5d"] is None:
            needs_update = True
        if days_elapsed >= 10 and signal["price_10d"] is None:
            needs_update = True
        if days_elapsed >= 20 and signal["price_20d"] is None:
            needs_update = True
        if days_elapsed >= 30 and signal["price_30d"] is None:
            needs_update = True

        if needs_update:
            current_price = get_stock_price(ticker)
            if current_price:
                if days_elapsed >= 5 and signal["price_5d"] is None:
                    signal["price_5d"] = current_price
                    signal["return_5d"] = (current_price - entry) / entry * 100
                if days_elapsed >= 10 and signal["price_10d"] is None:
                    signal["price_10d"] = current_price
                    signal["return_10d"] = (current_price - entry) / entry * 100
                if days_elapsed >= 20 and signal["price_20d"] is None:
                    signal["price_20d"] = current_price
                    signal["return_20d"] = (current_price - entry) / entry * 100
                if days_elapsed >= 30 and signal["price_30d"] is None:
                    signal["price_30d"] = current_price
                    signal["return_30d"] = (current_price - entry) / entry * 100
                    signal["outcome"] = "win" if signal["return_30d"] > 0 else "loss"

                update_count += 1
                time.sleep(0.2)

    print(f"Updated {update_count} signals with price data")

    save_signals(data)

    # Summary stats
    print("\n" + "=" * 60)
    print("PERFORMANCE SUMMARY")
    print("=" * 60)

    completed = [s for s in data["signals"] if s.get("return_30d") is not None]

    if completed:
        wins = sum(1 for s in completed if s["return_30d"] > 0)
        big_wins = sum(1 for s in completed if s["return_30d"] >= 5)
        returns = [s["return_30d"] for s in completed]

        print(f"\nCompleted signals (30+ days): {len(completed)}")
        print(f"Win rate: {wins/len(completed)*100:.1f}%")
        print(f"+5% hit rate: {big_wins/len(completed)*100:.1f}%")
        print(f"Average return: {sum(returns)/len(returns):+.2f}%")
        print(f"Best: {max(returns):+.1f}% | Worst: {min(returns):+.1f}%")

        for low, high in [(100, 999), (80, 99), (70, 79)]:
            bucket = [s for s in completed if low <= s["score"] <= high]
            if bucket:
                b_returns = [s["return_30d"] for s in bucket]
                b_wins = sum(1 for r in b_returns if r > 0)
                print(f"\nScore {low}-{high}: {len(bucket)} signals")
                print(f"  Win rate: {b_wins/len(bucket)*100:.1f}%")
                print(f"  Avg return: {sum(b_returns)/len(b_returns):+.2f}%")
    else:
        print("\nNo completed signals yet (need 30+ days of tracking)")

    pending = [s for s in data["signals"] if s.get("return_30d") is None]
    print(f"\nPending signals: {len(pending)}")
    print(f"\nTotal tracked: {len(data['signals'])}")


if __name__ == "__main__":
    scan_and_save()
