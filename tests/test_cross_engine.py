import contextlib
import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from lets_quant.cli import main
from lets_quant.cross_engine import (
    EngineValidationError,
    file_sha256,
    read_nav_rows,
    read_trade_rows,
    reconcile_engine_candidate,
    summarize_candidate,
    write_engine_candidate,
)


ROOT = Path(__file__).resolve().parents[1]


class CrossEngineTest(unittest.TestCase):
    def _reference_run(self, temporary: Path) -> Path:
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
                    str(temporary / "reference"),
                ]
            )
        self.assertEqual(exit_code, 0, stdout.getvalue())
        return Path(json.loads(stdout.getvalue())["artifact_directory"])

    def _candidate_run(self, temporary: Path, reference: Path) -> Path:
        nav_rows = read_nav_rows(reference / "nav.csv")
        trade_rows = read_trade_rows(reference / "trades.csv")
        return write_engine_candidate(
            reference_directory=reference,
            output_root=temporary / "candidates",
            engine={
                "name": "test-independent-engine",
                "version": "1.0",
                "adapter_version": "1",
            },
            nav_rows=nav_rows,
            trade_rows=trade_rows,
            metrics=summarize_candidate(nav_rows, trade_rows),
            validation_scope={
                "input": "frozen_order_intents",
                "validated_components": ["fixture normalization"],
                "excluded_components": ["strategy validity"],
            },
            limitations=["test fixture only"],
        )

    def test_identical_candidate_passes_and_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._candidate_run(temporary, reference)

            report = reconcile_engine_candidate(reference, candidate)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["summary"]["blocked_check_count"], 0)
            self.assertFalse(report["investment_validity_established"])
            self.assertFalse(report["automatic_execution_allowed"])
            report_path = temporary / "report.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "reconcile-engine",
                        "--reference-run",
                        str(reference),
                        "--candidate-run",
                        str(candidate),
                        "--report-out",
                        str(report_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0, stdout.getvalue())
            self.assertEqual(payload["status"], "pass")
            self.assertTrue(report_path.exists())

    def test_candidate_file_tampering_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._candidate_run(temporary, reference)
            nav_path = candidate / "nav.csv"
            nav_path.write_text(
                nav_path.read_text(encoding="utf-8").replace(
                    "103672.92993430", "103772.92993430"
                ),
                encoding="utf-8",
            )

            report = reconcile_engine_candidate(reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                checks["candidate_file_integrity"]["status"], "blocked"
            )
            self.assertEqual(checks["nav_and_cash"]["status"], "blocked")

    def test_hash_consistent_result_drift_is_still_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._candidate_run(temporary, reference)
            nav_path = candidate / "nav.csv"
            with nav_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            rows[-1]["cash"] = f"{float(rows[-1]['cash']) + 10:.8f}"
            with nav_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["date", "nav", "cash", "positions"]
                )
                writer.writeheader()
                writer.writerows(rows)
            manifest_path = candidate / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["nav.csv"] = file_sha256(nav_path)
            identity_payload = {
                "engine": manifest["engine"],
                "reference": manifest["reference"],
                "files": manifest["files"],
                "validation_scope": manifest["validation_scope"],
                "limitations": manifest["limitations"],
            }
            manifest["candidate_id"] = hashlib.sha256(
                json.dumps(
                    identity_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            report = reconcile_engine_candidate(reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                checks["candidate_file_integrity"]["status"], "pass"
            )
            self.assertEqual(checks["nav_and_cash"]["status"], "blocked")

    def test_reference_input_drift_fails_integrity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._candidate_run(temporary, reference)
            signals_path = reference / "signals.csv"
            signals_path.write_text(
                signals_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                EngineValidationError,
                "reference artifact integrity failed for signals.csv",
            ):
                reconcile_engine_candidate(reference, candidate)

    def test_candidate_bound_to_another_intact_run_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            first_reference = self._reference_run(temporary)
            candidate = self._candidate_run(temporary, first_reference)
            second_reference = self._reference_run(temporary)

            report = reconcile_engine_candidate(second_reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(checks["reference_binding"]["status"], "blocked")
            self.assertEqual(checks["nav_and_cash"]["status"], "pass")

    def test_non_finite_tolerance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._candidate_run(temporary, reference)

            for tolerance in (float("nan"), float("inf")):
                with self.subTest(tolerance=tolerance):
                    with self.assertRaisesRegex(
                        EngineValidationError,
                        "money_tolerance must be finite",
                    ):
                        reconcile_engine_candidate(
                            reference,
                            candidate,
                            money_tolerance=tolerance,
                        )


if __name__ == "__main__":
    unittest.main()
