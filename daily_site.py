"""Build the A/H-share five-trading-day site snapshot."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable


Bar = dict[str, Any]
Row = dict[str, Any]


def last_five_sessions(bars: Iterable[Bar]) -> list[Bar]:
    """Return the latest five distinct trading dates, or no result when incomplete."""
    by_date = {str(item["date"]): item for item in bars}
    selected = [by_date[key] for key in sorted(by_date)[-5:]]
    return selected if len(selected) == 5 else []


def five_day_return(bars: list[Bar]) -> float:
    """Use the first session's open and fifth session's close."""
    if len(bars) != 5 or float(bars[0]["open"]) <= 0:
        raise ValueError("five adjusted sessions with positive first open required")
    return (float(bars[-1]["close"]) / float(bars[0]["open"]) - 1) * 100


def add_moving_averages(bars: list[Bar], windows: tuple[int, ...]) -> list[Bar]:
    enriched = [dict(item) for item in bars]
    closes = [float(item["close"]) for item in bars]
    for index, item in enumerate(enriched):
        for window in windows:
            item[f"ma{window}"] = (
                sum(closes[index - window + 1:index + 1]) / window
                if index + 1 >= window else None
            )
    return enriched


def to_weekly_bars(bars: list[Bar]) -> list[Bar]:
    """Aggregate daily OHLC by ISO trading week; mark a live week as partial."""
    grouped: dict[tuple[int, int], list[Bar]] = {}
    for item in sorted(bars, key=lambda value: value["date"]):
        iso = date.fromisoformat(str(item["date"])).isocalendar()
        grouped.setdefault((iso.year, iso.week), []).append(item)
    weekly: list[Bar] = []
    for items in grouped.values():
        latest = date.fromisoformat(str(items[-1]["date"]))
        weekly.append({
            "date": items[-1]["date"],
            "open": float(items[0]["open"]),
            "close": float(items[-1]["close"]),
            "high": max(float(item["high"]) for item in items),
            "low": min(float(item["low"]) for item in items),
            "volume": sum(float(item.get("volume") or 0) for item in items),
            "partial": latest.weekday() < 4,
        })
    return weekly


RANK_CONFIG = {
    "chem_large": ("基础化工", 3, 3),
    "chem_small": ("基础化工", 5, 5),
    "oil": ("石油石化", 5, 5),
    "bj": ("北交所", 3, 3),
    "hk": ("港股", 3, 2),
}


def select_rankings(rows: list[Row]) -> dict[str, dict[str, list[Row]]]:
    output: dict[str, dict[str, list[Row]]] = {}
    for bucket, (group, gain_limit, loss_limit) in RANK_CONFIG.items():
        eligible = [row for row in rows if row["group"] == group]
        if bucket == "chem_large":
            eligible = [row for row in eligible if row.get("market_cap_yuan") is not None and row["market_cap_yuan"] > 20_000_000_000]
        elif bucket == "chem_small":
            eligible = [row for row in eligible if row.get("market_cap_yuan") is not None and row["market_cap_yuan"] < 20_000_000_000]
        eligible = [row for row in eligible if row.get("return_pct") is not None]
        gainers = sorted(eligible, key=lambda row: (-row["return_pct"], row["code"]))[:gain_limit]
        losers = sorted(eligible, key=lambda row: (row["return_pct"], row["code"]))[:loss_limit]
        output[bucket] = {"gainers": gainers, "losers": losers}
    return output


def normalize_code(code: str) -> str:
    """Normalize supported exchange codes; HK tickers always use five digits."""
    number, separator, market = code.strip().upper().partition(".")
    if not separator or market not in {"SH", "SZ", "BJ", "HK"}:
        raise ValueError(f"unsupported stock symbol: {code}")
    if market == "HK":
        if not re.fullmatch(r"\d{1,5}", number):
            raise ValueError(f"invalid HK symbol: {code}")
        number = number.zfill(5)
    elif not re.fullmatch(r"\d{6}", number):
        raise ValueError(f"invalid mainland symbol: {code}")
    return f"{number}.{market}"


def market_for_code(code: str) -> str:
    return "HK" if normalize_code(code).endswith(".HK") else "CN"


def tencent_symbol(code: str) -> str:
    normalized = normalize_code(code)
    number, market = normalized.split(".")
    return f"{market.lower()}{number}"


def parse_tencent_quote(raw: str, code: str) -> dict[str, Any]:
    """Parse a Tencent quote and retain the native trading currency."""
    if not raw.startswith(f"v_{tencent_symbol(code)}="):
        raise ValueError(f"unexpected quote symbol for {code}")
    _, _, body = raw.partition('="')
    fields = body.split('"', 1)[0].split("~")
    if len(fields) < 74:
        raise ValueError(f"incomplete quote for {code}")
    price = float(fields[3])
    if not (math.isfinite(price) and price > 0):
        raise ValueError(f"invalid last price for {code}")
    stamp_value = fields[30]
    stamp_format = "%Y/%m/%d %H:%M:%S" if "/" in stamp_value else "%Y%m%d%H%M%S"
    stamp = datetime.strptime(stamp_value, stamp_format).strftime("%Y-%m-%d %H:%M:%S")
    is_hk = code.endswith(".HK")
    quoted_cap = float(fields[45]) * 100_000_000 if fields[45] else None
    if is_hk:
        cap = quoted_cap
        currency = "HKD"
    else:
        shares = float(fields[73])
        if not (math.isfinite(shares) and shares > 0):
            raise ValueError(f"invalid total shares for {code}")
        cap = price * shares
        if quoted_cap is not None and abs(quoted_cap / cap - 1) > 0.02:
            raise ValueError(f"total market cap does not match shares for {code}")
        currency = "CNY"
    return {
        "name": fields[1] or code,
        "last_price": price,
        "market_cap": cap,
        "market_cap_yuan": None if is_hk else cap,
        "currency": currency,
        "market_cap_currency": currency,
        "quote_at": stamp,
        "quote_source": "Tencent quote",
        "quote_url": f"https://qt.gtimg.cn/q={tencent_symbol(code)}",
    }


def parse_tencent_history(payload: dict[str, Any], code: str) -> dict[str, Any]:
    symbol = tencent_symbol(code)
    record = payload.get("data", {}).get(symbol, {})
    if record.get("qfqday"):
        raw = record["qfqday"]
        adjustment = "qfq"
    elif code.endswith(".BJ") and record.get("day"):
        raw = record["day"]
        adjustment = "raw_bj"
    else:
        raise ValueError(f"qfq history unavailable for {code}")
    bars = []
    for item in raw:
        try:
            values = [float(item[index]) for index in range(1, 6)]
            if min(values[:4]) <= 0 or not all(math.isfinite(value) for value in values):
                continue
            bars.append({
                "date": str(item[0]),
                "open": values[0],
                "close": values[1],
                "high": values[2],
                "low": values[3],
                "volume": values[4],
            })
        except (IndexError, TypeError, ValueError):
            continue
    if not bars:
        raise ValueError(f"no valid history for {code}")
    return {"bars": sorted(bars, key=lambda item: item["date"]), "adjustment": adjustment}


EASTMONEY_HK_HISTORY_URL = "https://33.push2his.eastmoney.com/api/qt/stock/kline/get"


def parse_eastmoney_hk_history(payload: dict[str, Any], code: str) -> dict[str, Any]:
    """Parse Eastmoney's fqt=1 HK series, which is explicitly forward adjusted."""
    lines = payload.get("data", {}).get("klines") or []
    bars = []
    for line in lines:
        try:
            fields = str(line).split(",")
            values = [float(fields[index]) for index in range(1, 6)]
            if min(values[:4]) <= 0 or not all(math.isfinite(value) for value in values):
                continue
            bars.append({
                "date": fields[0], "open": values[0], "close": values[1],
                "high": values[2], "low": values[3], "volume": values[4],
            })
        except (IndexError, TypeError, ValueError):
            continue
    if not bars:
        raise ValueError(f"Eastmoney HK qfq history unavailable for {code}")
    return {
        "bars": sorted(bars, key=lambda item: item["date"]),
        "adjustment": "qfq_eastmoney",
        "history_url": EASTMONEY_HK_HISTORY_URL,
    }


