import contextlib
import csv
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from lets_quant.cli import main

from tests.engine_helpers import build_curated_reference


ROOT = Path(__file__).resolve().parents[1]
RQALPHA_AVAILABLE = importlib.util.find_spec("rqalpha") is not None


@unittest.skipUnless(RQALPHA_AVAILABLE, "rqalpha optional dependency missing")
class RqalphaAdapterTest(unittest.TestCase):
    def _reference_run(
        self,
        temporary: Path,
        *,
        policy_path: Path = None,
        prices_path: Path = None,
        initial_holdings_path: Path = None,
    ) -> Path:
        policy_path = policy_path or ROOT / "config/policy.example.json"
        prices_path = prices_path or ROOT / "examples/prices.csv"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            args = [
                    "backtest",
                    "--policy",
                    str(policy_path),
                    "--prices",
                    str(prices_path),
                    "--output-root",
                    str(temporary / "reference"),
                ]
            if initial_holdings_path is not None:
                args.extend(
                    ["--initial-holdings", str(initial_holdings_path)]
                )
            exit_code = main(args)
        self.assertEqual(exit_code, 0, stdout.getvalue())
        return Path(json.loads(stdout.getvalue())["artifact_directory"])

    def _validate(
        self,
        temporary: Path,
        reference: Path,
        liquidity: Path = None,
        prices_path: Path = None,
        decision_mode: str = None,
    ) -> tuple:
        prices_path = prices_path or ROOT / "examples/prices.csv"
        args = [
            "validate-rqalpha",
            "--reference-run",
            str(reference),
            "--prices",
            str(prices_path),
            "--output-root",
            str(temporary / "candidate"),
        ]
        if liquidity is not None:
            args.extend(["--liquidity", str(liquidity)])
        if decision_mode is not None:
            args.extend(["--decision-mode", decision_mode])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(args)
        return exit_code, json.loads(stdout.getvalue())

    def test_native_order_lifecycle_reconciles_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)

            with patch(
                "lets_quant.rqalpha_adapter.load_frozen_order_intents",
                side_effect=AssertionError(
                    "independent mode read frozen reference orders"
                ),
            ):
                exit_code, payload = self._validate(temporary, reference)

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["summary"]["blocked_check_count"], 0)
            self.assertEqual(payload["summary"]["max_abs_nav_difference"], 0.0)
            decisions = payload["summary"]["policy_decisions"]
            self.assertTrue(decisions["present"])
            self.assertEqual(decisions["candidate_signal_count"], 4)
            self.assertEqual(decisions["candidate_decision_count"], 4)
            lifecycle = payload["summary"]["order_lifecycle"]
            self.assertTrue(lifecycle["present"])
            self.assertEqual(lifecycle["order_count"], 7)
            self.assertEqual(lifecycle["partial_order_count"], 1)
            candidate = Path(payload["candidate_directory"])
            events = (candidate / "events.csv").read_text(encoding="utf-8")
            self.assertIn("order_unsolicited_update", events)
            self.assertIn("fill 3800 actually", events)

    def test_nonzero_initial_holdings_use_native_account_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(
                temporary,
                initial_holdings_path=ROOT / "examples/holdings.csv",
            )

            exit_code, payload = self._validate(temporary, reference)

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["summary"]["max_abs_nav_difference"], 0)
            self.assertEqual(
                payload["summary"]["policy_decisions"]["mismatches"], []
            )
            candidate = Path(payload["candidate_directory"])
            manifest = json.loads(
                (candidate / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["validation_scope"]["initial_position_count"], 3
            )

    def test_first_day_split_applies_to_native_opening_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            holdings_path = temporary / "holdings.csv"
            holdings_path.write_text(
                "symbol,quantity\n510300.XSHG,100\n",
                encoding="utf-8",
            )
            fixture = build_curated_reference(
                temporary / "fixture",
                adjustment="none",
                corporate_action_rows=[
                    {
                        "symbol": "510300.XSHG",
                        "event_type": "split",
                        "ex_date": "2025-01-02",
                        "announced_at": "2025-01-01T09:00:00+08:00",
                        "cash_amount": "",
                        "ratio": "2",
                        "available_at": "2025-01-01T09:00:00+08:00",
                    }
                ],
                initial_holdings_path=holdings_path,
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "validate-rqalpha",
                        "--reference-run",
                        str(fixture["reference"]),
                        "--dataset",
                        str(fixture["dataset"]),
                        "--output-root",
                        str(temporary / "candidate"),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            candidate = Path(payload["candidate_directory"])
            with (candidate / "nav.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                first_nav = next(csv.DictReader(handle))
            self.assertEqual(
                json.loads(first_nav["positions"])["510300.XSHG"], 200
            )
            with (candidate / "corporate_actions.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                action = next(csv.DictReader(handle))
            self.assertEqual(action["quantity_delta"], "100")

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
            self.assertEqual(checks["policy_decisions"]["status"], "blocked")
            self.assertTrue(
                all(
                    mismatch["signal_index"] > 0
                    for mismatch in checks["policy_decisions"]["details"][
                        "mismatches"
                    ]
                )
            )
            self.assertEqual(checks["order_lifecycle"]["status"], "pass")
            self.assertEqual(checks["trades_and_costs"]["status"], "blocked")

    def test_frozen_orders_mode_remains_execution_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)

            exit_code, payload = self._validate(
                temporary,
                reference,
                decision_mode="frozen_orders",
            )

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertFalse(payload["summary"]["policy_decisions"]["present"])
            report = json.loads(
                Path(payload["report_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["validation_scope"]["input"], "frozen_orders"
            )

    def test_momentum_policy_is_generated_from_point_in_time_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            policy = json.loads(
                (ROOT / "config/policy.example.json").read_text(
                    encoding="utf-8"
                )
            )
            policy["name"] = "rqalpha-momentum-pit"
            policy["strategy"] = {
                "kind": "momentum_filter",
                "target_weights": {"UP": 0.5, "DOWN": 0.4},
                "rebalance_every_n_trading_days": 10,
                "lookback_trading_days": 20,
                "minimum_momentum": 0.0,
            }
            policy["portfolio"].update(
                {"benchmark": None, "cash_buffer_weight": 0.1}
            )
            policy["execution"]["lot_size"] = 10
            policy_path = temporary / "momentum-policy.json"
            policy_path.write_text(
                json.dumps(policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            prices_path = temporary / "momentum-prices.csv"
            trading_dates = []
            candidate = date(2025, 1, 2)
            while len(trading_dates) < 45:
                if candidate.weekday() < 5:
                    trading_dates.append(candidate)
                candidate += timedelta(days=1)
            with prices_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["date", "symbol", "close"]
                )
                writer.writeheader()
                for index, trading_date in enumerate(trading_dates):
                    writer.writerow(
                        {
                            "date": trading_date.isoformat(),
                            "symbol": "UP",
                            "close": 100 + index,
                        }
                    )
                    writer.writerow(
                        {
                            "date": trading_date.isoformat(),
                            "symbol": "DOWN",
                            "close": 100 - index * 0.5,
                        }
                    )

            reference = self._reference_run(
                temporary,
                policy_path=policy_path,
                prices_path=prices_path,
            )
            exit_code, payload = self._validate(
                temporary,
                reference,
                prices_path=prices_path,
            )

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            decisions = payload["summary"]["policy_decisions"]
            self.assertTrue(decisions["present"])
            self.assertEqual(decisions["candidate_signal_count"], 5)
            self.assertEqual(decisions["candidate_decision_count"], 5)
            self.assertGreater(decisions["candidate_accepted_count"], 0)
            candidate_directory = Path(payload["candidate_directory"])
            with (candidate_directory / "signals.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                signal_rows = list(csv.DictReader(handle))
            self.assertEqual(signal_rows[0]["status"], "blocked")
            self.assertIn("warm-up", signal_rows[0]["reason"])
            selected = json.loads(signal_rows[2]["decision_evidence"])
            self.assertEqual(selected["selected_symbols"], ["UP"])

    def test_drawdown_freeze_tracks_peaks_between_signal_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            policy = json.loads(
                (ROOT / "config/policy.example.json").read_text(
                    encoding="utf-8"
                )
            )
            policy["name"] = "rqalpha-daily-drawdown-peak"
            policy["strategy"] = {
                "kind": "fixed_weight",
                "target_weights": {"ONLY": 0.9},
                "rebalance_every_n_trading_days": 3,
            }
            policy["portfolio"].update(
                {"benchmark": None, "cash_buffer_weight": 0.1}
            )
            policy["execution"]["lot_size"] = 1
            policy["risk"].update(
                {
                    "max_single_weight": 0.9,
                    "max_gross_exposure": 0.9,
                    "max_drawdown": 0.1,
                }
            )
            policy_path = temporary / "drawdown-policy.json"
            policy_path.write_text(
                json.dumps(policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            prices_path = temporary / "drawdown-prices.csv"
            trading_dates = []
            candidate = date(2025, 1, 2)
            while len(trading_dates) < 8:
                if candidate.weekday() < 5:
                    trading_dates.append(candidate)
                candidate += timedelta(days=1)
            closes = [10.0, 10.0, 20.0, 17.0, 17.0, 17.0, 17.0, 17.0]
            with prices_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["date", "symbol", "close"]
                )
                writer.writeheader()
                for trading_date, close in zip(trading_dates, closes):
                    writer.writerow(
                        {
                            "date": trading_date.isoformat(),
                            "symbol": "ONLY",
                            "close": close,
                        }
                    )

            reference = self._reference_run(
                temporary,
                policy_path=policy_path,
                prices_path=prices_path,
            )
            exit_code, payload = self._validate(
                temporary,
                reference,
                prices_path=prices_path,
            )

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            candidate_directory = Path(payload["candidate_directory"])
            with (candidate_directory / "signals.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                signal_rows = list(csv.DictReader(handle))
            self.assertEqual(signal_rows[1]["status"], "blocked")
            self.assertEqual(
                signal_rows[1]["reason"],
                "maximum drawdown risk freeze is active",
            )

    def test_curated_ohlcv_suspension_uses_native_rejection(self) -> None:
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
                        "validate-rqalpha",
                        "--reference-run",
                        str(fixture["reference"]),
                        "--dataset",
                        str(fixture["dataset"]),
                        "--output-root",
                        str(temporary / "candidate"),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            lifecycle = payload["summary"]["order_lifecycle"]
            self.assertEqual(lifecycle["rejected_order_count"], 1)
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
            events = (candidate / "events.csv").read_text(encoding="utf-8")
            self.assertIn("order_tradability_reject", events)
            manifest = json.loads(
                (candidate / "manifest.json").read_text(encoding="utf-8")
            )
            scope = manifest["validation_scope"]
            self.assertEqual(scope["bar_mapping"], "curated_ohlcv")

    def test_unadjusted_cash_dividend_uses_native_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            fixture = build_curated_reference(
                temporary,
                adjustment="none",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "validate-rqalpha",
                        "--reference-run",
                        str(fixture["reference"]),
                        "--dataset",
                        str(fixture["dataset"]),
                        "--output-root",
                        str(temporary / "candidate"),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["summary"]["max_abs_cash_difference"], 0)
            action_summary = payload["summary"]["corporate_actions"]
            self.assertEqual(action_summary["candidate_explicit_action_count"], 1)
            candidate = Path(payload["candidate_directory"])
            with (candidate / "corporate_actions.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                actions = list(csv.DictReader(handle))
            self.assertEqual(actions[0]["accounting_event_type"], "cash_dividend")
            self.assertEqual(actions[0]["cash_delta"], "10.00000000")

    def test_unadjusted_split_and_reverse_split_use_native_positions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            fixture = build_curated_reference(
                temporary,
                adjustment="none",
                corporate_action_rows=[
                    {
                        "symbol": "510300.XSHG",
                        "event_type": "split",
                        "ex_date": "2025-01-06",
                        "announced_at": "2025-01-02T09:00:00+08:00",
                        "cash_amount": "",
                        "ratio": "2",
                        "available_at": "2025-01-02T09:00:00+08:00",
                    },
                    {
                        "symbol": "510300.XSHG",
                        "event_type": "reverse_split",
                        "ex_date": "2025-01-07",
                        "announced_at": "2025-01-02T09:00:00+08:00",
                        "cash_amount": "",
                        "ratio": "0.5",
                        "available_at": "2025-01-02T09:00:00+08:00",
                    },
                ],
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "validate-rqalpha",
                        "--reference-run",
                        str(fixture["reference"]),
                        "--dataset",
                        str(fixture["dataset"]),
                        "--output-root",
                        str(temporary / "candidate"),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["summary"]["max_abs_nav_difference"], 0)
            candidate = Path(payload["candidate_directory"])
            with (candidate / "corporate_actions.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                actions = list(csv.DictReader(handle))
            self.assertEqual(
                [row["accounting_event_type"] for row in actions],
                ["split", "reverse_split"],
            )
            self.assertGreater(int(actions[0]["quantity_delta"]), 0)
            self.assertLess(int(actions[1]["quantity_delta"]), 0)

    def test_cross_action_intent_is_rejected_before_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            fixture = build_curated_reference(
                temporary,
                adjustment="none",
                corporate_action_rows=[
                    {
                        "symbol": "510300.XSHG",
                        "event_type": "split",
                        "ex_date": "2025-01-03",
                        "announced_at": "2025-01-02T09:00:00+08:00",
                        "cash_amount": "",
                        "ratio": "2",
                        "available_at": "2025-01-02T09:00:00+08:00",
                    }
                ],
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "validate-rqalpha",
                        "--reference-run",
                        str(fixture["reference"]),
                        "--dataset",
                        str(fixture["dataset"]),
                        "--output-root",
                        str(temporary / "candidate"),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(
                payload["summary"]["order_lifecycle"]["rejected_order_count"],
                1,
            )
            candidate = Path(payload["candidate_directory"])
            trades = (candidate / "trades.csv").read_text(encoding="utf-8")
            events = (candidate / "events.csv").read_text(encoding="utf-8")
            self.assertIn("rejected_corporate_action", trades)
            self.assertIn("order_corporate_action_reject", events)

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
