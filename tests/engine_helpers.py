import contextlib
import csv
import io
import json
from pathlib import Path
from typing import Dict, List, Optional

from lets_quant.cli import main


ROOT = Path(__file__).resolve().parents[1]


def _run_json(args) -> Dict[str, object]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = main(args)
    if exit_code != 0:
        raise AssertionError(stdout.getvalue())
    return json.loads(stdout.getvalue())


def build_curated_reference(
    temporary: Path,
    *,
    adjustment: str = "hfq",
    suspended_symbol: Optional[str] = None,
    suspension_date: str = "2025-01-03",
    include_corporate_action: bool = True,
    corporate_action_rows: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Path]:
    temporary.mkdir(parents=True, exist_ok=True)
    bars_path = temporary / "bars.csv"
    with (ROOT / "examples/m1/bars.csv").open(
        newline="", encoding="utf-8"
    ) as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        row["adjustment"] = adjustment
    with bars_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    research_policy = json.loads(
        (ROOT / "config/research_policy.cn-etf.example.json").read_text(
            encoding="utf-8"
        )
    )
    research_policy["adjustment"] = adjustment
    research_policy_path = temporary / "research-policy.json"
    research_policy_path.write_text(
        json.dumps(research_policy, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    suspensions_path = temporary / "suspensions.csv"
    suspensions_path.write_text(
        "date,symbol,available_at\n"
        + (
            f"{suspension_date},{suspended_symbol},"
            f"{suspension_date}T09:00:00+08:00\n"
            if suspended_symbol
            else ""
        ),
        encoding="utf-8",
    )
    corporate_actions_path = temporary / "corporate-actions.csv"
    action_fields = [
        "symbol",
        "event_type",
        "ex_date",
        "announced_at",
        "cash_amount",
        "ratio",
        "available_at",
    ]
    if corporate_action_rows is not None:
        with corporate_actions_path.open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=action_fields)
            writer.writeheader()
            writer.writerows(corporate_action_rows)
    elif include_corporate_action:
        corporate_actions_path.write_bytes(
            (ROOT / "examples/m1/corporate_actions.csv").read_bytes()
        )
    else:
        corporate_actions_path.write_text(
            ",".join(action_fields) + "\n",
            encoding="utf-8",
        )

    snapshot = _run_json(
        [
            "snapshot-data",
            "--provider",
            "local_csv",
            "--provider-version",
            "1",
            "--dataset-name",
            "engine_input_fixture",
            "--input",
            str(bars_path),
            "--license-manifest",
            str(ROOT / "config/data_providers.example.json"),
            "--request-json",
            '{"fixture":"engine-input"}',
            "--output-root",
            str(temporary / "raw"),
        ]
    )
    curated = _run_json(
        [
            "curate-data",
            "--snapshot",
            str(snapshot["snapshot_directory"]),
            "--research-policy",
            str(research_policy_path),
            "--calendar",
            str(ROOT / "examples/m1/calendar.csv"),
            "--instruments",
            str(ROOT / "examples/m1/instruments.csv"),
            "--suspensions",
            str(suspensions_path),
            "--corporate-actions",
            str(corporate_actions_path),
            "--as-of",
            "2025-01-08T23:59:59+08:00",
            "--output-root",
            str(temporary / "curated"),
        ]
    )
    reference = _run_json(
        [
            "backtest",
            "--policy",
            str(ROOT / "config/policy.cn-etf.example.json"),
            "--dataset",
            str(curated["dataset_directory"]),
            "--output-root",
            str(temporary / "reference"),
        ]
    )
    return {
        "dataset": Path(str(curated["dataset_directory"])),
        "reference": Path(str(reference["artifact_directory"])),
        "prices": Path(str(curated["dataset_directory"])) / "prices.csv",
    }