def parse_sina_bj_history(payload: list[dict[str, Any]]) -> dict[str, Any]:
    bars = []
    for item in payload:
        try:
            bar = {"date": str(item["day"]), **{key: float(item[key]) for key in ("open", "close", "high", "low", "volume")}}
            if min(bar[key] for key in ("open", "close", "high", "low")) > 0:
                bars.append(bar)
        except (KeyError, TypeError, ValueError):
            continue
    if not bars:
        raise ValueError("no valid BJ history from Sina")
    return {"bars": sorted(bars, key=lambda item: item["date"]), "adjustment": "raw_bj",
            "history_url": "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"}


def parse_sina_adjusted_frame(frame: Any, code: str) -> dict[str, Any]:
    bars = []
    for row in frame.to_dict("records"):
        try:
            bar = {"date": str(row["date"])[:10],
                   **{key: float(row[key]) for key in ("open", "close", "high", "low", "volume")}}
            if min(bar[key] for key in ("open", "close", "high", "low")) > 0:
                bars.append(bar)
        except (KeyError, TypeError, ValueError):
            continue
    if not bars:
        raise ValueError(f"Sina qfq history unavailable for {code}")
    return {"bars": sorted(bars, key=lambda item: item["date"]), "adjustment": "qfq_sina",
            "history_url": f"https://finance.sina.com.cn/realstock/company/{tencent_symbol(code)}/qfq.js"}


