"""Publish a validated A-share five-session bundle from a cloud runner."""

from __future__ import annotations

import argparse
from datetime import datetime, time as clock_time, timedelta, timezone
import json
import os
from pathlib import Path
import time
from typing import Any

import daily_site


CST = timezone(timedelta(hours=8), name="Asia/Shanghai")
RANK_COUNTS = {
    "chem_large": {"gainers": 3, "losers": 3},
    "chem_small": {"gainers": 5, "losers": 5},
    "oil": {"gainers": 5, "losers": 5},
    "bj": {"gainers": 3, "losers": 3},
}


class SnapshotCoverageError(ValueError):
    """A retryable snapshot failure caused by temporarily incomplete market data."""

    def __init__(self, snapshot: dict[str, Any], errors: dict[str, str]):
        super().__init__("snapshot coverage below 98%; preserving previous result")
        self.diagnostics = {
            "as_of": snapshot.get("as_of"),
            "stats": snapshot.get("stats", {}),
            "excluded_codes": snapshot.get("excluded_codes", {}),
            "fetch_error_codes": sorted(errors),
        }


def now_china() -> datetime:
    return datetime.now(CST)


def validate_bundle(bundle: dict[str, Any]) -> None:
    snapshot = bundle.get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(bundle.get("products"), dict):
        raise ValueError("bundle requires snapshot and products")
    daily_site.validate_snapshot(snapshot)
    windows = snapshot.get("market_windows", {})
    for market in ("CN",):
        window = windows.get(market, {})
        if not window.get("as_of") or not window.get("window_start"):
            raise ValueError(f"missing {market} market window")
    for bucket, required_by_side in RANK_COUNTS.items():
        group = snapshot.get("groups", {}).get(bucket, {})
        for side in ("gainers", "losers"):
            rows = group.get(side)
            required = required_by_side[side]
            if not isinstance(rows, list) or len(rows) != required:
                raise ValueError(f"incomplete rankings: {bucket}/{side} requires {required}")
            for row in rows:
                market = row.get("market") or "CN"
                as_of = windows[market]["as_of"]
                if row.get("as_of") not in {None, as_of}:
                    raise ValueError(f"wrong market window in {bucket}/{side}: {row.get('code')}")
                if not str(row.get("quote_at", "")).startswith(as_of):
                    raise ValueError(f"stale quote in {bucket}/{side}: {row.get('code')}")
                if row.get("adjustment") not in {"qfq", "qfq_sina", "qfq_sina_live"}:
                    raise ValueError(f"unadjusted history in {bucket}/{side}: {row.get('code')}")
                if bucket == "bj" and row.get("adjustment") not in {"qfq_sina", "qfq_sina_live"}:
                    raise ValueError(f"BJ adjusted history required: {row.get('code')}")
                if not row.get("daily") or row["daily"][-1].get("date") != as_of or not row.get("weekly"):
                    raise ValueError(f"incomplete chart history in {bucket}/{side}: {row.get('code')}")


def should_publish(as_of: str, previous_as_of: str, current: datetime) -> bool:
    return bool(as_of and as_of > previous_as_of and as_of <= current.astimezone(CST).date().isoformat())


def seconds_until_release(current: datetime) -> int:
    local = current.astimezone(CST)
    release = datetime.combine(local.date(), clock_time(16, 10), tzinfo=CST)
    return max(0, int((release - local).total_seconds()))


