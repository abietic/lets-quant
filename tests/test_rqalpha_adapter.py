import contextlib
import csv
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from lets_quant.cli import main


ROOT = Path(__file__).resolve().parents[1]
RQALPHA_AVAILABLE = importlib.util.find_spec("rqalpha") is not None


@unittest.skipUnless(RQALPHA_AVAILABLE, "rqalpha optional dependency missing")
class RqalphaAdapterTest(unittest.TestCase):
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

    def _validate(
        self,
        temporary: Path,
        reference: Path,
        liquidity: Path = None,
    ) -> tuple:
        args = [
            "validate-rqalpha",
            "--reference-run",
            str(reference),
            "--prices",
            str(ROOT / "examples/prices.csv"),
            "--output-root",
            str(temporary / "candidate"),
        ]
        if liquidity is not None:
            args.extend(["--liquidity", str(liquidity)])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(args)
        return exit_code, json.loads(stdout.getvalue())

    def test_native_order_lifecycle_reconciles_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)

            exit_code, payload = self._validate(temporary, reference)

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["summary"]["blocked_check_count"], 0)
            self.assertEqual(payload["summary"]["max_abs_nav_difference"], 0.0)
            lifecycle = payload["summary"]["order_lifecycle"]
            self.assertTrue(lifecycle["present"])
            self.assertEqual(lifecycle["order_count"], 7)
            self.assertEqual(lifecycle["partial_order_count"], 1)
            candidate = Path(payload["candidate_directory"])
            events = (candidate / "events.csv").read_text(encoding="utf-8")
            self.assertIn("order_unsolicited_update", events)
            self.assertIn("fill 3800 actually", events)

    def test_liquidity_partial_fill_stays_internally_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            prices_path = ROOT / "examples/prices.csv"
            with prices_path.open(newline="", encoding="utf-8") as handle:
                price_rows = list(csv.DictReader(handle))
            liquidity = temporary / "liquidity.csv"
            with liquidity.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["date", "symbol", "volume"]
                )
                writer.writeheader()
                for row in price_rows:
                    if row["symbol"] == "BENCH":
                        continue
                    volume = (
                        1000
                        if row["date"] == "2025-01-03"
                        and row["symbol"] == "ASSET_A"
                        else 10**12
                    )
                    writer.writerow(
                        {
                            "date": row["date"],
                            "symbol": row["symbol"],
                            "volume": volume,
                        }
                    )

            exit_code, payload = self._validate(
                temporary, reference, liquidity=liquidity
            )

            self.assertEqual(exit_code, 3, payload)
            self.assertEqual(payload["status"], "blocked")
            report = json.loads(
                Path(payload["report_path"]).read_text(encoding="utf-8")
            )
            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(checks["order_lifecycle"]["status"], "pass")
            self.assertEqual(checks["trades_and_costs"]["status"], "blocked")

    def test_zero_affordable_quantity_is_a_native_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            policy = json.loads(
                (ROOT / "config/policy.example.json").read_text(
                    encoding="utf-8"
                )
            )
            policy["name"] = "rqalpha-zero-affordable-quantity"
            policy["strategy"]["target_weights"] = {"ONLY": 0.01}
            policy["portfolio"].update(
                {
                    "initial_cash": 1000,
                    "benchmark": None,
                    "cash_buffer_weight": 0.99,
                }
            )
            policy["execution"]["lot_size"] = 1
            policy_path = temporary / "policy.json"
            policy_path.write_text(
                json.dumps(policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            prices_path = temporary / "prices.csv"
            prices_path.write_text(
                "date,symbol,close\n"
                "2025-01-02,ONLY,10.00\n"
                "2025-01-03,ONLY,10.00\n",
                encoding="utf-8",
            )
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
            self.assertEqual(backtest_exit, 0, stdout.getvalue())
            reference = json.loads(stdout.getvalue())["artifact_directory"]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                validation_exit = main(
                    [
                        "validate-rqalpha",
                        "--reference-run",
                        reference,
                        "--prices",
                        str(prices_path),
                        "--output-root",
                        str(temporary / "candidate"),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(validation_exit, 0, payload)
            self.assertEqual(payload["status"], "pass")
            lifecycle = payload["summary"]["order_lifecycle"]
            self.assertEqual(lifecycle["rejected_order_count"], 1)
            candidate = Path(payload["candidate_directory"])
            events = (candidate / "events.csv").read_text(encoding="utf-8")
            self.assertIn("REJECTED", events)


if __name__ == "__main__":
    unittest.main()
