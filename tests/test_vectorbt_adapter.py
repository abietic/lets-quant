import contextlib
import csv
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from lets_quant.cli import main

from tests.engine_helpers import build_curated_reference


ROOT = Path(__file__).resolve().parents[1]
VECTORBT_AVAILABLE = importlib.util.find_spec("vectorbt") is not None


@unittest.skipUnless(VECTORBT_AVAILABLE, "vectorbt optional dependency missing")
class VectorbtAdapterTest(unittest.TestCase):
    def _run_validation(
        self, temporary: Path, policy_path: Path, prices_path: Path
    ) -> dict:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            backtest_exit = main(
                [
                    "backtest",
                    "--policy",
                    str(policy_path),
                    "--prices",
                    str(prices_path),
                    "--output-root",
                    str(temporary / "reference"),
                ]
            )
        reference = json.loads(stdout.getvalue())["artifact_directory"]
        self.assertEqual(backtest_exit, 0)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            validation_exit = main(
                [
                    "validate-vectorbt",
                    "--reference-run",
                    reference,
                    "--prices",
                    str(prices_path),
                    "--output-root",
                    str(temporary / "candidate"),
                ]
            )
        self.assertEqual(validation_exit, 0, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_frozen_orders_reconcile_with_zero_nav_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            payload = self._run_validation(
                temporary,
                ROOT / "config/policy.example.json",
                ROOT / "examples/prices.csv",
            )

            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["summary"]["blocked_check_count"], 0)
            self.assertEqual(payload["summary"]["max_abs_nav_difference"], 0.0)
            self.assertEqual(
                payload["summary"]["max_abs_cash_difference"], 0.0
            )
            candidate = Path(payload["candidate_directory"])
            manifest = json.loads(
                (candidate / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["engine"]["name"], "vectorbt")
            self.assertFalse(manifest["investment_validity_established"])
            self.assertIn(
                "strategy signal generation and point-in-time feature logic",
                manifest["validation_scope"]["excluded_components"],
            )

    def test_single_symbol_portfolio_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            policy = json.loads(
                (ROOT / "config/policy.example.json").read_text(
                    encoding="utf-8"
                )
            )
            policy["name"] = "single-symbol-vectorbt-test"
            policy["strategy"]["target_weights"] = {"ONLY": 0.5}
            policy["portfolio"]["benchmark"] = None
            policy_path = temporary / "policy.json"
            policy_path.write_text(
                json.dumps(policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            prices_path = temporary / "prices.csv"
            prices_path.write_text(
                "date,symbol,close\n"
                "2025-01-02,ONLY,10.00\n"
                "2025-01-03,ONLY,10.10\n"
                "2025-01-06,ONLY,10.20\n"
                "2025-01-07,ONLY,10.30\n",
                encoding="utf-8",
            )

            payload = self._run_validation(
                temporary, policy_path, prices_path
            )

            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["summary"]["candidate_nav_rows"], 4)

    def test_curated_suspension_is_rejected_without_losing_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            fixture = build_curated_reference(
                temporary,
                suspended_symbol="511010.XSHG",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "validate-vectorbt",
                        "--reference-run",
                        str(fixture["reference"]),
                        "--output-root",
                        str(temporary / "candidate"),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            candidate = Path(payload["candidate_directory"])
            with (candidate / "trades.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                trades = list(csv.DictReader(handle))
            rejected = [
                trade
                for trade in trades
                if trade["status"] == "rejected_not_tradable"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["symbol"], "511010.XSHG")
            manifest = json.loads(
                (candidate / "manifest.json").read_text(encoding="utf-8")
            )
            scope = manifest["validation_scope"]
            self.assertEqual(scope["reference_data_source"], "curated_dataset")
            self.assertEqual(
                scope["tradability_source"], "curated_observations"
            )


if __name__ == "__main__":
    unittest.main()
