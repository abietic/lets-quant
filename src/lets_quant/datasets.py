from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .data import DataError, load_prices
from .models import CorporateAction, MarketData, Policy
from .research import ResearchPolicy, load_research_policy
from .snapshots import (
    RawSnapshot,
    canonical_json_bytes,
    file_sha256,
    load_snapshot,
)


DATASET_SCHEMA_VERSION = 2
SUPPORTED_DATASET_SCHEMA_VERSIONS = {1, DATASET_SCHEMA_VERSION}
BAR_COLUMNS = {
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "available_at",
    "adjustment",
}
CALENDAR_COLUMNS = {"date", "is_open", "available_at"}
INSTRUMENT_COLUMNS = {
    "symbol",
    "exchange",
    "asset_type",
    "listed_on",
    "delisted_on",
    "available_at",
}
SUSPENSION_COLUMNS = {"date", "symbol", "available_at"}
ACTION_COLUMNS = {
    "symbol",
    "event_type",
    "ex_date",
    "announced_at",
    "cash_amount",
    "ratio",
    "available_at",
}
OBSERVATION_COLUMNS = {
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "tradable",
    "available_at",
    "source_snapshot_id",
    "adjustment",
}


@dataclass(frozen=True)
class CuratedDataset:
    directory: Path
    manifest: Dict[str, Any]
    market: MarketData

    @property
    def dataset_id(self) -> str:
        return str(self.manifest["dataset_id"])


@dataclass(frozen=True)
class DatasetBuildResult:
    directory: Path
    dataset_id: str
    status: str
    quality_report: Dict[str, Any]


