"""
Signal Tracker - GitHub Actions Version
Runs every 5 minutes during market hours (9:00 AM - 5:00 PM ET)
Commits results back to the repository.

v2 UPDATES (Feb 2026):
- Scan every 5 minutes (was 30 min)
- Market cap from Yahoo Finance (UW API doesn't return it)
- Outcome tracking uses historical closes (was using current price)
- Outcome tracking only runs on top-of-hour scans (efficiency)
- Added +1d and +3d checkpoints

V4 SCORING (Feb 21, 2026):
- Backtested 3,347 signals with return data (Feb 2-17, 2026)
- V/OI is the only validated predictive feature (61% win rate at >= 10x)
- REMOVED: DTE bonus (was +30, data shows DTE 0-3 = 38.8% win rate)
- REMOVED: Sweep bonus (was +15, data shows sweeps = 42.1% vs 56.2%)
- REMOVED: Block trade bonus (was +20, data shows 1-3 fills = 32.1%)
- REMOVED: Large premium bonus (was +15, $500K+ is institutional hedging)
- ADDED: Insider-sized premium sweet spot ($50K-$200K = +15)
- ADDED: Sector penalties (Consumer Defensive = 27% win rate)
- ADDED: Earnings proximity bonus (pre-earnings + high V/OI = best combo)
- Ask % demoted from +35 to +3 (not predictive)
- MIN_SCORE lowered from 70 to 20 (V4 scale is tighter)
- Cross-referenced against Feb 2026 big movers: pre-catalyst signals
  averaged $27K median premium, 0.7x median V/OI, 28% sweep rate

V5 ENHANCEMENTS (Feb 22, 2026):
- PUT SCANNING: now captures both calls and puts (was calls only)
- Sentiment confirmation inverted for puts (bearish net premium = confirming)
- Outcome tracking: puts win when stock drops (inverted from calls)
- OPTION LEVERAGE: implied_leverage field + estimate_option_return() at checkpoints
- INTRADAY TIMING: scan_hour field (UTC) for time-of-day analysis
- EXIT FRAMEWORK: peak_return, peak_checkpoint, drawdown_from_peak fields
- BUG FIXES: exclusion list enforcement, V/OI hard gate, DTE multi-format parsing
- DATA CAPTURE: created_at, total_size, option_chain, iv, all_opening, has_floor, has_multileg
- DATA CAPTURE: avg_30d_volume, shares_outstanding, beta, issue_type, has_earnings_history
- DATA CAPTURE: 5-day options volume history (was saving only latest day)
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
SIGNALS_FILE = OUTPUT_DIR / "signal_database.json"

# Quality thresholds (V4: lowered to capture more signals on tighter scale)
MIN_SCORE = 20
MIN_VOI_RATIO = 5.0

# Sectors to penalize (backtest: sub-40% win rates at 5d)
SECTOR_PENALTIES = {
    "Consumer Defensive": -15,    # 27% win rate, -5.47% avg
    "Financial Services": -10,    # 41.5%, -2.06%
    "Basic Materials": -5,        # 38.4%, -1.84%
    "Communication Services": -5, # 41.6%, -1.51%
}

# Outcome tracking
MAX_OUTCOME_UPDATES = 50  # Max price fetches per run for outcome tracking

# Excluded tickers
EXCLUDED = {
    # Indices
    "SPY", "QQQ", "IWM", "DIA", "SPX", "SPXW", "RUTW", "NDX",
    # Volatility
    "VIX", "VXX", "UVXY",
    # Leveraged ETFs
    "SQQQ", "TQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "TNA", "TZA",
    # Sector ETFs
    "XLF", "XLE", "XLK", "XLV", "XBI", "ARKK", "GDX", "GDXJ",
    # Precious metals / commodities / currencies
    "GLD", "SLV", "AEM", "USO", "UNG", "CPER", "TLT", "UUP", "FXE",
    # Crypto-related
    "COIN", "MARA", "MSTR", "HUT", "WULF", "RIOT", "BITF", "CLSK", "IBIT", "BITO",
    # Country / region ETFs
    "FXI", "EWZ", "EWJ", "EWT", "EWY", "EWG", "EWU", "EWA", "EWC", "EWH",
    "EWS", "EWM", "EWP", "EWQ", "EWI", "EWL", "EWN", "EWD", "EWK", "EWO",
    "INDA", "MCHI", "THD", "EPOL", "TUR", "ECH", "ARGT", "GXG", "EIDO",
    "RSX", "FLBR", "FLCH", "FLJP", "FLGB", "FLKR",
    # Broad market / international ETFs
    "EEM", "EFA", "VTI", "VOO", "IVV", "VWO", "VEA", "IEMG", "ACWI", "VT",
    "VXUS", "IXUS", "SCHF", "IEFA", "KWEB", "ASHR", "MSOS", "AMLP",
    "ARKK", "ARKG", "ARKF", "ARKW", "ARKQ",
}


def load_signals():
    """Load existing tracked signals. Historical data is never deleted."""
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
            pass  # Expected to fail, but sets cookies
        req2 = urllib.request.Request(
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        crumb = opener.open(req2, timeout=10).read().decode()
        return crumb, opener
    except Exception:
        return None, None


# Module-level cache for Yahoo crumb (reused across calls)
_yahoo_crumb = None
_yahoo_opener = None


def get_yahoo_quote(ticker):
    """Get quote data (market cap, shares outstanding) from Yahoo Finance v7 API."""
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
        # Crumb may have expired - try refreshing once
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


# Cache for daily options volume and ticker info
_options_volume_cache = {}
_ticker_info_cache = {}


def get_ticker_info(ticker):
    """
    Fetch company info: sector, market_cap, next_earnings.
    Uses /api/stock/{ticker}/info endpoint.
    """
    global _ticker_info_cache

    if ticker in _ticker_info_cache:
        return _ticker_info_cache[ticker]

    try:
        resp = httpx.get(
            f"{BASE_URL}/api/stock/{ticker}/info",
            headers=HEADERS,
            timeout=30
        )
        if resp.status_code != 200:
            return None

        data = resp.json().get("data", {})
        if not data:
            return None

        # Extract relevant fields
        market_cap = data.get("market_cap")
        if market_cap:
            market_cap = float(market_cap)

        # Yahoo Finance fallback for market cap (UW API often returns None)
        if not market_cap:
            market_cap = get_market_cap_yahoo(ticker)
            time.sleep(0.3)

        result = {
            "sector": data.get("sector"),
            "industry": data.get("industry"),
            "market_cap": market_cap,
            "next_earnings": data.get("next_earnings_date"),
            "name": data.get("name"),
            # Free fields from same endpoint
            "avg_30d_volume": int(float(data["avg30_volume"])) if data.get("avg30_volume") else None,
            "shares_outstanding": int(float(data["outstanding"])) if data.get("outstanding") else None,
            "beta": float(data["beta"]) if data.get("beta") else None,
            "issue_type": data.get("issue_type"),
            "has_earnings_history": data.get("has_earnings_history"),
        }

        _ticker_info_cache[ticker] = result
        time.sleep(0.3)
        return result

    except Exception as e:
        print(f"Error fetching ticker info for {ticker}: {e}")
        return None


def categorize_market_cap(market_cap):
    """Categorize market cap into size buckets."""
    if market_cap is None:
        return None
    if market_cap >= 200_000_000_000:  # $200B+
        return "mega"
    elif market_cap >= 10_000_000_000:  # $10B+
        return "large"
    elif market_cap >= 2_000_000_000:  # $2B+
        return "mid"
    elif market_cap >= 300_000_000:  # $300M+
        return "small"
    else:
        return "micro"


def get_daily_options_volume(ticker):
    """
    Fetch daily options volume to verify net premium sentiment.
    Returns dict with call_premium, put_premium, net_premium, net_sentiment, confirmed.
    """
    global _options_volume_cache

    # Check cache first
    if ticker in _options_volume_cache:
        return _options_volume_cache[ticker]

    try:
        resp = httpx.get(
            f"{BASE_URL}/api/stock/{ticker}/options-volume",
            headers=HEADERS,
            params={"limit": 5},
            timeout=30
        )
        if resp.status_code != 200:
            return None

        data = resp.json().get("data", [])
        if not data:
            return None

        # Get most recent day's data
        latest = data[0]
        call_premium = float(latest.get("call_premium", 0) or 0)
        put_premium = float(latest.get("put_premium", 0) or 0)
        call_volume = int(latest.get("call_volume", 0) or 0)
        put_volume = int(latest.get("put_volume", 0) or 0)
        net_premium = call_premium - put_premium

        # Store full 5-day history (we fetch it anyway, don't discard it)
        volume_history = []
        for day in data[:5]:
            volume_history.append({
                "date": day.get("date", ""),
                "call_premium": float(day.get("call_premium", 0) or 0),
                "put_premium": float(day.get("put_premium", 0) or 0),
                "call_volume": int(day.get("call_volume", 0) or 0),
                "put_volume": int(day.get("put_volume", 0) or 0),
            })

        result = {
            "date": latest.get("date", ""),
            "call_premium": call_premium,
            "put_premium": put_premium,
            "call_volume": call_volume,
            "put_volume": put_volume,
            "net_premium": net_premium,
            "net_sentiment": "BULLISH" if net_premium > 0 else "BEARISH",
            "confirmed": net_premium > 0,
            "volume_history_5d": volume_history,
        }

        # Cache result
        _options_volume_cache[ticker] = result
        time.sleep(0.3)  # Rate limit
        return result

    except Exception as e:
        print(f"Error fetching options volume for {ticker}: {e}")
        return None


def calculate_score(alert):
    """
    V4 scoring model -- outcome-validated, insider-aware.

    Backtested on 3,347 signals (Feb 2-17, 2026). Cross-referenced against
    22 major stock moves to profile real pre-catalyst flow patterns.

    Key findings driving V4:
    - V/OI is the ONLY feature validated across all timeframes
    - Insiders use modest-sized bets ($20K-$200K), not whale trades
    - Sweeps, block trades, short DTE are anti-predictive (noise/hedging)
    - V4 top quartile: 61% win rate, +2.24% avg (vs V3 top: 44%, -1.00%)
    """
    score = 0
    flags = []

    # --- 1. V/OI RATIO -- THE DOMINANT SIGNAL (max 50 pts) ---
    # Validated: 64.4% win at >= 50x, 61.7% at 10-19x, 67.5% at 20-49x (10d)
    voi_str = alert.get("volume_oi_ratio", "0")
    voi = float(voi_str) if voi_str else 0

    if voi >= 50:
        score += 50
        flags.append(f"Extreme V/OI ({voi:.0f}x)")
    elif voi >= 20:
        score += 45
        flags.append(f"Very high V/OI ({voi:.0f}x)")
    elif voi >= 10:
        score += 35
        flags.append(f"High V/OI ({voi:.1f}x)")
    elif voi >= 5:
        score += 20
        flags.append(f"Elevated V/OI ({voi:.1f}x)")
    elif 0 < voi < 1:
        score -= 5  # Below average

    # --- 2. PREMIUM SWEET SPOT -- INSIDER-SIZED (max 15 pts) ---
    # Cross-reference: pre-catalyst signals average $27K median premium.
    # $50K-$200K had best performance. $500K+ is institutional hedging.
    premium = float(alert.get("total_premium", 0) or 0)
    if 50_000 <= premium < 200_000:
        score += 15
        flags.append(f"Insider-sized (${premium/1000:.0f}K)")
    elif 200_000 <= premium < 500_000:
        score += 10
        flags.append(f"Sizable (${premium/1000:.0f}K)")
    elif 20_000 <= premium < 50_000:
        score += 5
        flags.append(f"Modest bet (${premium/1000:.0f}K)")
    # No bonus for $500K+ or < $20K

    # --- 3. ASK % -- HEAVILY DEMOTED (max 3 pts, was 35) ---
    # Data: Ask < 80% (59.1%, +1.84%) outperforms Ask >= 98% (52.2%, +0.68%)
    total_ask_prem = float(alert.get("total_ask_side_prem", 0) or 0)
    total_bid_prem = float(alert.get("total_bid_side_prem", 0) or 0)
    total_prem = total_ask_prem + total_bid_prem
    ask_pct = (total_ask_prem / total_prem * 100) if total_prem > 0 else 0

    if ask_pct >= 95:
        score += 3

    # --- 4. OTM % -- SMALL BONUS FOR MODERATE OTM (max 5 pts) ---
    # V/OI>=20 + OTM>=5% + Confirmed at 10d: 79.4% win rate.
    # But deep OTM alone doesn't help. Danger zone at 0-5%.
    strike = float(alert.get("strike", 0) or 0)
    spot = float(alert.get("underlying_price", 0) or 0)
    opt_type = alert.get("type", "").lower()
    otm_pct = 0

    if spot > 0 and strike > 0:
        if opt_type == "call":
            otm_pct = ((strike - spot) / spot * 100) if strike > spot else 0
        else:
            otm_pct = ((spot - strike) / spot * 100) if strike < spot else 0

        if 5 <= otm_pct <= 15:
            score += 5
            flags.append(f"Moderate OTM ({otm_pct:.0f}%)")
        elif 0 < otm_pct < 5:
            score -= 5  # Danger zone

    # --- 5. DTE -- PENALTY ONLY (was +30 bonus) ---
    # Data: DTE 0-3 has 38.8% win rate (worst bucket). All DTE underperform.
    dte = None
    expiry_raw = alert.get("expiry") or ""
    expiry_raw = str(expiry_raw).strip()
    if expiry_raw:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                expiry = datetime.strptime(expiry_raw, fmt)
                dte = (expiry - datetime.now()).days
                break
            except ValueError:
                continue

    if dte is not None and dte <= 3:
        score -= 5  # Danger zone

    # --- REMOVED FEATURES ---
    # Sweep: REMOVED (42.1% win vs 56.2% without -- anti-predictive)
    # Block trade (low fills): REMOVED (1-3 fills = 32.1% win rate)
    # Large premium bonus: REMOVED (replaced by insider sweet spot above)

    return score, flags, ask_pct, voi, dte, otm_pct, premium


def estimate_option_return(entry_price, checkpoint_price, strike, option_price_entry,
                           dte_at_entry, days_elapsed, opt_type="call"):
    """
    Estimate option P&L using delta approximation from moneyness.

    No free API for historical option prices, so we approximate:
    option_return ~ (delta * stock_move) / option_price, adjusted for time decay.
    Captures the key insight: a 2% stock move on a $0.50 option = potentially 40%+ gain.
    """
    if not option_price_entry or option_price_entry <= 0:
        return None
    if not checkpoint_price or not entry_price:
        return None

    stock_move = checkpoint_price - entry_price
    if opt_type == "put":
        stock_move = -stock_move  # Puts profit from decline

    # Delta estimate from moneyness (how far ITM/OTM the option is)
    if opt_type == "call":
        moneyness = (entry_price - strike) / entry_price  # positive = ITM
    else:
        moneyness = (strike - entry_price) / entry_price

    if moneyness >= 0.05:
        delta = 0.70
    elif moneyness >= 0:
        delta = 0.55
    elif moneyness >= -0.05:
        delta = 0.40
    elif moneyness >= -0.15:
        delta = 0.25
    else:
        delta = 0.10

    # Time decay: sqrt approximation (theta accelerates near expiry)
    if dte_at_entry and dte_at_entry > 0:
        dte_remaining = max(dte_at_entry - days_elapsed, 0)
        time_factor = (dte_remaining / dte_at_entry) ** 0.5
    else:
        time_factor = 1.0

    estimated_new_price = max((option_price_entry + delta * stock_move) * time_factor, 0)
    return round((estimated_new_price - option_price_entry) / option_price_entry * 100, 1)


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
        ticker = (alert.get("ticker") or "").strip().upper()
        opt_type = (alert.get("type") or "").lower()
        alert_id = alert.get("id", "")

        if not ticker or ticker in EXCLUDED:
            continue
        if opt_type not in ("call", "put"):
            continue
        if alert_id in existing_ids:
            continue

        score, flags, ask_pct_val, voi_val, dte, otm_pct, premium_val = calculate_score(alert)

        # Hard gate: V/OI is the only validated predictive feature.
        # Signals below MIN_VOI_RATIO are noise regardless of other factors.
        if voi_val < MIN_VOI_RATIO:
            continue

        # Pre-enrichment score check (sector/cap/earnings adjust later)
        if score < MIN_SCORE:
            continue

        spot = float(alert.get("underlying_price", 0) or 0)
        if spot <= 0:
            spot = get_stock_price(ticker)

        if not spot:
            continue

        # --- MUST HAVE: Raw signal characteristics from alert ---
        volume = int(alert.get("volume", 0) or 0)
        open_interest = int(alert.get("open_interest", 0) or 0)
        has_sweep = bool(alert.get("has_sweep", False))
        trade_count = int(alert.get("trade_count", 0) or 0)

        # Recalculate OTM % with actual spot (may differ from alert's underlying_price)
        strike = float(alert.get("strike", 0) or 0)
        if spot > 0 and strike > 0:
            if opt_type == "call":
                otm_pct = ((strike - spot) / spot * 100) if strike > spot else 0
            else:  # put
                otm_pct = ((spot - strike) / spot * 100) if strike < spot else 0

        # Option price at entry (mid price or ask)
        option_bid = float(alert.get("bid", 0) or 0)
        option_ask = float(alert.get("ask", 0) or 0)
        if option_bid > 0 and option_ask > 0:
            option_price_entry = (option_bid + option_ask) / 2
        elif option_ask > 0:
            option_price_entry = option_ask
        else:
            option_price_entry = None

        # Alert rule (what triggered it)
        alert_rule = alert.get("alert_rule") or alert.get("rule")

        # Free fields from flow alert (already fetched, just not saved before)
        created_at = alert.get("created_at", "")  # Alert creation timestamp
        total_size = int(alert.get("total_size", 0) or 0)  # Contract count
        option_chain = alert.get("option_chain", "")  # Full OCC symbol
        iv = float(alert.get("iv_end", 0) or 0)  # Implied volatility
        all_opening = bool(alert.get("all_opening_trades", False))
        has_floor = bool(alert.get("has_floor", False))
        has_multileg = bool(alert.get("has_multileg", False))

        # --- MUST HAVE: Company context ---
        ticker_info = get_ticker_info(ticker)
        if ticker_info:
            sector = ticker_info.get("sector")
            industry = ticker_info.get("industry")
            market_cap = ticker_info.get("market_cap")
            cap_category = categorize_market_cap(market_cap)
            next_earnings = ticker_info.get("next_earnings")
            avg_30d_volume = ticker_info.get("avg_30d_volume")
            shares_outstanding = ticker_info.get("shares_outstanding")
            beta = ticker_info.get("beta")
            issue_type_ticker = ticker_info.get("issue_type")
            has_earnings_history = ticker_info.get("has_earnings_history")

            # Calculate days to earnings
            if next_earnings:
                try:
                    earnings_date = datetime.strptime(next_earnings, "%Y-%m-%d")
                    days_to_earnings = (earnings_date - datetime.now()).days
                except:
                    days_to_earnings = None
            else:
                days_to_earnings = None
        else:
            sector = None
            industry = None
            market_cap = None
            cap_category = None
            next_earnings = None
            days_to_earnings = None
            avg_30d_volume = None
            shares_outstanding = None
            beta = None
            issue_type_ticker = None
            has_earnings_history = None

        # --- V4: SECTOR PENALTY ---
        if sector in SECTOR_PENALTIES:
            score += SECTOR_PENALTIES[sector]
            flags.append(f"Weak sector ({sector})")

        # --- V4: MARKET CAP BONUS ---
        if cap_category == "large":
            score += 5
        elif cap_category == "mega":
            score += 3

        # --- V4: EARNINGS PROXIMITY BONUS ---
        if days_to_earnings is not None and 0 <= days_to_earnings <= 7:
            score += 10
            flags.append(f"Pre-earnings ({days_to_earnings}d)")
        elif days_to_earnings is not None and 8 <= days_to_earnings <= 14:
            score += 5
            flags.append(f"Near earnings ({days_to_earnings}d)")

        # Verify net premium sentiment
        vol_data = get_daily_options_volume(ticker)
        if vol_data:
            net_premium = vol_data["net_premium"]
            net_sentiment = vol_data["net_sentiment"]
            confirmed = vol_data["confirmed"]
            daily_call_premium = vol_data["call_premium"]
            daily_put_premium = vol_data["put_premium"]
            daily_call_volume = vol_data.get("call_volume")
            daily_put_volume = vol_data.get("put_volume")

            # V4: Contradicted penalty only (confirmed no longer gets bonus)
            # Data showed: Confirmed 42.8% win vs Unknown 69.2%
            # "Confirmed" may indicate crowded trades, not insider conviction
            # For puts: bearish net premium is confirming, bullish is contradicting
            if opt_type == "put":
                confirmed = not confirmed  # Invert for puts
            if not confirmed:
                score -= 10
                direction = "BULLISH" if opt_type == "put" else "BEARISH"
                flags.append(f"!! NET {direction} (${net_premium/1000:.0f}K)")
        else:
            net_premium = None
            net_sentiment = "UNKNOWN"
            confirmed = None
            daily_call_premium = None
            daily_put_premium = None
            daily_call_volume = None
            daily_put_volume = None

        # Implied leverage: stock price / option price
        implied_leverage = None
        if option_price_entry and option_price_entry > 0:
            implied_leverage = round(spot / option_price_entry, 1)

        signal = {
            # Core identification
            "alert_id": alert_id,
            "scan_date": datetime.now().strftime("%Y-%m-%d"),
            "scan_time": datetime.now().strftime("%H:%M"),
            "scan_hour": datetime.utcnow().hour,
            "opt_type": opt_type,
            "ticker": ticker,
            "strike": strike,
            "expiry": alert.get("expiry", ""),
            "premium": float(alert.get("total_premium", 0) or 0),
            "score": score,
            "flags": flags,
            "entry_price": spot,
            "ask_pct": ask_pct_val,
            "voi_ratio": voi_val,

            # MUST HAVE: Raw signal characteristics
            "volume": volume,
            "open_interest": open_interest,
            "has_sweep": has_sweep,
            "trade_count": trade_count,
            "dte": dte,
            "otm_pct": otm_pct,

            # SHOULD HAVE: Option pricing
            "option_price_entry": option_price_entry,
            "option_bid": option_bid,
            "option_ask": option_ask,
            "implied_leverage": implied_leverage,
            "alert_rule": alert_rule,

            # Flow alert metadata (free -- already fetched)
            "created_at": created_at,
            "total_size": total_size,
            "option_chain": option_chain,
            "iv": iv,
            "all_opening": all_opening,
            "has_floor": has_floor,
            "has_multileg": has_multileg,

            # MUST HAVE: Company context
            "sector": sector,
            "industry": industry,
            "market_cap": market_cap,
            "cap_category": cap_category,
            "next_earnings": next_earnings,
            "days_to_earnings": days_to_earnings,
            "avg_30d_volume": avg_30d_volume,
            "shares_outstanding": shares_outstanding,
            "beta": beta,
            "issue_type_ticker": issue_type_ticker,
            "has_earnings_history": has_earnings_history,

            # Net premium verification
            "net_premium": net_premium,
            "net_sentiment": net_sentiment,
            "confirmed": confirmed,
            "daily_call_premium": daily_call_premium,
            "daily_put_premium": daily_put_premium,
            "daily_call_volume": daily_call_volume,
            "daily_put_volume": daily_put_volume,
            "volume_history_5d": vol_data.get("volume_history_5d") if vol_data else None,

            # Price tracking for outcome analysis
            "price_1d": None,
            "price_3d": None,
            "price_5d": None,
            "price_10d": None,
            "price_20d": None,
            "price_30d": None,
            "return_1d": None,
            "return_3d": None,
            "return_5d": None,
            "return_10d": None,
            "return_20d": None,
            "return_30d": None,
            "outcome": None,

            # Option return estimates (delta approximation)
            "option_return_1d": None,
            "option_return_3d": None,
            "option_return_5d": None,
            "option_return_10d": None,
            "option_return_20d": None,
            "option_return_30d": None,

            # SPY benchmark for alpha calculation
            "spy_entry_price": None,
            "spy_return_1d": None,
            "spy_return_3d": None,
            "spy_return_5d": None,
            "spy_return_10d": None,
            "spy_return_20d": None,
            "spy_return_30d": None,
            "alpha_1d": None,
            "alpha_3d": None,
            "alpha_5d": None,
            "alpha_10d": None,
            "alpha_20d": None,
            "alpha_30d": None,

            # Exit framework (populated by outcome tracker)
            "peak_return": None,
            "peak_checkpoint": None,
            "drawdown_from_peak": None,
        }

        # Final V4 score gate (after all enrichment adjustments)
        if score < MIN_SCORE:
            continue

        new_signals.append(signal)
        existing_ids.add(alert_id)

    data["signals"].extend(new_signals)
    data["last_scan"] = datetime.now().isoformat()

    print(f"\nNew high-quality signals found: {len(new_signals)}")

    if new_signals:
        print("\n--- NEW SIGNALS ---")
        for s in sorted(new_signals, key=lambda x: x["score"], reverse=True)[:10]:
            # Confirmation indicator (ASCII for Windows compatibility)
            if s.get("confirmed") is True:
                conf = "OK"
            elif s.get("confirmed") is False:
                conf = "!!"
            else:
                conf = "? "

            otype = "PUT " if s.get("opt_type") == "put" else "CALL"
            print(f"  {conf} {otype} {s['ticker']:6} Score:{s['score']:3} | ${s['premium']/1000:.0f}K | "
                  f"Ask:{s['ask_pct']:.0f}% | V/OI:{s['voi_ratio']:.1f}x | {s['flags'][0]}")

    # Update existing signals with price data
    # Only run full outcome tracking on top-of-hour scans (efficiency for 5-min runs)
    now_utc = datetime.utcnow()
    run_outcomes = (now_utc.minute < 10)  # Only first scan of each hour

    if run_outcomes:
        print("\nUpdating price data for existing signals (top-of-hour run)...")
    else:
        print("\nSkipping outcome tracking (not top-of-hour).")

    update_count = 0
    price_cache = {}  # Cache: "TICKER_YYYY-MM-DD" -> price

    if run_outcomes:
        checkpoints = [
            (1, "price_1d", "return_1d"),
            (3, "price_3d", "return_3d"),
            (5, "price_5d", "return_5d"),
            (10, "price_10d", "return_10d"),
            (20, "price_20d", "return_20d"),
            (30, "price_30d", "return_30d"),
        ]

        for signal in data["signals"]:
            if update_count >= MAX_OUTCOME_UPDATES:
                break

            scan_date = datetime.strptime(signal["scan_date"], "%Y-%m-%d")
            days_elapsed = (datetime.now() - scan_date).days
            ticker = signal["ticker"]
            entry = signal["entry_price"]

            if not entry or entry <= 0:
                continue

            signal_updated = False

            # Fetch SPY entry price for this signal's scan date (cached per date)
            scan_date_str = signal["scan_date"]
            spy_entry_key = f"SPY_{scan_date_str}"
            if signal.get("spy_entry_price") is None:
                if spy_entry_key in price_cache:
                    spy_entry = price_cache[spy_entry_key]
                else:
                    spy_entry = get_historical_close("SPY", scan_date_str)
                    if spy_entry is None:
                        spy_entry = get_stock_price("SPY")
                    time.sleep(0.3)
                    price_cache[spy_entry_key] = spy_entry
                if spy_entry:
                    signal["spy_entry_price"] = round(spy_entry, 4)
                    signal_updated = True

            for days, price_key, return_key in checkpoints:
                # Initialize fields for older signals that don't have +1d/+3d keys
                if price_key not in signal:
                    signal[price_key] = None
                if return_key not in signal:
                    signal[return_key] = None

                # Initialize SPY/alpha fields for older signals
                spy_return_key = f"spy_return_{days}d"
                alpha_key = f"alpha_{days}d"
                if spy_return_key not in signal:
                    signal[spy_return_key] = None
                if alpha_key not in signal:
                    signal[alpha_key] = None

                if days_elapsed >= days and signal[price_key] is None:
                    target_date = scan_date + timedelta(days=days)
                    target_str = target_date.strftime("%Y-%m-%d")
                    cache_key = f"{ticker}_{target_str}"

                    # Check if target date is today or yesterday (use current price)
                    days_to_target = (datetime.now() - target_date).days

                    if cache_key in price_cache:
                        price = price_cache[cache_key]
                    elif days_to_target <= 1:
                        price = get_stock_price(ticker)
                        time.sleep(0.3)
                        price_cache[cache_key] = price
                    else:
                        price = get_historical_close(ticker, target_str)
                        time.sleep(0.3)
                        price_cache[cache_key] = price

                    if price:
                        signal[price_key] = round(price, 4)
                        signal[return_key] = round((price - entry) / entry * 100, 2)
                        signal_updated = True

                        # Estimate option return at this checkpoint
                        opt_return_key = f"option_return_{days}d"
                        if signal.get("option_price_entry"):
                            signal[opt_return_key] = estimate_option_return(
                                entry, price, signal.get("strike", 0),
                                signal["option_price_entry"],
                                signal.get("dte"), days,
                                signal.get("opt_type", "call")
                            )

                # Calculate SPY return and alpha at this checkpoint
                spy_entry_price = signal.get("spy_entry_price")
                if days_elapsed >= days and signal.get(spy_return_key) is None and spy_entry_price:
                    target_date = scan_date + timedelta(days=days)
                    target_str = target_date.strftime("%Y-%m-%d")
                    spy_cache_key = f"SPY_{target_str}"
                    days_to_target = (datetime.now() - target_date).days

                    if spy_cache_key in price_cache:
                        spy_price = price_cache[spy_cache_key]
                    elif days_to_target <= 1:
                        spy_price = get_stock_price("SPY")
                        time.sleep(0.3)
                        price_cache[spy_cache_key] = spy_price
                    else:
                        spy_price = get_historical_close("SPY", target_str)
                        time.sleep(0.3)
                        price_cache[spy_cache_key] = spy_price

                    if spy_price:
                        spy_ret = round((spy_price - spy_entry_price) / spy_entry_price * 100, 2)
                        signal[spy_return_key] = spy_ret
                        signal_updated = True

                        # Alpha = signal return - SPY return
                        sig_ret = signal.get(return_key)
                        if sig_ret is not None:
                            signal[alpha_key] = round(sig_ret - spy_ret, 2)

            # Update exit framework: peak return, drawdown from peak
            returns_so_far = []
            for k in [1, 3, 5, 10, 20, 30]:
                r = signal.get(f"return_{k}d")
                if r is not None:
                    returns_so_far.append((k, r))
            if returns_so_far:
                # For puts, stock return is negative when winning. Use absolute direction.
                is_put = signal.get("opt_type") == "put"
                directed = [(-r if is_put else r, k) for k, r in returns_so_far]
                best_val, best_k = max(directed, key=lambda x: x[0])
                signal["peak_return"] = round(best_val, 2)
                signal["peak_checkpoint"] = f"{best_k}d"
                latest_val = directed[-1][0]
                signal["drawdown_from_peak"] = round(latest_val - best_val, 2)

            # Determine win/loss at 30d
            if signal.get("price_30d") is not None and signal.get("outcome") is None:
                if signal.get("opt_type") == "put":
                    signal["outcome"] = "win" if signal["return_30d"] < 0 else "loss"
                else:
                    signal["outcome"] = "win" if signal["return_30d"] > 0 else "loss"
                signal_updated = True

            if signal_updated:
                update_count += 1

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

        # Alpha vs SPY summary
        alphas_30d = [s["alpha_30d"] for s in completed if s.get("alpha_30d") is not None]
        if alphas_30d:
            alpha_wins = sum(1 for a in alphas_30d if a > 0)
            print(f"\n--- ALPHA vs SPY (30d) ---")
            print(f"Signals with alpha data: {len(alphas_30d)}")
            print(f"Alpha win rate: {alpha_wins/len(alphas_30d)*100:.1f}% (beat SPY)")
            print(f"Average alpha: {sum(alphas_30d)/len(alphas_30d):+.2f}%")
            print(f"Best alpha: {max(alphas_30d):+.1f}% | Worst: {min(alphas_30d):+.1f}%")

        for low, high in [(100, 999), (80, 99), (70, 79)]:
            bucket = [s for s in completed if low <= s["score"] <= high]
            if bucket:
                b_returns = [s["return_30d"] for s in bucket]
                b_wins = sum(1 for r in b_returns if r > 0)
                b_alphas = [s["alpha_30d"] for s in bucket if s.get("alpha_30d") is not None]
                alpha_str = ""
                if b_alphas:
                    alpha_str = f" | Avg alpha: {sum(b_alphas)/len(b_alphas):+.2f}%"
                print(f"\nScore {low}-{high}: {len(bucket)} signals")
                print(f"  Win rate: {b_wins/len(bucket)*100:.1f}%")
                print(f"  Avg return: {sum(b_returns)/len(b_returns):+.2f}%{alpha_str}")
    else:
        print("\nNo completed signals yet (need 30+ days of tracking)")

    pending = [s for s in data["signals"] if s.get("return_30d") is None]
    print(f"\nPending signals: {len(pending)}")
    print(f"\nTotal tracked: {len(data['signals'])}")


if __name__ == "__main__":
    scan_and_save()
