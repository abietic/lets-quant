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
from .cross_engine import (
    EngineValidationError,
    reconcile_engine_candidate,
    write_reconciliation_report,
)
from .data import (
    DataError,
    generated_instrument_master,
    load_holdings,
    load_prices,
)
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
from .experiment_verification import verify_experiment_artifacts
from .execution import (
    PaperAuditError,
    PaperExchange,
    PaperExecutionError,
    audit_paper_exchange,
    load_paper_audit_input,
    replay_event_file,
    save_paper_audit_report,
)
from .models import InstrumentMetadata, MarketData, Policy
from .orders import build_manual_order_plan
from .providers import DailyBarsRequest
from .providers.akshare import AkshareEtfDailyBarsProvider
from .research import ResearchPolicyError, load_research_policy
from .rqalpha_adapter import run_rqalpha_validation
from .scenarios import (
    SUPPORTED_SYNTHETIC_SCENARIOS,
    generate_synthetic_market,
)
from .snapshots import save_snapshot_bytes, snapshot_file
from .strategies import StrategyError
from .vectorbt_adapter import run_vectorbt_validation


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
    backtest.add_argument("--initial-holdings", type=Path)
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

    verify_experiment = subcommands.add_parser(
        "verify-experiment",
        help="verify experiment file hashes and cross-file consistency",
    )
    verify_experiment.add_argument(
        "--experiment-run", type=Path, required=True
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

    vectorbt_validation = subcommands.add_parser(
        "validate-vectorbt",
        help=(
            "replay frozen order intents in VectorBT and reconcile artifacts"
        ),
    )
    vectorbt_validation.add_argument(
        "--reference-run", type=Path, required=True
    )
    vectorbt_source = vectorbt_validation.add_mutually_exclusive_group()
    vectorbt_source.add_argument(
        "--prices",
        type=Path,
        help="price CSV; defaults to the path bound by the reference manifest",
    )
    vectorbt_source.add_argument(
        "--dataset",
        type=Path,
        help="curated dataset; defaults to the path bound by the reference",
    )
    vectorbt_validation.add_argument(
        "--output-root",
        type=Path,
        default=Path("artifacts/engine-validation"),
    )
    vectorbt_validation.add_argument(
        "--money-tolerance", type=float, default=1e-6
    )
    vectorbt_validation.add_argument(
        "--ratio-tolerance", type=float, default=1e-10
    )

    rqalpha_validation = subcommands.add_parser(
        "validate-rqalpha",
        help=(
            "independently generate policy decisions in RQAlpha and reconcile "
            "signals, execution, and native order lifecycle artifacts"
        ),
    )
    rqalpha_validation.add_argument(
        "--reference-run", type=Path, required=True
    )
    rqalpha_source = rqalpha_validation.add_mutually_exclusive_group()
    rqalpha_source.add_argument(
        "--prices",
        type=Path,
        help="price CSV; defaults to the path bound by the reference manifest",
    )
    rqalpha_source.add_argument(
        "--dataset",
        type=Path,
        help="curated dataset; defaults to the path bound by the reference",
    )
    rqalpha_validation.add_argument(
        "--liquidity",
        type=Path,
        help="optional complete date/symbol/volume CSV for liquidity stress",
    )
    rqalpha_validation.add_argument(
        "--decision-mode",
        choices=("independent_policy", "frozen_orders"),
        default="independent_policy",
        help=(
            "independent_policy validates PIT decisions and execution; "
            "frozen_orders replays reference intents for execution diagnostics"
        ),
    )
    rqalpha_validation.add_argument(
        "--volume-percent", type=float, default=1.0
    )
    rqalpha_validation.add_argument(
        "--output-root",
        type=Path,
        default=Path("artifacts/engine-validation"),
    )
    rqalpha_validation.add_argument(
        "--money-tolerance", type=float, default=1e-6
    )
    rqalpha_validation.add_argument(
        "--ratio-tolerance", type=float, default=1e-10
    )

    reconcile_engine = subcommands.add_parser(
        "reconcile-engine",
        help="reconcile a normalized independent-engine candidate",
    )
    reconcile_engine.add_argument("--reference-run", type=Path, required=True)
    reconcile_engine.add_argument("--candidate-run", type=Path, required=True)
    reconcile_engine.add_argument("--report-out", type=Path, required=True)
    reconcile_engine.add_argument(
        "--money-tolerance", type=float, default=1e-6
    )
    reconcile_engine.add_argument(
        "--ratio-tolerance", type=float, default=1e-10
    )
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
) -> Tuple[
    MarketData,
    Path,
    Optional[Dict[str, Any]],
    List[InstrumentMetadata],
    str,
]:
    if args.dataset is None:
        market = load_prices(args.prices)
        return (
            market,
            args.prices,
            None,
            generated_instrument_master(market),
            "generated_from_standalone_prices",
        )
    dataset = load_curated_dataset(args.dataset)
    validate_strategy_scope(policy, dataset.manifest)
    return (
        dataset.market,
        dataset.directory / "prices.csv",
        dataset.manifest,
        list(dataset.instruments.values()),
        "curated_dataset",
    )


