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
RANK_COUNTS = {"chem_large": 3, "chem_small": 5, "oil": 5, "bj": 3}


def now_china() -> datetime:
    return datetime.now(CST)


def validate_bundle(bundle: dict[str, Any]) -> None:
    snapshot = bundle.get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(bundle.get("products"), dict):
        raise ValueError("bundle requires snapshot and products")
    daily_site.validate_snapshot(snapshot)
    as_of = snapshot["as_of"]
    for bucket, required in RANK_COUNTS.items():
        group = snapshot.get("groups", {}).get(bucket, {})
        for side in ("gainers", "losers"):
            rows = group.get(side)
            if not isinstance(rows, list) or len(rows) != required:
                raise ValueError(f"incomplete rankings: {bucket}/{side} requires {required}")
            for row in rows:
                if not str(row.get("quote_at", "")).startswith(as_of):
                    raise ValueError(f"stale quote in {bucket}/{side}: {row.get('code')}")
                if row.get("adjustment") not in {"qfq", "qfq_sina"}:
                    raise ValueError(f"unadjusted history in {bucket}/{side}: {row.get('code')}")
                if bucket == "bj" and row.get("adjustment") != "qfq_sina":
                    raise ValueError(f"BJ adjusted history required: {row.get('code')}")
                if not row.get("daily") or row["daily"][-1].get("date") != as_of or not row.get("weekly"):
                    raise ValueError(f"incomplete chart history in {bucket}/{side}: {row.get('code')}")


def should_publish(as_of: str, previous_as_of: str, current: datetime) -> bool:
    return bool(as_of and as_of > previous_as_of and as_of <= current.astimezone(CST).date().isoformat())


def seconds_until_release(current: datetime) -> int:
    local = current.astimezone(CST)
    release = datetime.combine(local.date(), clock_time(16, 0), tzinfo=CST)
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
    for _ in range(2):
        if not errors:
            break
        time.sleep(1)
        retry_histories, retry_quotes, retry_errors = daily_site.fetch_market_data(list(errors), workers=3)
        histories.update(retry_histories)
        quotes.update(retry_quotes)
        errors = retry_errors
    fallback_codes = sorted(set(errors) | {code for code in codes if code.endswith(".BJ")})
    if fallback_codes:
        fallback_histories, fallback_quotes, fallback_errors = fetch_sina_with_retries(fallback_codes)
        histories.update(fallback_histories)
        quotes.update(fallback_quotes)
        errors = fallback_errors
    snapshot = daily_site.make_snapshot(universe, histories, quotes, now_china().strftime("%Y-%m-%d %H:%M:%S"))
    snapshot["stats"]["fetch_errors"] = len(errors)
    snapshot["fetch_error_codes"] = sorted(errors)
    snapshot["source_notes"] = [
        "行情、总股本：腾讯行情接口；市值=最新价×总股本。",
        "沪深日线优先用腾讯前复权序列，缺失时及北交所日线改用新浪前复权序列；未复权历史不得纳入排名。",
        "所有收益按同一组最近五个交易日的首日开盘至第五日收盘计算。",
    ]
    daily_site.validate_snapshot(snapshot)
    catalog = json.loads((root / "product_catalog.json").read_text(encoding="utf-8"))
    products = daily_site.build_products(catalog, snapshot["as_of"])
    bundle = {"snapshot": snapshot, "products": products}
    validate_bundle(bundle)
    return bundle, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the public five-session A-share data bundle")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("latest.json"))
    args = parser.parse_args()
    bundle, errors = build_bundle(Path(__file__).parent)
    snapshot = bundle["snapshot"]
    previous_as_of = ""
    if args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        previous_as_of = previous.get("snapshot", {}).get("as_of", "")
    result = {"as_of": snapshot["as_of"], "previous_as_of": previous_as_of,
              "stats": snapshot["stats"], "fetch_error_codes": sorted(errors)}
    if not should_publish(snapshot["as_of"], previous_as_of, now_china()):
        result["unchanged"] = "no new trading session"
        print(json.dumps(result, ensure_ascii=False))
        return 0
    delay = seconds_until_release(now_china())
    if delay:
        print(f"Validated; waiting {delay} seconds until 16:00 Asia/Shanghai", flush=True)
        time.sleep(delay)
    write_bundle(bundle, args.output)
    result["published"] = str(args.output)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