def fetch_sina_adjusted_fallback(codes: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Use Sina's forward-adjusted series for BJ stocks and Tencent WAF misses."""
    import akshare as ak
    import requests
    histories: dict[str, dict[str, Any]] = {}
    quotes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    proxy_keys = {key: os.environ.pop(key) for key in list(os.environ) if "PROXY" in key.upper()}
    try:
        end = datetime.now(timezone(timedelta(hours=8))).date()
        start = end - timedelta(days=550)
        def fetch_one(code: str) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, str | None]:
            try:
                frame = ak.stock_zh_a_daily(symbol=tencent_symbol(code),
                                            start_date=start.strftime("%Y%m%d"),
                                            end_date=end.strftime("%Y%m%d"), adjust="qfq")
                history = parse_sina_adjusted_frame(frame, code)
                session = requests.Session()
                session.trust_env = False
                response = session.get(f"https://qt.gtimg.cn/q={tencent_symbol(code)}", timeout=12)
                response.raise_for_status()
                quote = parse_tencent_quote(response.content.decode("gbk", errors="replace"), code)
                return code, history, quote, None
            except Exception as exc:
                return code, None, None, f"{type(exc).__name__}: {exc}"
        # akshare's Sina path initializes py_mini_racer, whose native allocator is
        # not safe to initialize concurrently on Windows. Keep this fallback serial.
        for requested_code in codes:
            code, history, quote, error = fetch_one(requested_code)
            if history:
                histories[code] = history
            if quote:
                quotes[code] = quote
            if error:
                errors[code] = error
    finally:
        os.environ.update(proxy_keys)
    return histories, quotes, errors