def parse_timestamp(value: str, path: str = "timestamp") -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DataError(f"{path} must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise DataError(f"{path} must include a timezone")
    return result


def _parse_date(value: str, path: str, *, allow_empty: bool = False) -> Optional[date]:
    normalized = value.strip()
    if allow_empty and not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise DataError(f"{path} must be YYYY-MM-DD") from exc


def _parse_float(
    value: str,
    path: str,
    *,
    minimum: Optional[float] = None,
    allow_empty: bool = False,
) -> Optional[float]:
    normalized = value.strip()
    if allow_empty and not normalized:
        return None
    try:
        result = float(normalized)
    except ValueError as exc:
        raise DataError(f"{path} must be a number") from exc
    if not math.isfinite(result):
        raise DataError(f"{path} must be finite")
    if minimum is not None and result < minimum:
        raise DataError(f"{path} must be >= {minimum}")
    return result


def _read_csv(
    path: Path, expected_columns: Set[str]
) -> List[tuple[int, Dict[str, str]]]:
    try:
        handle = path.open(newline="", encoding="utf-8")
    except FileNotFoundError as exc:
        raise DataError(f"data input not found: {path}") from exc
    with handle:
        reader = csv.DictReader(handle)
        actual = set(reader.fieldnames or [])
        if actual != expected_columns:
            raise DataError(
                f"{path} must have exactly these columns: "
                f"{', '.join(sorted(expected_columns))}"
            )
        return [
            (line_number, dict(row))
            for line_number, row in enumerate(reader, start=2)
        ]


def _quality_check(
    checks: List[Dict[str, Any]],
    *,
    name: str,
    passed: bool,
    message: str,
    severity: str = "error",
    count: Optional[int] = None,
) -> None:
    if passed:
        status = "pass"
    elif severity == "warning":
        status = "warning"
    else:
        status = "fail"
    check: Dict[str, Any] = {
        "name": name,
        "status": status,
        "severity": severity,
        "message": message,
    }
    if count is not None:
        check["count"] = count
    checks.append(check)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(
    path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _parse_bars(
    snapshot: RawSnapshot,
    policy: ResearchPolicy,
    as_of: datetime,
    checks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    future_count = 0
    duplicates: Set[tuple[date, str]] = set()
    duplicate_keys: Set[tuple[date, str]] = set()
    out_of_scope: Set[str] = set()
    adjustment_mismatches: Set[str] = set()
    invalid_ohlc = 0
    early_availability = 0

    for line_number, row in _read_csv(snapshot.payload_path, BAR_COLUMNS):
        prefix = f"{snapshot.payload_path}:{line_number}"
        trading_date = _parse_date(row["date"], f"{prefix}:date")
        assert trading_date is not None
        symbol = row["symbol"].strip().upper()
        if not symbol:
            raise DataError(f"{prefix}:symbol must not be empty")
        available_at = parse_timestamp(
            row["available_at"], f"{prefix}:available_at"
        )
        if available_at > as_of:
            future_count += 1
            continue
        if trading_date < policy.horizon.start_date:
            continue
        if available_at.date() < trading_date:
            early_availability += 1

        key = (trading_date, symbol)
        if key in duplicates:
            duplicate_keys.add(key)
        duplicates.add(key)
        if symbol not in policy.symbols:
            out_of_scope.add(symbol)

        adjustment = row["adjustment"].strip().lower()
        if adjustment != policy.adjustment:
            adjustment_mismatches.add(adjustment or "<empty>")

        open_price = _parse_float(row["open"], f"{prefix}:open", minimum=0.0)
        high = _parse_float(row["high"], f"{prefix}:high", minimum=0.0)
        low = _parse_float(row["low"], f"{prefix}:low", minimum=0.0)
        close = _parse_float(row["close"], f"{prefix}:close", minimum=0.0)
        volume = _parse_float(row["volume"], f"{prefix}:volume", minimum=0.0)
        amount = _parse_float(row["amount"], f"{prefix}:amount", minimum=0.0)
        assert open_price is not None
        assert high is not None
        assert low is not None
        assert close is not None
        assert volume is not None
        assert amount is not None
        if (
            open_price <= 0
            or high <= 0
            or low <= 0
            or close <= 0
            or low > min(open_price, close)
            or high < max(open_price, close)
            or low > high
        ):
            invalid_ohlc += 1

        parsed.append(
            {
                "date": trading_date,
                "symbol": symbol,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "available_at": available_at,
                "adjustment": adjustment,
            }
        )

    _quality_check(
        checks,
        name="point_in_time_filter",
        passed=True,
        message=(
            f"excluded {future_count} rows unavailable at the requested as-of"
        ),
        count=future_count,
    )
    _quality_check(
        checks,
        name="duplicate_bar_keys",
        passed=not duplicate_keys,
        message=(
            "no duplicate date/symbol bars"
            if not duplicate_keys
            else f"found {len(duplicate_keys)} duplicate date/symbol bars"
        ),
        count=len(duplicate_keys),
    )
    _quality_check(
        checks,
        name="research_universe",
        passed=not out_of_scope,
        message=(
            "all bar symbols are in the frozen research universe"
            if not out_of_scope
            else "out-of-scope symbols: " + ", ".join(sorted(out_of_scope))
        ),
        count=len(out_of_scope),
    )
    _quality_check(
        checks,
        name="adjustment_convention",
        passed=not adjustment_mismatches,
        message=(
            f"all bars use adjustment={policy.adjustment}"
            if not adjustment_mismatches
            else "unexpected adjustment values: "
            + ", ".join(sorted(adjustment_mismatches))
        ),
        count=len(adjustment_mismatches),
    )
    _quality_check(
        checks,
        name="ohlc_consistency",
        passed=invalid_ohlc == 0,
        message=(
            "all OHLC values are positive and internally consistent"
            if invalid_ohlc == 0
            else f"found {invalid_ohlc} invalid OHLC rows"
        ),
        count=invalid_ohlc,
    )
    _quality_check(
        checks,
        name="bar_availability_order",
        passed=early_availability == 0,
        message=(
            "bar availability is not earlier than its market date"
            if early_availability == 0
            else f"found {early_availability} bars available before market date"
        ),
        count=early_availability,
    )
    return parsed


def _parse_calendar(path: Path, as_of: datetime) -> Dict[date, bool]:
    calendar: Dict[date, bool] = {}
    for line_number, row in _read_csv(path, CALENDAR_COLUMNS):
        prefix = f"{path}:{line_number}"
        available_at = parse_timestamp(
            row["available_at"], f"{prefix}:available_at"
        )
        if available_at > as_of:
            continue
        trading_date = _parse_date(row["date"], f"{prefix}:date")
        assert trading_date is not None
        normalized_open = row["is_open"].strip().lower()
        if normalized_open not in {"true", "false"}:
            raise DataError(f"{prefix}:is_open must be true or false")
        if trading_date in calendar:
            raise DataError(f"{prefix}:duplicate calendar date")
        calendar[trading_date] = normalized_open == "true"
    return calendar


def _parse_instruments(
    path: Path, as_of: datetime
) -> Dict[str, Dict[str, Any]]:
    instruments: Dict[str, Dict[str, Any]] = {}
    for line_number, row in _read_csv(path, INSTRUMENT_COLUMNS):
        prefix = f"{path}:{line_number}"
        available_at = parse_timestamp(
            row["available_at"], f"{prefix}:available_at"
        )
        if available_at > as_of:
            continue
        symbol = row["symbol"].strip().upper()
        if not symbol:
            raise DataError(f"{prefix}:symbol must not be empty")
        if symbol in instruments:
            raise DataError(f"{prefix}:duplicate instrument symbol")
        listed_on = _parse_date(row["listed_on"], f"{prefix}:listed_on")
        delisted_on = _parse_date(
            row["delisted_on"], f"{prefix}:delisted_on", allow_empty=True
        )
        assert listed_on is not None
        if delisted_on is not None and delisted_on < listed_on:
            raise DataError(f"{prefix}:delisted_on is earlier than listed_on")
        instruments[symbol] = {
            "symbol": symbol,
            "exchange": row["exchange"].strip().upper(),
            "asset_type": row["asset_type"].strip().upper(),
            "listed_on": listed_on,
            "delisted_on": delisted_on,
            "available_at": available_at,
        }
    return instruments


def _parse_suspensions(
    path: Path, as_of: datetime
) -> List[Dict[str, Any]]:
    suspensions: List[Dict[str, Any]] = []
    seen: Set[tuple[date, str]] = set()
    for line_number, row in _read_csv(path, SUSPENSION_COLUMNS):
        prefix = f"{path}:{line_number}"
        available_at = parse_timestamp(
            row["available_at"], f"{prefix}:available_at"
        )
        if available_at > as_of:
            continue
        trading_date = _parse_date(row["date"], f"{prefix}:date")
        assert trading_date is not None
        symbol = row["symbol"].strip().upper()
        key = (trading_date, symbol)
        if not symbol:
            raise DataError(f"{prefix}:symbol must not be empty")
        if key in seen:
            raise DataError(f"{prefix}:duplicate suspension")
        seen.add(key)
        suspensions.append(
            {
                "date": trading_date,
                "symbol": symbol,
                "available_at": available_at,
            }
        )
    return suspensions


def _parse_actions(path: Path, as_of: datetime) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    seen: Set[tuple[str, str, date]] = set()
    allowed_types = {"cash_dividend", "split", "reverse_split"}
    for line_number, row in _read_csv(path, ACTION_COLUMNS):
        prefix = f"{path}:{line_number}"
        available_at = parse_timestamp(
            row["available_at"], f"{prefix}:available_at"
        )
        if available_at > as_of:
            continue
        announced_at = parse_timestamp(
            row["announced_at"], f"{prefix}:announced_at"
        )
        if announced_at > available_at:
            raise DataError(f"{prefix}:announced_at is after available_at")
        symbol = row["symbol"].strip().upper()
        event_type = row["event_type"].strip().lower()
        ex_date = _parse_date(row["ex_date"], f"{prefix}:ex_date")
        assert ex_date is not None
        if not symbol:
            raise DataError(f"{prefix}:symbol must not be empty")
        if event_type not in allowed_types:
            raise DataError(f"{prefix}:unsupported event_type {event_type!r}")
        key = (symbol, event_type, ex_date)
        if key in seen:
            raise DataError(f"{prefix}:duplicate corporate action")
        seen.add(key)
        cash_amount = _parse_float(
            row["cash_amount"],
            f"{prefix}:cash_amount",
            minimum=0.0,
            allow_empty=True,
        )
        ratio = _parse_float(
            row["ratio"], f"{prefix}:ratio", minimum=0.0, allow_empty=True
        )
        if event_type == "cash_dividend" and cash_amount is None:
            raise DataError(
                f"{prefix}:cash_dividend requires cash_amount"
            )
        if event_type in {"split", "reverse_split"} and (
            ratio is None or ratio <= 0
        ):
            raise DataError(f"{prefix}:{event_type} requires ratio > 0")
        if event_type == "split" and ratio is not None and ratio <= 1:
            raise DataError(f"{prefix}:split requires ratio > 1")
        if event_type == "reverse_split" and ratio is not None and ratio >= 1:
            raise DataError(f"{prefix}:reverse_split requires ratio < 1")
        actions.append(
            {
                "symbol": symbol,
                "event_type": event_type,
                "ex_date": ex_date,
                "announced_at": announced_at,
                "cash_amount": cash_amount,
                "ratio": ratio,
                "available_at": available_at,
            }
        )
    return actions


def _validate_reference_data(
    *,
    bars: List[Dict[str, Any]],
    calendar: Dict[date, bool],
    instruments: Dict[str, Dict[str, Any]],
    suspensions: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    policy: ResearchPolicy,
    checks: List[Dict[str, Any]],
) -> None:
    policy_by_symbol = {
        instrument.symbol: instrument for instrument in policy.instruments
    }
    metadata_errors: List[str] = []
    for symbol, expected in policy_by_symbol.items():
        actual = instruments.get(symbol)
        if actual is None:
            metadata_errors.append(f"{symbol}: missing")
            continue
        if actual["exchange"] != expected.exchange:
            metadata_errors.append(f"{symbol}: exchange mismatch")
        if actual["asset_type"] != expected.asset_type:
            metadata_errors.append(f"{symbol}: asset_type mismatch")
    extras = sorted(set(instruments) - policy.symbols)
    if extras:
        metadata_errors.append("extra symbols: " + ", ".join(extras))
    _quality_check(
        checks,
        name="instrument_master_scope",
        passed=not metadata_errors,
        message=(
            "instrument master matches the frozen research scope"
            if not metadata_errors
            else "; ".join(metadata_errors)
        ),
        count=len(metadata_errors),
    )

    unknown_calendar_dates = sorted(
        {
            bar["date"]
            for bar in bars
            if bar["date"] not in calendar or not calendar[bar["date"]]
        }
    )
    _quality_check(
        checks,
        name="trading_calendar_alignment",
        passed=not unknown_calendar_dates,
        message=(
            "all bars fall on known open trading days"
            if not unknown_calendar_dates
            else f"{len(unknown_calendar_dates)} bar dates are not known open days"
        ),
        count=len(unknown_calendar_dates),
    )

    lifecycle_errors = 0
    for bar in bars:
        instrument = instruments.get(bar["symbol"])
        if instrument is None:
            continue
        if bar["date"] < instrument["listed_on"] or (
            instrument["delisted_on"] is not None
            and bar["date"] > instrument["delisted_on"]
        ):
            lifecycle_errors += 1
    _quality_check(
        checks,
        name="listing_lifecycle",
        passed=lifecycle_errors == 0,
        message=(
            "all bars are within listing lifecycles"
            if lifecycle_errors == 0
            else f"found {lifecycle_errors} bars outside listing lifecycles"
        ),
        count=lifecycle_errors,
    )

    unknown_suspensions = sorted(
        {
            item["symbol"]
            for item in suspensions
            if item["symbol"] not in policy.symbols
        }
    )
    _quality_check(
        checks,
        name="suspension_scope",
        passed=not unknown_suspensions,
        message=(
            "all suspension records are in scope"
            if not unknown_suspensions
            else "out-of-scope suspension symbols: "
            + ", ".join(unknown_suspensions)
        ),
        count=len(unknown_suspensions),
    )

    unknown_actions = sorted(
        {
            item["symbol"]
            for item in actions
            if item["symbol"] not in policy.symbols
        }
    )
    _quality_check(
        checks,
        name="corporate_action_scope",
        passed=not unknown_actions,
        message=(
            "all corporate actions are in scope"
            if not unknown_actions
            else "out-of-scope corporate action symbols: "
            + ", ".join(unknown_actions)
        ),
        count=len(unknown_actions),
    )

    if bars:
        first_date = min(bar["date"] for bar in bars)
        last_date = max(bar["date"] for bar in bars)
        bar_keys = {(bar["date"], bar["symbol"]) for bar in bars}
        missing: List[tuple[date, str]] = []
        for trading_date, is_open in calendar.items():
            if not is_open or not first_date <= trading_date <= last_date:
                continue
            for symbol in sorted(policy.symbols):
                instrument = instruments.get(symbol)
                if instrument is None:
                    continue
                if trading_date < instrument["listed_on"]:
                    continue
                if (
                    instrument["delisted_on"] is not None
                    and trading_date > instrument["delisted_on"]
                ):
                    continue
                if (trading_date, symbol) not in bar_keys:
                    missing.append((trading_date, symbol))
        _quality_check(
            checks,
            name="open_day_price_completeness",
            passed=not missing,
            message=(
                "every in-scope instrument has a close on each open day"
                if not missing
                else f"missing {len(missing)} required open-day prices"
            ),
            count=len(missing),
        )
    else:
        _quality_check(
            checks,
            name="open_day_price_completeness",
            passed=False,
            message="no bars remain after point-in-time filtering",
            count=0,
        )

    relevant_actions = [
        action
        for action in actions
        if bars
        and min(bar["date"] for bar in bars)
        <= action["ex_date"]
        <= max(bar["date"] for bar in bars)
    ]
    _quality_check(
        checks,
        name="corporate_action_adjustment",
        passed=True,
        message=(
            f"{len(relevant_actions)} in-range actions are covered by "
            f"adjustment={policy.adjustment}"
            if policy.adjustment != "none"
            else f"{len(relevant_actions)} in-range actions will be posted "
            "through the explicit accounting ledger"
        ),
        count=len(relevant_actions),
    )
    if policy.adjustment == "qfq":
        _quality_check(
            checks,
            name="qfq_revision_risk",
            passed=False,
            severity="warning",
            message=(
                "qfq history can change after later corporate actions; "
                "the raw snapshot pins the observed revision"
            ),
        )

    trading_day_count = len({bar["date"] for bar in bars})
    _quality_check(
        checks,
        name="minimum_history",
        passed=(
            trading_day_count
            >= policy.horizon.minimum_history_trading_days
        ),
        severity="warning",
        message=(
            f"dataset has {trading_day_count} trading days; policy requests "
            f"{policy.horizon.minimum_history_trading_days}"
        ),
        count=trading_day_count,
    )


def _dataset_identity(
    *,
    snapshot: RawSnapshot,
    policy: ResearchPolicy,
    policy_path: Path,
    as_of: datetime,
    calendar_path: Path,
    instruments_path: Path,
    suspensions_path: Path,
    corporate_actions_path: Path,
) -> Dict[str, Any]:
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "builder": "lets_quant.curated_daily_bars.v2",
        "builder_source_sha256": _builder_source_sha256(),
        "source_snapshot_id": snapshot.snapshot_id,
        "research_policy_sha256": file_sha256(policy_path),
        "research_scope_sha256": hashlib.sha256(
            canonical_json_bytes(policy.to_dict())
        ).hexdigest(),
        "as_of": as_of.isoformat(),
        "inputs": {
            "calendar_sha256": file_sha256(calendar_path),
            "instruments_sha256": file_sha256(instruments_path),
            "suspensions_sha256": file_sha256(suspensions_path),
            "corporate_actions_sha256": file_sha256(corporate_actions_path),
        },
    }


def _builder_source_sha256() -> str:
    source_directory = Path(__file__).resolve().parent
    source_files = [
        source_directory / "data.py",
        source_directory / "datasets.py",
        source_directory / "research.py",
        source_directory / "snapshots.py",
    ]
    digest = hashlib.sha256()
    for path in source_files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _render_timestamp(value: datetime) -> str:
    return value.isoformat()


def build_curated_dataset(
    *,
    snapshot_path: Path,
    research_policy_path: Path,
    calendar_path: Path,
    instruments_path: Path,
    suspensions_path: Path,
    corporate_actions_path: Path,
    as_of: datetime,
    output_root: Path,
) -> DatasetBuildResult:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise DataError("as_of must include a timezone")
    snapshot = load_snapshot(snapshot_path)
    policy = load_research_policy(research_policy_path)
    checks: List[Dict[str, Any]] = []

    snapshot_fetched_at = parse_timestamp(
        str(snapshot.manifest["fetched_at"]),
        "snapshot.fetched_at",
    )
    locally_observed = snapshot_fetched_at <= as_of
    if policy.point_in_time_mode == "local_observation":
        _quality_check(
            checks,
            name="snapshot_observation_basis",
            passed=locally_observed,
            message=(
                "raw snapshot was locally observed by the requested as-of"
                if locally_observed
                else "raw snapshot was fetched after the requested as-of"
            ),
        )
        bars = (
            _parse_bars(snapshot, policy, as_of, checks)
            if locally_observed
            else []
        )
    else:
        _quality_check(
            checks,
            name="snapshot_observation_basis",
            passed=locally_observed,
            severity="warning",
            message=(
                "raw snapshot was locally observed by the requested as-of"
                if locally_observed
                else (
                    "using provider publication times from a retrospectively "
                    "fetched snapshot; this is not vendor vintage proof"
                )
            ),
        )
        bars = _parse_bars(snapshot, policy, as_of, checks)
    calendar = _parse_calendar(calendar_path, as_of)
    instruments = _parse_instruments(instruments_path, as_of)
    suspensions = _parse_suspensions(suspensions_path, as_of)
    actions = _parse_actions(corporate_actions_path, as_of)
    _validate_reference_data(
        bars=bars,
        calendar=calendar,
        instruments=instruments,
        suspensions=suspensions,
        actions=actions,
        policy=policy,
        checks=checks,
    )

    status = "fail" if any(
        check["status"] == "fail" for check in checks
    ) else "pass"
    identity = _dataset_identity(
        snapshot=snapshot,
        policy=policy,
        policy_path=research_policy_path,
        as_of=as_of,
        calendar_path=calendar_path,
        instruments_path=instruments_path,
        suspensions_path=suspensions_path,
        corporate_actions_path=corporate_actions_path,
    )
    suspension_keys = {
        (item["date"], item["symbol"]) for item in suspensions
    }
    bars.sort(key=lambda item: (item["date"], item["symbol"]))
    quality_payload: Dict[str, Any] = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "status": status,
        "summary": {
            "bar_rows": len(bars),
            "trading_days": len({bar["date"] for bar in bars}),
            "symbols": sorted({bar["symbol"] for bar in bars}),
            "suspension_rows": len(suspensions),
            "corporate_action_rows": len(actions),
            "point_in_time_mode": policy.point_in_time_mode,
        },
        "checks": checks,
    }

    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".building-", dir=output_root)
    )
    try:
        _write_csv(
            temporary / "observations.csv",
            [
                "date",
                "symbol",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "tradable",
                "available_at",
                "source_snapshot_id",
                "adjustment",
            ],
            (
                {
                    "date": bar["date"].isoformat(),
                    "symbol": bar["symbol"],
                    "open": f"{bar['open']:.10f}",
                    "high": f"{bar['high']:.10f}",
                    "low": f"{bar['low']:.10f}",
                    "close": f"{bar['close']:.10f}",
                    "volume": f"{bar['volume']:.10f}",
                    "amount": f"{bar['amount']:.10f}",
                    "tradable": str(
                        (bar["date"], bar["symbol"]) not in suspension_keys
                    ).lower(),
                    "available_at": _render_timestamp(bar["available_at"]),
                    "source_snapshot_id": snapshot.snapshot_id,
                    "adjustment": bar["adjustment"],
                }
                for bar in bars
            ),
        )
        _write_csv(
            temporary / "prices.csv",
            ["date", "symbol", "close"],
            (
                {
                    "date": bar["date"].isoformat(),
                    "symbol": bar["symbol"],
                    "close": f"{bar['close']:.10f}",
                }
                for bar in bars
            ),
        )
        _write_csv(
            temporary / "calendar.csv",
            ["date", "is_open"],
            (
                {
                    "date": trading_date.isoformat(),
                    "is_open": str(is_open).lower(),
                }
                for trading_date, is_open in sorted(calendar.items())
            ),
        )
        _write_csv(
            temporary / "instruments.csv",
            [
                "symbol",
                "exchange",
                "asset_type",
                "listed_on",
                "delisted_on",
                "available_at",
            ],
            (
                {
                    "symbol": item["symbol"],
                    "exchange": item["exchange"],
                    "asset_type": item["asset_type"],
                    "listed_on": item["listed_on"].isoformat(),
                    "delisted_on": (
                        item["delisted_on"].isoformat()
                        if item["delisted_on"] is not None
                        else ""
                    ),
                    "available_at": _render_timestamp(item["available_at"]),
                }
                for item in sorted(
                    instruments.values(), key=lambda value: value["symbol"]
                )
            ),
        )
        _write_csv(
            temporary / "suspensions.csv",
            ["date", "symbol", "available_at"],
            (
                {
                    "date": item["date"].isoformat(),
                    "symbol": item["symbol"],
                    "available_at": _render_timestamp(item["available_at"]),
                }
                for item in sorted(
                    suspensions,
                    key=lambda value: (value["date"], value["symbol"]),
                )
            ),
        )
        _write_csv(
            temporary / "corporate_actions.csv",
            [
                "symbol",
                "event_type",
                "ex_date",
                "announced_at",
                "cash_amount",
                "ratio",
                "available_at",
            ],
            (
                {
                    "symbol": item["symbol"],
                    "event_type": item["event_type"],
                    "ex_date": item["ex_date"].isoformat(),
                    "announced_at": _render_timestamp(item["announced_at"]),
                    "cash_amount": (
                        "" if item["cash_amount"] is None else item["cash_amount"]
                    ),
                    "ratio": "" if item["ratio"] is None else item["ratio"],
                    "available_at": _render_timestamp(item["available_at"]),
                }
                for item in sorted(
                    actions,
                    key=lambda value: (
                        value["ex_date"],
                        value["symbol"],
                        value["event_type"],
                    ),
                )
            ),
        )
        _write_json(
            temporary / "research_policy.snapshot.json", policy.to_dict()
        )

        protected_files = [
            "calendar.csv",
            "corporate_actions.csv",
            "instruments.csv",
            "observations.csv",
            "prices.csv",
            "research_policy.snapshot.json",
            "suspensions.csv",
        ]
        protected_hashes = {
            name: file_sha256(temporary / name) for name in protected_files
        }
        identity["curated_files"] = protected_hashes
        identity["quality_payload_sha256"] = hashlib.sha256(
            canonical_json_bytes(quality_payload)
        ).hexdigest()
        dataset_id = hashlib.sha256(
            canonical_json_bytes(identity)
        ).hexdigest()
        destination = output_root / dataset_id
        quality_report: Dict[str, Any] = {
            "dataset_id": dataset_id,
            **quality_payload,
        }
        if destination.exists():
            manifest, report = _validate_dataset_directory(
                destination, require_pass=False
            )
            return DatasetBuildResult(
                directory=destination,
                dataset_id=str(manifest["dataset_id"]),
                status=str(report["status"]),
                quality_report=report,
            )

        _write_json(temporary / "quality.json", quality_report)
        data_files = [*protected_files, "quality.json"]
        manifest: Dict[str, Any] = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "artifact_type": "curated_daily_bars_dataset",
            "dataset_id": dataset_id,
            "built_at": datetime.now(as_of.tzinfo).isoformat(),
            "as_of": as_of.isoformat(),
            "quality_status": status,
            "source_snapshot": {
                "snapshot_id": snapshot.snapshot_id,
                "path": str(snapshot.directory.resolve()),
                "provider": snapshot.manifest["provider"],
                "provider_version": snapshot.manifest["provider_version"],
                "fetched_at": snapshot.manifest["fetched_at"],
                "payload_sha256": snapshot.manifest["payload"]["sha256"],
                "license": snapshot.manifest["license"],
            },
            "research_policy_path": str(research_policy_path.resolve()),
            "research_policy_sha256": file_sha256(research_policy_path),
            "research_scope": policy.to_dict(),
            "input_files": {
                "calendar": {
                    "path": str(calendar_path.resolve()),
                    "sha256": file_sha256(calendar_path),
                },
                "instruments": {
                    "path": str(instruments_path.resolve()),
                    "sha256": file_sha256(instruments_path),
                },
                "suspensions": {
                    "path": str(suspensions_path.resolve()),
                    "sha256": file_sha256(suspensions_path),
                },
                "corporate_actions": {
                    "path": str(corporate_actions_path.resolve()),
                    "sha256": file_sha256(corporate_actions_path),
                },
            },
            "point_in_time": {
                "rule": "available_at <= as_of",
                "mode": policy.point_in_time_mode,
                "timezone_required": True,
                "future_rows_are_excluded": True,
                "snapshot_fetched_at": snapshot_fetched_at.isoformat(),
                "locally_observed_by_as_of": locally_observed,
            },
            "price_semantics": {
                "adjustment": policy.adjustment,
                "corporate_action_handling": (
                    "explicit_ledger"
                    if policy.adjustment == "none"
                    else "embedded_in_adjusted_prices"
                ),
                "manual_order_planning_eligible": (
                    policy.adjustment == "none"
                ),
                "adjusted_cost_and_lot_model": (
                    "research_approximation"
                    if policy.adjustment != "none"
                    else "unadjusted_reference"
                ),
            },
            "revision_policy": (
                "input, builder, quality, or curated output changes create a "
                "new dataset_id; existing datasets are never overwritten"
            ),
            "identity": identity,
            "files": {
                name: file_sha256(temporary / name) for name in data_files
            },
        }
        _write_json(temporary / "manifest.json", manifest)
        try:
            temporary.rename(destination)
        except FileExistsError:
            _validate_dataset_directory(destination, require_pass=False)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    return DatasetBuildResult(
        directory=destination,
        dataset_id=dataset_id,
        status=status,
        quality_report=quality_report,
    )