def _backtest(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    (
        market,
        prices_path,
        dataset_manifest,
        instrument_master,
        instrument_master_source,
    ) = _load_market_source(args, policy)
    initial_holdings = (
        load_holdings(args.initial_holdings)
        if args.initial_holdings is not None
        else []
    )
    result = run_backtest(
        policy, market, initial_holdings=initial_holdings
    )
    destination = write_backtest_artifacts(
        result,
        policy,
        args.policy,
        prices_path,
        args.output_root,
        instrument_master=instrument_master,
        instrument_master_source=instrument_master_source,
        initial_holdings=initial_holdings,
        initial_holdings_path=args.initial_holdings,
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


def _verify_experiment(args: argparse.Namespace) -> int:
    report = verify_experiment_artifacts(args.experiment_run)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _plan_orders(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    market, prices_path, dataset_manifest, _, _ = _load_market_source(
        args, policy
    )
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


def _validate_vectorbt(args: argparse.Namespace) -> int:
    destination, report = run_vectorbt_validation(
        reference_directory=args.reference_run,
        prices_path=args.prices,
        dataset_path=args.dataset,
        output_root=args.output_root,
        money_tolerance=args.money_tolerance,
        ratio_tolerance=args.ratio_tolerance,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "candidate_directory": str(destination.resolve()),
                "report_path": str(
                    (destination / "reconciliation.json").resolve()
                ),
                "report_sha256": report["report_sha256"],
                "summary": report["summary"],
                "investment_validity_established": False,
                "automatic_execution_allowed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "pass" else 3


def _validate_rqalpha(args: argparse.Namespace) -> int:
    destination, report = run_rqalpha_validation(
        reference_directory=args.reference_run,
        prices_path=args.prices,
        dataset_path=args.dataset,
        liquidity_path=args.liquidity,
        decision_mode=args.decision_mode,
        volume_percent=args.volume_percent,
        output_root=args.output_root,
        money_tolerance=args.money_tolerance,
        ratio_tolerance=args.ratio_tolerance,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "candidate_directory": str(destination.resolve()),
                "report_path": str(
                    (destination / "reconciliation.json").resolve()
                ),
                "report_sha256": report["report_sha256"],
                "summary": report["summary"],
                "investment_validity_established": False,
                "automatic_execution_allowed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "pass" else 3


def _reconcile_engine(args: argparse.Namespace) -> int:
    report = reconcile_engine_candidate(
        args.reference_run,
        args.candidate_run,
        money_tolerance=args.money_tolerance,
        ratio_tolerance=args.ratio_tolerance,
    )
    write_reconciliation_report(report, args.report_out)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report_path": str(args.report_out.resolve()),
                "report_sha256": report["report_sha256"],
                "summary": report["summary"],
                "investment_validity_established": False,
                "automatic_execution_allowed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "pass" else 3


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
        if args.command == "verify-experiment":
            return _verify_experiment(args)
        if args.command == "plan-orders":
            return _plan_orders(args)
        if args.command == "replay-paper-events":
            return _replay_paper_events(args)
        if args.command == "audit-paper-state":
            return _audit_paper_state(args)
        if args.command == "validate-vectorbt":
            return _validate_vectorbt(args)
        if args.command == "validate-rqalpha":
            return _validate_rqalpha(args)
        if args.command == "reconcile-engine":
            return _reconcile_engine(args)
    except (
        DataError,
        EngineValidationError,
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
