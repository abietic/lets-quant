import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from lets_quant.cli import main


ROOT = Path(__file__).resolve().parents[1]


class CliTest(unittest.TestCase):
    def test_backtest_writes_auditable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "runs"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "backtest",
                        "--policy",
                        str(ROOT / "config/policy.example.json"),
                        "--prices",
                        str(ROOT / "examples/prices.csv"),
                        "--output-root",
                        str(output),
                    ]
                )

            self.assertEqual(exit_code, 0, stdout.getvalue())
            run_directories = list(output.iterdir())
            self.assertEqual(len(run_directories), 1)
            files = {path.name for path in run_directories[0].iterdir()}
            self.assertEqual(
                files,
                {
                    "accounting.csv",
                    "ledger.csv",
                    "manifest.json",
                    "metrics.json",
                    "nav.csv",
                    "policy.snapshot.json",
                    "signals.csv",
                    "trades.csv",
                },
            )
            manifest = json.loads(
                (run_directories[0] / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(manifest["source_tree_sha256"]), 64)
            self.assertEqual(
                set(manifest["file_sha256"]), files - {"manifest.json"}
            )
            for name, expected_hash in manifest["file_sha256"].items():
                self.assertEqual(
                    hashlib.sha256(
                        (run_directories[0] / name).read_bytes()
                    ).hexdigest(),
                    expected_hash,
                )

    def test_example_order_plan_is_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "plans"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "plan-orders",
                        "--policy",
                        str(ROOT / "config/policy.example.json"),
                        "--prices",
                        str(ROOT / "examples/prices.csv"),
                        "--holdings",
                        str(ROOT / "examples/holdings.csv"),
                        "--cash",
                        "25000",
                        "--output-root",
                        str(output),
                    ]
                )

            self.assertEqual(exit_code, 0, stdout.getvalue())
            plan_directories = list(output.iterdir())
            self.assertEqual(len(plan_directories), 1)
            self.assertTrue((plan_directories[0] / "plan.json").exists())
            self.assertTrue((plan_directories[0] / "orders.csv").exists())

    def test_m1_dataset_backtest_preserves_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                snapshot_exit = main(
                    [
                        "snapshot-data",
                        "--provider",
                        "local_csv",
                        "--provider-version",
                        "1",
                        "--dataset-name",
                        "etf_daily_bars",
                        "--input",
                        str(ROOT / "examples/m1/bars.csv"),
                        "--license-manifest",
                        str(ROOT / "config/data_providers.example.json"),
                        "--output-root",
                        str(temporary / "raw"),
                    ]
                )
            snapshot_payload = json.loads(stdout.getvalue())
            self.assertEqual(snapshot_exit, 0)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                curate_exit = main(
                    [
                        "curate-data",
                        "--snapshot",
                        snapshot_payload["snapshot_directory"],
                        "--research-policy",
                        str(
                            ROOT
                            / "config/research_policy.cn-etf.example.json"
                        ),
                        "--calendar",
                        str(ROOT / "examples/m1/calendar.csv"),
                        "--instruments",
                        str(ROOT / "examples/m1/instruments.csv"),
                        "--suspensions",
                        str(ROOT / "examples/m1/suspensions.csv"),
                        "--corporate-actions",
                        str(ROOT / "examples/m1/corporate_actions.csv"),
                        "--as-of",
                        "2025-01-08T23:59:59+08:00",
                        "--output-root",
                        str(temporary / "curated"),
                    ]
                )
            curated_payload = json.loads(stdout.getvalue())
            self.assertEqual(curate_exit, 0)

            stdout = io.StringIO()
            output = temporary / "runs"
            with contextlib.redirect_stdout(stdout):
                backtest_exit = main(
                    [
                        "backtest",
                        "--policy",
                        str(ROOT / "config/policy.cn-etf.example.json"),
                        "--dataset",
                        curated_payload["dataset_directory"],
                        "--output-root",
                        str(output),
                    ]
                )

            self.assertEqual(backtest_exit, 0, stdout.getvalue())
            run_directory = next(output.iterdir())
            self.assertTrue((run_directory / "dataset.snapshot.json").exists())
            manifest = json.loads(
                (run_directory / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["data_source"]["dataset_id"],
                curated_payload["dataset_id"],
            )
            metrics = json.loads(
                (run_directory / "metrics.json").read_text(encoding="utf-8")
            )
            self.assertTrue(metrics["accounting_reconciled"])
            self.assertEqual(metrics["corporate_action_entry_count"], 1)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                plan_exit = main(
                    [
                        "plan-orders",
                        "--policy",
                        str(ROOT / "config/policy.cn-etf.example.json"),
                        "--dataset",
                        curated_payload["dataset_directory"],
                        "--holdings",
                        str(ROOT / "examples/m1/holdings.csv"),
                        "--cash",
                        "50000",
                    ]
                )
            self.assertEqual(plan_exit, 2)
            self.assertIn("unadjusted", stderr.getvalue())

    def test_offline_experiment_writes_replayable_case_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "experiments"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "run-experiment",
                        "--policy",
                        str(ROOT / "config/policy.momentum.example.json"),
                        "--experiment",
                        str(ROOT / "config/experiment.m1_5.example.json"),
                        "--scenario",
                        "regime_shift",
                        "--scenario-start",
                        "2022-01-03",
                        "--scenario-trading-days",
                        "780",
                        "--output-root",
                        str(output),
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0, stdout.getvalue())
            self.assertEqual(payload["summary"]["case_count"], 9)
            experiment_directory = Path(payload["artifact_directory"])
            manifest = json.loads(
                (experiment_directory / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["result_sha256"], payload["result_sha256"]
            )
            self.assertFalse(manifest["investment_validity_established"])
            case_directories = list(
                (experiment_directory / "cases").iterdir()
            )
            self.assertEqual(len(case_directories), 9)
            self.assertTrue(
                all((case / "signals.csv").exists() for case in case_directories)
            )
            self.assertTrue(
                all((case / "ledger.csv").exists() for case in case_directories)
            )

    def test_paper_event_replay_is_restart_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first_state = Path(temp_dir) / "first-state.json"
            second_state = Path(temp_dir) / "second-state.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                first_exit = main(
                    [
                        "replay-paper-events",
                        "--initial-cash",
                        "100000",
                        "--events",
                        str(ROOT / "examples/paper/events.jsonl"),
                        "--state-out",
                        str(first_state),
                    ]
                )
            first_payload = json.loads(stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                second_exit = main(
                    [
                        "replay-paper-events",
                        "--resume-state",
                        str(first_state),
                        "--events",
                        str(ROOT / "examples/paper/events.jsonl"),
                        "--state-out",
                        str(second_state),
                    ]
                )
            second_payload = json.loads(stdout.getvalue())

            self.assertEqual(first_exit, 0)
            self.assertEqual(second_exit, 0)
            self.assertEqual(
                first_payload["state_sha256"], second_payload["state_sha256"]
            )
            self.assertEqual(second_payload["recorded_event_count"], 9)
            self.assertEqual(
                second_payload["reconciliation"]["status"], "pass"
            )
            self.assertFalse(second_payload["automatic_execution_allowed"])

    def test_paper_audit_cli_writes_checksummed_review_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            state_path = temporary / "state.json"
            report_path = temporary / "report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                replay_exit = main(
                    [
                        "replay-paper-events",
                        "--initial-cash",
                        "100000",
                        "--events",
                        str(ROOT / "examples/paper/audit_events.jsonl"),
                        "--state-out",
                        str(state_path),
                    ]
                )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                audit_exit = main(
                    [
                        "audit-paper-state",
                        "--state",
                        str(state_path),
                        "--audit-input",
                        str(ROOT / "examples/paper/audit_input.json"),
                        "--report-out",
                        str(report_path),
                    ]
                )

            output = json.loads(stdout.getvalue())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(replay_exit, 0)
            self.assertEqual(audit_exit, 0)
            self.assertEqual(output["status"], "review_required")
            self.assertEqual(output["report_sha256"], report["report_sha256"])
            self.assertFalse(report["automatic_execution_allowed"])


if __name__ == "__main__":
    unittest.main()