def _validate_dataset_directory(
    path: Path, *, require_pass: bool
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    manifest_path = path if path.name == "manifest.json" else path / "manifest.json"
    directory = manifest_path.parent
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataError(f"dataset manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(
            f"invalid dataset manifest JSON: line {exc.lineno}, "
            f"column {exc.colno}"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version")
        not in SUPPORTED_DATASET_SCHEMA_VERSIONS
        or manifest.get("artifact_type") != "curated_daily_bars_dataset"
    ):
        raise DataError("path is not a supported curated dataset")
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise DataError("dataset identity is missing")
    expected_id = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    if manifest.get("dataset_id") != expected_id or directory.name != expected_id:
        raise DataError("dataset identity hash mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise DataError("dataset file hashes are missing")
    for filename, expected_hash in files.items():
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise DataError("dataset manifest contains an invalid filename")
        file_path = directory / filename
        if not file_path.is_file():
            raise DataError(f"dataset file is missing: {file_path}")
        if file_sha256(file_path) != expected_hash:
            raise DataError(f"dataset file hash mismatch: {file_path}")
    try:
        quality = json.loads((directory / "quality.json").read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise DataError("dataset quality report is invalid") from exc
    if not isinstance(quality, dict) or quality.get("dataset_id") != expected_id:
        raise DataError("dataset quality report identity mismatch")
    quality_payload = {
        key: value for key, value in quality.items() if key != "dataset_id"
    }
    if hashlib.sha256(canonical_json_bytes(quality_payload)).hexdigest() != (
        identity.get("quality_payload_sha256")
    ):
        raise DataError("dataset quality report content hash mismatch")

    protected_files = identity.get("curated_files")
    if not isinstance(protected_files, dict) or not protected_files:
        raise DataError("dataset protected file hashes are missing")
    for filename, expected_hash in protected_files.items():
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise DataError("dataset identity contains an invalid filename")
        file_path = directory / filename
        if not file_path.is_file() or file_sha256(file_path) != expected_hash:
            raise DataError(f"dataset protected file hash mismatch: {file_path}")
        if files.get(filename) != expected_hash:
            raise DataError(
                f"dataset manifest and identity disagree for {filename}"
            )

    try:
        policy_snapshot = json.loads(
            (directory / "research_policy.snapshot.json").read_text(
                encoding="utf-8"
            )
        )
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise DataError("dataset research policy snapshot is invalid") from exc
    if manifest.get("research_scope") != policy_snapshot:
        raise DataError("dataset research scope does not match its snapshot")
    if hashlib.sha256(canonical_json_bytes(policy_snapshot)).hexdigest() != (
        identity.get("research_scope_sha256")
    ):
        raise DataError("dataset research scope content hash mismatch")

    if manifest.get("as_of") != identity.get("as_of"):
        raise DataError("dataset as_of does not match its identity")
    if manifest.get("research_policy_sha256") != identity.get(
        "research_policy_sha256"
    ):
        raise DataError("dataset research policy hash does not match identity")
    source_snapshot = manifest.get("source_snapshot")
    if (
        not isinstance(source_snapshot, dict)
        or source_snapshot.get("snapshot_id")
        != identity.get("source_snapshot_id")
    ):
        raise DataError("dataset source snapshot does not match identity")
    input_files = manifest.get("input_files")
    identity_inputs = identity.get("inputs")
    if not isinstance(input_files, dict) or not isinstance(
        identity_inputs, dict
    ):
        raise DataError("dataset input lineage is missing")
    input_hash_keys = {
        "calendar": "calendar_sha256",
        "instruments": "instruments_sha256",
        "suspensions": "suspensions_sha256",
        "corporate_actions": "corporate_actions_sha256",
    }
    for input_name, identity_key in input_hash_keys.items():
        entry = input_files.get(input_name)
        if (
            not isinstance(entry, dict)
            or entry.get("sha256") != identity_inputs.get(identity_key)
        ):
            raise DataError(
                f"dataset input lineage mismatch: {input_name}"
            )
    if manifest.get("quality_status") != quality.get("status"):
        raise DataError("dataset quality status mismatch")
    point_in_time = manifest.get("point_in_time")
    if (
        not isinstance(point_in_time, dict)
        or point_in_time.get("mode")
        != policy_snapshot.get("point_in_time_mode")
    ):
        raise DataError("dataset point-in-time mode mismatch")
    price_semantics = manifest.get("price_semantics")
    if (
        not isinstance(price_semantics, dict)
        or price_semantics.get("adjustment")
        != policy_snapshot.get("adjustment")
    ):
        raise DataError("dataset price semantics mismatch")
    expected_action_handling = (
        "explicit_ledger"
        if policy_snapshot.get("adjustment") == "none"
        else "embedded_in_adjusted_prices"
    )
    actual_action_handling = price_semantics.get(
        "corporate_action_handling"
    )
    if manifest.get("schema_version") == 1 and actual_action_handling is None:
        actual_action_handling = expected_action_handling
    if actual_action_handling != expected_action_handling:
        raise DataError("dataset corporate action semantics mismatch")
    if require_pass and quality.get("status") != "pass":
        raise DataError("curated dataset failed data quality checks")
    return manifest, quality


def load_curated_dataset(path: Path) -> CuratedDataset:
    manifest, _ = _validate_dataset_directory(path, require_pass=True)
    directory = path.parent if path.name == "manifest.json" else path
    market = load_prices(directory / "prices.csv")
    tradable_by_date: Dict[date, Set[str]] = {
        trading_date: set() for trading_date in market.dates
    }
    observed_keys: Set[tuple[date, str]] = set()
    for line_number, row in _read_csv(
        directory / "observations.csv", OBSERVATION_COLUMNS
    ):
        prefix = f"{directory / 'observations.csv'}:{line_number}"
        trading_date = _parse_date(row["date"], f"{prefix}:date")
        assert trading_date is not None
        symbol = row["symbol"].strip().upper()
        key = (trading_date, symbol)
        if key in observed_keys:
            raise DataError(f"{prefix}:duplicate observation")
        observed_keys.add(key)
        normalized = row["tradable"].strip().lower()
        if normalized not in {"true", "false"}:
            raise DataError(f"{prefix}:tradable must be true or false")
        if normalized == "true":
            tradable_by_date.setdefault(trading_date, set()).add(symbol)
    price_keys = {
        (trading_date, symbol)
        for trading_date in market.dates
        for symbol in market.prices_on(trading_date)
    }
    if observed_keys != price_keys:
        raise DataError("dataset observations and prices have different keys")
    actions_by_date: Dict[date, List[CorporateAction]] = {}
    as_of = parse_timestamp(manifest["as_of"], "dataset.manifest.as_of")
    for action in _parse_actions(directory / "corporate_actions.csv", as_of):
        corporate_action = CorporateAction(
            symbol=action["symbol"],
            event_type=action["event_type"],
            ex_date=action["ex_date"],
            announced_at=action["announced_at"],
            cash_amount=action["cash_amount"],
            ratio=action["ratio"],
            available_at=action["available_at"],
        )
        actions_by_date.setdefault(corporate_action.ex_date, []).append(
            corporate_action
        )
    price_adjustment = manifest["price_semantics"]["adjustment"]
    market = MarketData(
        dates=market.dates,
        prices_by_date=market.prices_by_date,
        tradable_by_date=tradable_by_date,
        corporate_actions_by_date=actions_by_date,
        price_adjustment=price_adjustment,
    )
    return CuratedDataset(
        directory=directory, manifest=manifest, market=market
    )


def validate_strategy_scope(
    policy: Policy, dataset_manifest: Mapping[str, Any]
) -> None:
    scope = dataset_manifest.get("research_scope")
    if not isinstance(scope, dict):
        raise DataError("dataset research scope is missing")
    instruments = scope.get("instruments")
    if not isinstance(instruments, list):
        raise DataError("dataset research instruments are missing")
    tradable = {
        str(item.get("symbol", "")).upper()
        for item in instruments
        if isinstance(item, dict)
        and isinstance(item.get("roles"), list)
        and "tradable" in item["roles"]
    }
    unknown_targets = sorted(
        set(policy.strategy.target_weights) - tradable
    )
    if unknown_targets:
        raise DataError(
            "strategy targets are outside the frozen tradable universe: "
            + ", ".join(unknown_targets)
        )
    expected_benchmark = scope.get("benchmark")
    if policy.portfolio.benchmark != expected_benchmark:
        raise DataError(
            "strategy benchmark does not match the curated dataset "
            f"({policy.portfolio.benchmark!r} != {expected_benchmark!r})"
        )
    if policy.base_currency != scope.get("base_currency"):
        raise DataError("strategy currency does not match the curated dataset")
    maximum_drawdown = scope.get("max_drawdown")
    if (
        not isinstance(maximum_drawdown, (int, float))
        or policy.risk.max_drawdown > float(maximum_drawdown) + 1e-12
    ):
        raise DataError(
            "strategy max_drawdown is looser than the frozen research limit"
        )


def validate_manual_planning_source(
    dataset_manifest: Mapping[str, Any]
) -> None:
    scope = dataset_manifest.get("research_scope")
    if not isinstance(scope, dict):
        raise DataError("dataset research scope is missing")
    adjustment = scope.get("adjustment")
    if adjustment != "none":
        raise DataError(
            "manual order planning requires unadjusted executable prices; "
            f"dataset adjustment={adjustment!r} is research-only"
        )