def make_snapshot(
    universe: dict[str, list[str]],
    histories: dict[str, dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    """Rank stocks against separate latest five-session windows for CN and HK."""
    market_windows: dict[str, dict[str, str]] = {}
    market_dates: dict[str, list[str]] = {}
    for market in ("CN", "HK"):
        dates = sorted({
            str(bar["date"])
            for code, item in histories.items()
            if market_for_code(code) == market
            for bar in item.get("bars", [])
        })[-5:]
        if dates:
            if len(dates) != 5:
                raise ValueError(f"fewer than five {market} market sessions")
            market_dates[market] = dates
            market_windows[market] = {"window_start": dates[0], "as_of": dates[-1]}
    if not market_windows:
        raise ValueError("fewer than five market sessions")
    stats = {"universe": 0, "eligible": 0, "missing_history": 0, "stale_history": 0,
             "missing_quote": 0, "stale_quote": 0, "missing_cap": 0, "unadjusted_history": 0}
    excluded_codes = {key: [] for key in (
        "missing_history", "stale_history", "missing_quote", "stale_quote", "missing_cap", "unadjusted_history"
    )}
    rows: list[Row] = []
    seen: set[tuple[str, str]] = set()
    for group, codes in universe.items():
        for code in codes:
            if (group, code) in seen:
                continue
            seen.add((group, code))
            stats["universe"] += 1
            market = market_for_code(code)
            dates = market_dates.get(market, [])
            history = histories.get(code)
            if not history or not history.get("bars"):
                stats["missing_history"] += 1
                excluded_codes["missing_history"].append(code)
                continue
            bars = sorted(history["bars"], key=lambda item: item["date"])
            by_date = {bar["date"]: bar for bar in bars}
            if any(day not in by_date for day in dates):
                stats["stale_history"] += 1
                excluded_codes["stale_history"].append(code)
                continue
            if history.get("adjustment") == "raw_bj":
                stats["unadjusted_history"] += 1
                excluded_codes["unadjusted_history"].append(code)
                continue
            quote = quotes.get(code)
            if not quote:
                stats["missing_quote"] += 1
                excluded_codes["missing_quote"].append(code)
                continue
            if not str(quote.get("quote_at", "")).startswith(dates[-1]):
                stats["stale_quote"] += 1
                excluded_codes["stale_quote"].append(code)
                continue
            cap = quote.get("market_cap")
            if cap is None and market == "CN":
                cap = quote.get("market_cap_yuan")
            if cap is None:
                stats["missing_cap"] += 1
                excluded_codes["missing_cap"].append(code)
            daily = add_moving_averages(bars[-100:], (5, 10, 60))
            weekly = add_moving_averages(to_weekly_bars(bars[-130:]), (5, 10))
            rows.append({
                "code": code, "name": quote["name"], "group": group,
                "return_pct": round(five_day_return([by_date[day] for day in dates]), 3),
                "last_price": float(quote["last_price"]), "market_cap": cap,
                "market_cap_yuan": cap if market == "CN" else None,
                "market": market, "currency": quote.get("currency") or ("HKD" if market == "HK" else "CNY"),
                "market_cap_currency": quote.get("market_cap_currency") or ("HKD" if market == "HK" else "CNY"),
                "window_start": dates[0], "as_of": dates[-1],
                "quote_at": quote.get("quote_at"), "quote_source": quote.get("quote_source"),
                "quote_url": quote.get("quote_url"), "adjustment": history["adjustment"],
                "daily": daily, "weekly": weekly,
                "history_url": history.get("history_url") or f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_symbol(code)},day,,,260,qfq",
            })
            stats["eligible"] += 1
    latest_as_of = max(window["as_of"] for window in market_windows.values())
    earliest_start = min(window["window_start"] for window in market_windows.values())
    return {
        "generated_at": generated_at, "as_of": latest_as_of, "window_start": earliest_start,
        "market_windows": market_windows,
        "method": "first session open to fifth session close",
        "groups": select_rankings(rows), "stats": stats, "excluded_codes": excluded_codes,
    }


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    groups = snapshot.get("groups", {})
    if not groups or not any(side for group in groups.values() for side in group.values()):
        raise ValueError("snapshot has no rankings")
    if not snapshot.get("as_of") or not snapshot.get("window_start"):
        raise ValueError("snapshot dates missing")
    stats = snapshot.get("stats", {})
    if stats.get("universe", 0) and stats.get("eligible", 0) / stats["universe"] < 0.98:
        raise ValueError("snapshot coverage below 98%; preserving previous result")


def write_snapshot(snapshot: dict[str, Any], target: str | Path) -> None:
    """Replace a successful snapshot atomically; never wipe a prior one on failure."""
    validate_snapshot(snapshot)
    path = Path(target)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_products(products: dict[str, dict[str, str]], target: str | Path) -> None:
    if not products or not all(item.get("product") for item in products.values()):
        raise ValueError("product catalog is empty or invalid")
    path = Path(target)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(products, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


UNIVERSE_FILES = {
    "基础化工": "基础化工list.txt",
    "石油石化": "石油石化list.txt",
    "北交所": "北交所.txt",
    "港股": "H股list.txt",
}


def load_universe(root: str | Path) -> dict[str, list[str]]:
    root = Path(root)
    output: dict[str, list[str]] = {}
    for group, filename in UNIVERSE_FILES.items():
        codes = []
        for line in (root / filename).read_text(encoding="utf-8-sig").splitlines():
            try:
                code = normalize_code(line)
            except ValueError:
                continue
            if code not in codes:
                codes.append(code)
        output[group] = codes
    return output


def fetch_market_data(codes: list[str], workers: int = 10) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Fetch Tencent adjusted history and same-provider quotes with bounded retries."""
    import requests

    def fetch_one(code: str) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, str | None]:
        symbol = tencent_symbol(code)
        history_url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,260,qfq"
        quote_url = f"https://qt.gtimg.cn/q={symbol}"
        session = requests.Session()
        session.trust_env = False
        errors = []
        for attempt in range(3):
            try:
                if code.endswith(".HK"):
                    h = session.get(EASTMONEY_HK_HISTORY_URL, params={
                        "secid": f"116.{code.split('.')[0]}",
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                        "klt": "101", "fqt": "1", "end": "20500000", "lmt": "260",
                    }, timeout=16)
                    h.raise_for_status()
                    history = parse_eastmoney_hk_history(h.json(), code)
                else:
                    h = session.get(history_url, timeout=16)
                    h.raise_for_status()
                    history = parse_tencent_history(h.json(), code)
                if code.endswith(".BJ") and len(history["bars"]) < 60:
                    sina = session.get("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData",
                                       params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": "260"}, timeout=16)
                    sina.raise_for_status()
                    history = parse_sina_bj_history(sina.json())
                q = session.get(quote_url, timeout=12)
                q.raise_for_status()
                quote = parse_tencent_quote(q.content.decode("gbk", errors="replace"), code)
                return code, history, quote, None
            except (requests.RequestException, ValueError, KeyError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                time.sleep(0.25 * (attempt + 1))
        return code, None, None, "; ".join(errors[-2:])

    histories: dict[str, dict[str, Any]] = {}
    quotes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, code): code for code in codes}
        for future in as_completed(futures):
            code, history, quote, error = future.result()
            if history:
                histories[code] = history
            if quote:
                quotes[code] = quote
            if error:
                errors[code] = error
    return histories, quotes, errors


FUTURES_URL = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20data=/InnerFuturesNewService.getDailyKLine"


def product_trend_is_recent(trend_date: str, stock_date: str) -> bool:
    lag = date.fromisoformat(stock_date) - date.fromisoformat(trend_date)
    return timedelta(0) <= lag <= timedelta(days=7)


def summarize_futures_trend(rows: list[dict[str, Any]], symbol: str, label: str) -> dict[str, str]:
    bars = sorted(rows, key=lambda row: row["d"])[-5:]
    if len(bars) != 5:
        raise ValueError(f"five futures daily bars required for {symbol}")
    first, last = float(bars[0]["o"]), float(bars[-1]["c"])
    if first <= 0:
        raise ValueError(f"invalid futures open for {symbol}")
    change = (last / first - 1) * 100
    direction = "上涨" if change > .1 else "下跌" if change < -.1 else "震荡持平"
    return {"trend": f"{label}近5个交易日{direction}{abs(change):.2f}%（首日开盘至末日收盘）；仅作关联参考，不等同公司产品售价。",
            "as_of": str(bars[-1]["d"]), "url": f"{FUTURES_URL}?symbol={symbol}"}


def build_products(catalog: dict[str, dict[str, str]], as_of: str) -> dict[str, dict[str, str]]:
    import requests
    session = requests.Session()
    session.trust_env = False
    trends: dict[str, dict[str, str]] = {}
    for symbol in sorted({item["futures"] for item in catalog.values() if item.get("futures")}):
        try:
            response = session.get(FUTURES_URL, params={"symbol": symbol}, timeout=15)
            response.raise_for_status()
            match = re.search(r"\(\s*(\[.*\])\s*\)\s*;?\s*$", response.text, re.S)
            if not match:
                raise ValueError("invalid futures JSONP")
            trend = summarize_futures_trend(json.loads(match.group(1)), symbol, "")
            if product_trend_is_recent(trend["as_of"], as_of):
                trends[symbol] = trend
        except (requests.RequestException, ValueError, KeyError):
            continue
    products = {}
    for code, item in catalog.items():
        reference = item.get("reference", "")
        trend = trends.get(item.get("futures", ""))
        products[code] = {"product": item["product"],
                          "product_url": f"https://basic.10jqka.com.cn/{code.split('.')[0]}/operate.html",
                          "trend": trend["trend"].replace("近5", f"{reference}截至{trend['as_of']}近5") if trend else "近期品种日线趋势未取得；未发现明确可核实的连续日线序列。",
                          "trend_url": trend["url"] if trend else ""}
    return products


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Refresh the five-session A-share site snapshot")
    parser.add_argument("--products-only", action="store_true", help="refresh product references for the existing snapshot")
    parser.add_argument("--force", action="store_true", help="replace a same-session snapshot after a data repair")
    args = parser.parse_args()
    root = Path(__file__).parent
    target = root / "snapshot.json"
    catalog = json.loads((root / "product_catalog.json").read_text(encoding="utf-8"))
    if args.products_only:
        old = json.loads(target.read_text(encoding="utf-8"))
        products = build_products(catalog, old["as_of"])
        write_products(products, target.parent / "products.json")
        print(json.dumps({"products": len(products), "as_of": old["as_of"]}, ensure_ascii=False))
        return 0
    if target.exists() and datetime.now(timezone(timedelta(hours=8))).weekday() >= 5 and not args.force:
        old = json.loads(target.read_text(encoding="utf-8"))
        print(json.dumps({"snapshot": str(target), "as_of": old["as_of"], "unchanged": "weekend"}, ensure_ascii=False))
        return 0
    universe = load_universe(root)
    codes = sorted({code for group_codes in universe.values() for code in group_codes})
    histories, quotes, errors = fetch_market_data(codes)
    for _ in range(2):
        if not errors:
            break
        time.sleep(1)
        retry_histories, retry_quotes, retry_errors = fetch_market_data(list(errors), workers=3)
        histories.update(retry_histories)
        quotes.update(retry_quotes)
        errors = retry_errors
    fallback_codes = sorted(set(errors) | {code for code in codes if code.endswith(".BJ")})
    if fallback_codes:
        fallback_histories, fallback_quotes, fallback_errors = fetch_sina_adjusted_fallback(fallback_codes)
        histories.update(fallback_histories)
        quotes.update(fallback_quotes)
        errors = fallback_errors
    generated_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    snapshot = make_snapshot(universe, histories, quotes, generated_at)
    snapshot["stats"]["fetch_errors"] = len(errors)
    snapshot["fetch_error_codes"] = sorted(errors)
    snapshot["source_notes"] = [
        "行情、总股本：腾讯行情接口；市值=最新价×总股本。",
        "沪深日线优先用腾讯前复权序列，缺失时及北交所日线改用新浪前复权序列；未复权历史不得纳入排名。",
        "所有收益按同一组最近五个交易日的首日开盘至第五日收盘计算。",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not args.force:
        old = json.loads(target.read_text(encoding="utf-8"))
        if old.get("as_of") >= snapshot["as_of"]:
            print(json.dumps({"snapshot": str(target), "as_of": old["as_of"], "unchanged": "no new session"}, ensure_ascii=False))
            return 0
    products = build_products(catalog, snapshot["as_of"])
    validate_snapshot(snapshot)
    write_products(products, target.parent / "products.json")
    write_snapshot(snapshot, target)
    print(json.dumps({"snapshot": str(target), "as_of": snapshot["as_of"],
                      "stats": snapshot["stats"], "errors_sample": dict(list(errors.items())[:12])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