def write_bundle(bundle: dict[str, Any], target: str | Path) -> None:
    validate_bundle(bundle)
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_sina_with_retries(codes: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    histories: dict[str, dict[str, Any]] = {}
    quotes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    remaining = codes
    for attempt in range(3):
        if not remaining:
            break
        fetched_histories, fetched_quotes, errors = daily_site.fetch_sina_adjusted_fallback(remaining)
        histories.update(fetched_histories)
        quotes.update(fetched_quotes)
        remaining = sorted(errors)
        if remaining and attempt < 2:
            time.sleep(0.75)
    return histories, quotes, errors


def build_bundle(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    universe = daily_site.load_universe(root)
    codes = sorted({code for group in universe.values() for code in group})
    histories, quotes, errors = daily_site.fetch_market_data(codes)
    for _ in range(1):
        if not errors:
            break
        time.sleep(1)
        retry_histories, retry_quotes, retry_errors = daily_site.fetch_market_data(list(errors), workers=8)
        histories.update(retry_histories)
        quotes.update(retry_quotes)
        errors = retry_errors
    snapshot = daily_site.make_snapshot(universe, histories, quotes, now_china().strftime("%Y-%m-%d %H:%M:%S"))
    snapshot["stats"]["fetch_errors"] = len(errors)
    snapshot["fetch_error_codes"] = sorted(errors)
    snapshot["source_notes"] = [
        "A股实时行情、总股本：腾讯行情接口；市值=最新价×总股本。",
        "A股日线使用新浪前复权序列；若当日日线延迟，仅在腾讯报价确认开盘、最高、最低和成交量均有效时补入当日行情。",
        "当日停牌或零成交股票不参与排名，并从有效覆盖率分母剔除；未复权历史不得纳入排名。",
        "A股使用最近五个交易日，收益按首日开盘至第五日收盘计算。",
    ]
    stats = snapshot.get("stats", {})
    active_universe = stats.get("universe", 0) - stats.get("no_trade", 0)
    if active_universe and stats.get("eligible", 0) / active_universe < 0.98:
        raise SnapshotCoverageError(snapshot, errors)
    daily_site.validate_snapshot(snapshot)
    catalog = json.loads((root / "product_catalog.json").read_text(encoding="utf-8"))
    products = daily_site.build_products(catalog, snapshot["as_of"])
    bundle = {"snapshot": snapshot, "products": products}
    validate_bundle(bundle)
    return bundle, errors


def build_bundle_with_retries(
    root: Path,
    *,
    now_fn=None,
    sleep_fn=None,
    builder=None,
    log_fn=None,
    retry_seconds: int = 300,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Retry only temporary coverage failures every five minutes through 16:40."""
    now_fn = now_fn or now_china
    sleep_fn = sleep_fn or time.sleep
    builder = builder or build_bundle
    log_fn = log_fn or (lambda message: print(message, flush=True))
    attempt = 0
    while True:
        attempt += 1
        try:
            return builder(root)
        except SnapshotCoverageError as exc:
            log_fn(json.dumps({"event": "coverage_retry", "attempt": attempt, **exc.diagnostics},
                              ensure_ascii=False))
            local = now_fn().astimezone(CST)
            deadline = datetime.combine(local.date(), clock_time(16, 40), tzinfo=CST)
            release = datetime.combine(local.date(), clock_time(16, 10), tzinfo=CST)
            if local < release:
                next_attempt = release
            else:
                elapsed = (local - release).total_seconds()
                next_attempt = release + timedelta(seconds=(int(elapsed // retry_seconds) + 1) * retry_seconds)
            next_attempt = min(next_attempt, deadline)
            remaining = int((next_attempt - local).total_seconds())
            if remaining <= 0:
                raise
            sleep_fn(min(retry_seconds, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the public five-session A-share data bundle")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("latest.json"))
    args = parser.parse_args()
    delay = seconds_until_release(now_china())
    if delay:
        print(f"Waiting {delay} seconds until 16:10 Asia/Shanghai before collecting market data", flush=True)
        time.sleep(delay)
    print(json.dumps({"event": "collection_start", "at": now_china().isoformat()}, ensure_ascii=False), flush=True)
    bundle, errors = build_bundle_with_retries(Path(__file__).parent)
    snapshot = bundle["snapshot"]
    previous_as_of = ""
    if args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        previous_as_of = previous.get("snapshot", {}).get("as_of", "")
    result = {"as_of": snapshot["as_of"], "previous_as_of": previous_as_of,
              "stats": snapshot["stats"], "fetch_error_codes": sorted(errors)}
    if not should_publish(snapshot["as_of"], previous_as_of, now_china()):
        result["unchanged"] = "no new trading session"
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    write_bundle(bundle, args.output)
    result["published"] = str(args.output)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
