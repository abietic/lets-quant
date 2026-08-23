from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .artifacts import (
    write_backtest_artifacts,
    write_experiment_artifacts,
    write_plan_artifacts,
)
from .backtest import run_backtest
from .config import PolicyError, load_policy
from .data import DataError, load_holdings, load_prices
from .datasets import (
    build_curated_dataset,
    load_curated_dataset,
    parse_timestamp,
    validate_manual_planning_source,
    validate_strategy_scope,
)
from .experiments import (
    ExperimentError,
    load_experiment_spec,
    market_identity,
    run_experiment,
)
from .execution import (
    PaperAuditError,
    PaperExchange,
    PaperExecutionError,
    audit_paper_exchange,
    load_paper_audit_input,
    replay_event_file,
    save_paper_audit_report,
)
from .models import MarketData, Policy
from .orders import build_manual_order_plan
from .providers import DailyBarsRequest
from .providers.akshare import AkshareEtfDailyBarsProvider
from .research import ResearchPolicyError, load_research_policy
from .scenarios import (
    SUPPORTED_SYNTHETIC_SCENARIOS,
    generate_synthetic_market,
)
from .snapshots import save_snapshot_bytes, snapshot_file
from .strategies import StrategyError


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _timestamp_argument(value: str) -> datetime:
    try:
        return parse_timestamp(value)
    except DataError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lets-quant",
        description=(
            "Local-only quantitative investing research and manual planning"
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser(
        "validate-policy", help="validate a policy without running a strategy"
    )
    validate.add_argument("--policy", type=Path, required=True)

    validate_research = subcommands.add_parser(
        "validate-research-policy",
        help="validate the frozen M1 market and instrument scope",
    )
    validate_research.add_argument(
        "--research-policy", type=Path, required=True
    )

    snapshot = subcommands.add_parser(
        "snapshot-data",
        help="store a file as a content-addressed immutable raw snapshot",
    )
    snapshot.add_argument("--provider", required=True)
    snapshot.add_argument("--provider-version", required=True)
    snapshot.add_argument("--dataset-name", required=True)
    snapshot.add_argument("--input", type=Path, required=True)
    snapshot.add_argument("--license-manifest", type=Path, required=True)
    snapshot.add_argument("--request-json", default="{}")
    snapshot.add_argument("--fetched-at", type=_timestamp_argument)
    snapshot.add_argument(
        "--output-root", type=Path, default=Path("data/raw")
    )

    fetch_akshare = subcommands.add_parser(
        "fetch-akshare",
        help="fetch ETF daily bars through the optional AKShare adapter",
    )
    fetch_akshare.add_argument(
        "--research-policy", type=Path, required=True
    )
    fetch_akshare.add_argument("--start", type=_date_argument, required=True)
    fetch_akshare.add_argument("--end", type=_date_argument, required=True)
    fetch_akshare.add_argument(
        "--license-manifest", type=Path, required=True
    )
    fetch_akshare.add_argument(
        "--output-root", type=Path, default=Path("data/raw")
    )

    curate = subcommands.add_parser(
        "curate-data",
        help="build a point-in-time daily dataset and quality report",
    )
    curate.add_argument("--snapshot", type=Path, required=True)
    curate.add_argument("--research-policy", type=Path, required=True)
    curate.add_argument("--calendar", type=Path, required=True)
    curate.add_argument("--instruments", type=Path, required=True)
    curate.add_argument("--suspensions", type=Path, required=True)
    curate.add_argument("--corporate-actions", type=Path, required=True)
    curate.add_argument("--as-of", type=_timestamp_argument, required=True)
    curate.add_argument(
        "--output-root", type=Path, default=Path("data/curated")
    )

    verify_dataset = subcommands.add_parser(
        "verify-dataset",
        help="verify dataset identity, file hashes, and quality status",
    )
    verify_dataset.add_argument("--dataset", type=Path, required=True)

    backtest = subcommands.add_parser(
        "backtest", help="run the reference daily-close simulator"
    )
    backtest.add_argument("--policy", type=Path, required=True)
    backtest_source = backtest.add_mutually_exclusive_group(required=True)
    backtest_source.add_argument("--prices", type=Path)
    backtest_source.add_argument("--dataset", type=Path)
    backtest.add_argument(
        "--output-root", type=Path, default=Path("artifacts/runs")
    )

    experiment = subcommands.add_parser(
        "run-experiment",
        help="run chronological windows across offline execution scenarios",
    )
    experiment.add_argument("--policy", type=Path, required=True)
    experiment.add_argument("--experiment", type=Path, required=True)
    experiment_source = experiment.add_mutually_exclusive_group(required=True)
    experiment_source.add_argument("--prices", type=Path)
    experiment_source.add_argument("--dataset", type=Path)
    experiment_source.add_argument(
        "--scenario", choices=SUPPORTED_SYNTHETIC_SCENARIOS
    )
    experiment.add_argument(
        "--scenario-start",
        type=_date_argument,
        default=date(2022, 1, 3),
    )
    experiment.add_argument(
        "--scenario-trading-days", type=int, default=780
    )
    experiment.add_argument(
        "--output-root", type=Path, default=Path("artifacts/experiments")
    )

    plan = subcommands.add_parser(
        "plan-orders",
        help="produce a blocked-or-reviewable order plan; never place orders",
    )
    plan.add_argument("--policy", type=Path, required=True)
    plan_source = plan.add_mutually_exclusive_group(required=True)
    plan_source.add_argument("--prices", type=Path)
    plan_source.add_argument("--dataset", type=Path)
    plan.add_argument("--holdings", type=Path, required=True)
    plan.add_argument("--cash", type=float, required=True)
    plan.add_argument("--as-of", type=_date_argument)
    plan.add_argument(
        "--output-root", type=Path, default=Path("artifacts/plans")
    )

    paper = subcommands.add_parser(
        "replay-paper-events",
        help="replay offline order events into a checksummed paper state",
    )
    paper_start = paper.add_mutually_exclusive_group(required=True)
    paper_start.add_argument("--initial-cash", type=float)
    paper_start.add_argument("--resume-state", type=Path)
    paper.add_argument("--holdings", type=Path)
    paper.add_argument("--events", type=Path, required=True)
    paper.add_argument("--state-out", type=Path, required=True)

    paper_audit = subcommands.add_parser(
        "audit-paper-state",
        help="audit offline paper health, fills, and imported account state",
    )
    paper_audit.add_argument("--state", type=Path, required=True)
    paper_audit.add_argument("--audit-input", type=Path, required=True)
    paper_audit.add_argument("--report-out", type=Path, required=True)
    return parser


def _parse_request_json(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataError(
            f"request-json is invalid at column {exc.colno}"
        ) from exc
    if not isinstance(parsed, dict):
        raise DataError("request-json must be a JSON object")
    return parsed


def _validate_policy(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    payload = {
        "status": "valid",
        "policy": policy.name,
        "execution_mode": policy.execution.mode,
        "target_weight_total": round(
            sum(policy.strategy.target_weights.values()), 12
        ),
        "automatic_execution_supported": False,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _validate_research_policy(args: argparse.Namespace) -> int:
    policy = load_research_policy(args.research_policy)
    print(
        json.dumps(
            {
                "status": "valid",
                "research_policy": policy.name,
                "market": policy.market,
                "symbols": sorted(policy.symbols),
                "tradable_symbols": sorted(policy.tradable_symbols),
                "benchmark": policy.benchmark,
                "adjustment": policy.adjustment,
                "point_in_time_mode": policy.point_in_time_mode,
                "purpose": policy.purpose,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _snapshot_data(args: argparse.Namespace) -> int:
    snapshot = snapshot_file(
        input_path=args.input,
        provider=args.provider,
        provider_version=args.provider_version,
        dataset=args.dataset_name,
        request=_parse_request_json(args.request_json),
        license_manifest_path=args.license_manifest,
        output_root=args.output_root,
        fetched_at=args.fetched_at,
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_directory": str(snapshot.directory.resolve()),
                "payload_sha256": snapshot.manifest["payload"]["sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _fetch_akshare(args: argparse.Namespace) -> int:
    research_policy = load_research_policy(args.research_policy)
    unsupported = sorted(
        instrument.symbol
        for instrument in research_policy.instruments
        if instrument.asset_type != "ETF"
    )
    if unsupported:
        raise DataError(
            "AKShare ETF adapter cannot fetch non-ETF instruments: "
            + ", ".join(unsupported)
        )
    provider = AkshareEtfDailyBarsProvider()
    payload = provider.fetch_daily_bars(
        DailyBarsRequest(
            symbols=sorted(research_policy.symbols),
            start_date=args.start,
            end_date=args.end,
            adjustment=research_policy.adjustment,
        )
    )
    snapshot = save_snapshot_bytes(
        content=payload.content,
        payload_filename=payload.filename,
        provider=payload.provider,
        provider_version=payload.provider_version,
        dataset=payload.dataset,
        request=payload.request,
        license_manifest_path=args.license_manifest,
        output_root=args.output_root,
        content_type=payload.content_type,
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "provider": payload.provider,
                "provider_version": payload.provider_version,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_directory": str(snapshot.directory.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _curate_data(args: argparse.Namespace) -> int:
    result = build_curated_dataset(
        snapshot_path=args.snapshot,
        research_policy_path=args.research_policy,
        calendar_path=args.calendar,
        instruments_path=args.instruments,
        suspensions_path=args.suspensions,
        corporate_actions_path=args.corporate_actions,
        as_of=args.as_of,
        output_root=args.output_root,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "dataset_id": result.dataset_id,
                "dataset_directory": str(result.directory.resolve()),
                "quality_summary": result.quality_report["summary"],
                "failed_checks": [
                    check["name"]
                    for check in result.quality_report["checks"]
                    if check["status"] == "fail"
                ],
                "warnings": [
                    check["message"]
                    for check in result.quality_report["checks"]
                    if check["status"] == "warning"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.status == "pass" else 4


def _verify_dataset(args: argparse.Namespace) -> int:
    dataset = load_curated_dataset(args.dataset)
    print(
        json.dumps(
            {
                "status": "valid",
                "dataset_id": dataset.dataset_id,
                "as_of": dataset.manifest["as_of"],
                "symbols": dataset.market.symbols,
                "trading_days": len(dataset.market.dates),
                "source_snapshot_id": dataset.manifest["source_snapshot"][
                    "snapshot_id"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _load_market_source(
    args: argparse.Namespace, policy: Policy
) -> Tuple[MarketData, Path, Optional[Dict[str, Any]]]:
    if args.dataset is None:
        return load_prices(args.prices), args.prices, None
    dataset = load_curated_dataset(args.dataset)
    validate_strategy_scope(policy, dataset.manifest)
    return (
        dataset.market,
        dataset.directory / "prices.csv",
        dataset.manifest,
    )


def _backtest(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    market, prices_path, dataset_manifest = _load_market_source(args, policy)
    result = run_backtest(policy, market)
    destination = write_backtest_artifacts(
        result,
        policy,
        args.policy,
        prices_path,
        args.output_root,
        dataset_manifest=dataset_manifest,
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "artifact_directory": str(destination.resolve()),
                "metrics": result.metrics,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _run_experiment(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    spec = load_experiment_spec(args.experiment)
    dataset_manifest = None
    market_snapshot = None

    if args.prices is not None:
        market = load_prices(args.prices)
        market_source_path = args.prices
        market_source = {"type": "standalone_prices_csv"}
    elif args.dataset is not None:
        dataset = load_curated_dataset(args.dataset)
        validate_strategy_scope(policy, dataset.manifest)
        market = dataset.market
        market_source_path = dataset.directory / "prices.csv"
        dataset_manifest = dataset.manifest
        market_source = {
            "type": "curated_dataset",
            "dataset_id": dataset.dataset_id,
            "as_of": dataset.manifest["as_of"],
        }
    else:
        symbols = set(policy.strategy.target_weights)
        if policy.portfolio.benchmark:
            symbols.add(policy.portfolio.benchmark)
        synthetic = generate_synthetic_market(
            args.scenario,
            start_date=args.scenario_start,
            trading_days=args.scenario_trading_days,
            symbols=sorted(symbols),
            benchmark=policy.portfolio.benchmark,
            seed=spec.seed,
        )
        market = synthetic.market
        market_source_path = None
        market_source = dict(synthetic.metadata)
        market_snapshot = {
            "metadata": synthetic.metadata,
            "market": market_identity(market),
        }

    result = run_experiment(spec, policy, market)
    destination = write_experiment_artifacts(
        result,
        policy,
        args.policy,
        args.experiment,
        args.output_root,
        market_source,
        market_source_path=market_source_path,
        market_snapshot=market_snapshot,
        dataset_manifest=dataset_manifest,
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "artifact_directory": str(destination.resolve()),
                "experiment_input_id": result.experiment_input_id,
                "result_sha256": result.result_sha256,
                "summary": result.summary,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _plan_orders(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    market, prices_path, dataset_manifest = _load_market_source(args, policy)
    if dataset_manifest is not None:
        validate_manual_planning_source(dataset_manifest)
    holdings = load_holdings(args.holdings)
    plan = build_manual_order_plan(
        policy,
        market,
        holdings,
        args.cash,
        args.as_of,
    )
    destination = write_plan_artifacts(
        plan,
        policy,
        args.policy,
        prices_path,
        args.holdings,
        args.output_root,
        dataset_manifest=dataset_manifest,
    )
    print(
        json.dumps(
            {
                "status": plan.status,
                "artifact_directory": str(destination.resolve()),
                "approval_required": plan.approval_required,
                "automatic_execution_allowed": (
                    plan.automatic_execution_allowed
                ),
                "violations": plan.violations,
                "recommendation_count": len(plan.recommendations),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if plan.status != "blocked" else 3


def _replay_paper_events(args: argparse.Namespace) -> int:
    if args.resume_state is not None:
        if args.holdings is not None:
            raise PaperExecutionError(
                "--holdings cannot be combined with --resume-state"
            )
        exchange = PaperExchange.load(args.resume_state)
    else:
        holdings = load_holdings(args.holdings) if args.holdings else []
        exchange = PaperExchange(
            initial_cash=args.initial_cash,
            initial_positions={
                holding.symbol: holding.quantity for holding in holdings
            },
        )
    input_event_count = replay_event_file(exchange, args.events)
    exchange.save(args.state_out)
    snapshot = exchange.to_snapshot()
    status_counts: Dict[str, int] = {}
    for order in exchange.orders.values():
        status_counts[order.status] = status_counts.get(order.status, 0) + 1
    print(
        json.dumps(
            {
                "status": "completed",
                "execution_mode": "offline_paper",
                "automatic_execution_allowed": False,
                "state_path": str(args.state_out.resolve()),
                "state_sha256": snapshot["state_sha256"],
                "input_event_count": input_event_count,
                "recorded_event_count": len(exchange.events),
                "order_count": len(exchange.orders),
                "order_status_counts": dict(sorted(status_counts.items())),
                "cash": exchange.cash,
                "positions": dict(sorted(exchange.positions.items())),
                "reconciliation": exchange.reconciliation(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _audit_paper_state(args: argparse.Namespace) -> int:
    exchange = PaperExchange.load(args.state)
    audit_input = load_paper_audit_input(args.audit_input)
    report = audit_paper_exchange(exchange, audit_input)
    save_paper_audit_report(report, args.report_out)
    print(
        json.dumps(
            {
                "status": report["status"],
                "execution_mode": "offline_paper",
                "automatic_execution_allowed": False,
                "report_path": str(args.report_out.resolve()),
                "report_sha256": report["report_sha256"],
                "paper_state_sha256": report["paper_state_sha256"],
                "audit_input_sha256": report["audit_input_sha256"],
                "summary": report["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 3 if report["status"] == "blocked" else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-policy":
            return _validate_policy(args)
        if args.command == "validate-research-policy":
            return _validate_research_policy(args)
        if args.command == "snapshot-data":
            return _snapshot_data(args)
        if args.command == "fetch-akshare":
            return _fetch_akshare(args)
        if args.command == "curate-data":
            return _curate_data(args)
        if args.command == "verify-dataset":
            return _verify_dataset(args)
        if args.command == "backtest":
            return _backtest(args)
        if args.command == "run-experiment":
            return _run_experiment(args)
        if args.command == "plan-orders":
            return _plan_orders(args)
        if args.command == "replay-paper-events":
            return _replay_paper_events(args)
        if args.command == "audit-paper-state":
            return _audit_paper_state(args)
    except (
        DataError,
        ExperimentError,
        PaperAuditError,
        PaperExecutionError,
        PolicyError,
        ResearchPolicyError,
        StrategyError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unsupported command: {args.command}")
    return 2
