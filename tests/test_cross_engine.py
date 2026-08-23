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
    read_signal_rows,
    read_trade_rows,
    reconcile_engine_candidate,
    summarize_candidate,
    write_engine_candidate,
)

from tests.engine_helpers import build_curated_reference


ROOT = Path(__file__).resolve().parents[1]


class CrossEngineTest(unittest.TestCase):
    def _reference_run(
        self, temporary: Path, policy_path: Path = None
    ) -> Path:
        policy_path = policy_path or ROOT / "config/policy.example.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "backtest",
                    "--policy",
                    str(policy_path),
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

    def _corporate_action_candidate_run(
        self,
        temporary: Path,
        reference: Path,
        *,
        cash_delta: float = 10.0,
    ) -> Path:
        nav_rows = read_nav_rows(reference / "nav.csv")
        trade_rows = read_trade_rows(reference / "trades.csv")
        action_rows = [
            {
                "trading_date": "2025-01-07",
                "symbol": "511010.XSHG",
                "source_event_type": "cash_dividend",
                "accounting_event_type": "cash_dividend",
                "quantity_delta": 0,
                "cash_delta": cash_delta,
                "cash_amount": 0.05,
                "ratio": None,
                "reference_id": (
                    "corporate_action:2025-01-07:"
                    "511010.XSHG:cash_dividend"
                ),
            }
        ]
        return write_engine_candidate(
            reference_directory=reference,
            output_root=temporary / "action-candidates",
            engine={
                "name": "test-action-engine",
                "version": "1.0",
                "adapter_version": "1",
            },
            nav_rows=nav_rows,
            trade_rows=trade_rows,
            corporate_action_rows=action_rows,
            metrics=summarize_candidate(nav_rows, trade_rows, action_rows),
            validation_scope={
                "input": "frozen_order_intents",
                "validated_components": ["corporate action accounting"],
                "excluded_components": ["strategy validity"],
            },
            limitations=["test fixture only"],
        )

    def _policy_candidate_run(
        self, temporary: Path, reference: Path
    ) -> Path:
        nav_rows = read_nav_rows(reference / "nav.csv")
        trade_rows = read_trade_rows(reference / "trades.csv")
        signal_rows = read_signal_rows(reference / "signals.csv")
        return write_engine_candidate(
            reference_directory=reference,
            output_root=temporary / "policy-candidates",
            engine={
                "name": "test-policy-engine",
                "version": "1.0",
                "adapter_version": "1",
            },
            nav_rows=nav_rows,
            trade_rows=trade_rows,
            signal_rows=signal_rows,
            metrics=summarize_candidate(nav_rows, trade_rows),
            validation_scope={
                "input": "independent_policy",
                "validated_components": ["policy decisions"],
                "excluded_components": ["market data validity"],
            },
            limitations=["test fixture only"],
        )

    def _refresh_candidate_identity(
        self, candidate: Path, changed_file: str
    ) -> None:
        manifest_path = candidate / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][changed_file] = file_sha256(candidate / changed_file)
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

    def _lifecycle_candidate_run(
        self,
        temporary: Path,
        reference: Path,
        *,
        precheck_reject_first: bool = False,
    ) -> Path:
        nav_rows = read_nav_rows(reference / "nav.csv")
        trade_rows = read_trade_rows(reference / "trades.csv")
        order_rows = []
        event_rows = []
        sequence = 1
        for index, trade in enumerate(trade_rows, start=1):
            order_id = f"fixture-order-{index}"
            precheck_rejected = precheck_reject_first and index == 1
            if precheck_rejected:
                trade.update(
                    {
                        "filled_quantity": 0,
                        "notional": 0.0,
                        "commission": 0.0,
                        "tax": 0.0,
                        "slippage": 0.0,
                        "status": "rejected_not_tradable",
                    }
                )
                final_status = "REJECTED"
                events = [
                    (
                        "order_tradability_reject",
                        "REJECTED",
                        0,
                        0.0,
                        0.0,
                        0.0,
                    )
                ]
            else:
                full = (
                    trade["filled_quantity"] == trade["requested_quantity"]
                )
                final_status = "FILLED" if full else "CANCELLED"
                events = [
                    (
                        "order_pending_new",
                        "PENDING_NEW",
                        0,
                        0.0,
                        0.0,
                        0.0,
                    ),
                    (
                        "order_creation_pass",
                        "ACTIVE",
                        0,
                        0.0,
                        0.0,
                        0.0,
                    ),
                    (
                        "trade",
                        "FILLED" if full else "ACTIVE",
                        trade["filled_quantity"],
                        trade["fill_price"],
                        trade["commission"],
                        trade["tax"],
                    ),
                ]
            if not precheck_rejected and not full:
                events.append(
                    (
                        "order_unsolicited_update",
                        "CANCELLED",
                        0,
                        0.0,
                        0.0,
                        0.0,
                    )
                )
            cumulative = 0
            for event_type, status, fill, price, commission, tax in events:
                cumulative += fill
                event_rows.append(
                    {
                        "sequence": sequence,
                        "event_time": (
                            f"{trade['execution_date']}T15:00:00+08:00"
                        ),
                        "event_type": event_type,
                        "order_id": order_id,
                        "trade_id": (
                            f"fixture-trade-{index}"
                            if event_type == "trade"
                            else ""
                        ),
                        "symbol": trade["symbol"],
                        "side": trade["side"],
                        "requested_quantity": trade["requested_quantity"],
                        "cumulative_filled_quantity": cumulative,
                        "event_fill_quantity": fill,
                        "order_status": status,
                        "fill_price": price,
                        "commission": commission,
                        "tax": tax,
                        "message": "",
                    }
                )
                sequence += 1
            order_rows.append(
                {
                    "order_id": order_id,
                    "signal_date": trade["signal_date"],
                    "execution_date": trade["execution_date"],
                    "symbol": trade["symbol"],
                    "side": trade["side"],
                    "requested_quantity": trade["requested_quantity"],
                    "filled_quantity": trade["filled_quantity"],
                    "avg_fill_price": (
                        0.0 if precheck_rejected else trade["fill_price"]
                    ),
                    "commission": trade["commission"],
                    "tax": trade["tax"],
                    "final_status": final_status,
                    "event_count": len(events),
                    "trade_count": 0 if precheck_rejected else 1,
                }
            )
        return write_engine_candidate(
            reference_directory=reference,
            output_root=temporary / "lifecycle-candidates",
            engine={
                "name": "test-event-engine",
                "version": "1.0",
                "adapter_version": "1",
            },
            nav_rows=nav_rows,
            trade_rows=trade_rows,
            metrics=summarize_candidate(nav_rows, trade_rows),
            order_rows=order_rows,
            event_rows=event_rows,
            validation_scope={
                "input": "frozen_order_intents",
                "validated_components": ["native order lifecycle"],
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

    def test_policy_signal_candidate_reconciles_decisions_and_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._policy_candidate_run(temporary, reference)

            report = reconcile_engine_candidate(reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "pass")
            self.assertEqual(checks["policy_decisions"]["status"], "pass")
            summary = report["summary"]["policy_decisions"]
            self.assertTrue(summary["present"])
            self.assertEqual(summary["candidate_signal_count"], 4)
            self.assertEqual(summary["candidate_decision_count"], 4)

    def test_blocked_signal_preserves_proposed_orders_for_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            policy = json.loads(
                (ROOT / "config/policy.example.json").read_text(
                    encoding="utf-8"
                )
            )
            policy["name"] = "turnover-blocked-candidate"
            policy["risk"]["max_turnover_per_rebalance"] = 0.1
            policy_path = temporary / "policy.json"
            policy_path.write_text(
                json.dumps(policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            reference = self._reference_run(temporary, policy_path)

            reference_signals = read_signal_rows(reference / "signals.csv")
            self.assertTrue(reference_signals)
            self.assertEqual(reference_signals[0]["status"], "blocked")
            self.assertTrue(reference_signals[0]["orders"])

            candidate = self._policy_candidate_run(temporary, reference)
            report = reconcile_engine_candidate(reference, candidate)
            self.assertEqual(report["status"], "pass")

    def test_hash_consistent_policy_signal_drift_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._policy_candidate_run(temporary, reference)
            signals_path = candidate / "signals.csv"
            signals_path.write_text(
                signals_path.read_text(encoding="utf-8").replace(
                    "passed pre-trade risk checks",
                    "candidate decision drift",
                    1,
                ),
                encoding="utf-8",
            )
            self._refresh_candidate_identity(candidate, "signals.csv")

            report = reconcile_engine_candidate(reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                checks["candidate_file_integrity"]["status"], "pass"
            )
            self.assertEqual(
                checks["policy_decisions"]["status"], "blocked"
            )

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

    def test_reference_corporate_actions_require_candidate_evidence(self) -> None:
        for adjustment in ("none", "hfq"):
            with self.subTest(adjustment=adjustment):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temporary = Path(temp_dir)
                    fixture = build_curated_reference(
                        temporary / "fixture", adjustment=adjustment
                    )
                    candidate = self._candidate_run(
                        temporary, fixture["reference"]
                    )

                    report = reconcile_engine_candidate(
                        fixture["reference"], candidate
                    )

                    checks = {
                        check["name"]: check for check in report["checks"]
                    }
                    self.assertEqual(report["status"], "blocked")
                    self.assertEqual(
                        checks["candidate_file_integrity"]["status"],
                        "blocked",
                    )
                    mismatches = checks["candidate_file_integrity"][
                        "details"
                    ]["mismatches"]
                    self.assertTrue(
                        any(
                            item["field"] == "corporate_actions.csv"
                            for item in mismatches
                        )
                    )

    def test_explicit_corporate_action_evidence_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            fixture = build_curated_reference(
                temporary / "fixture", adjustment="none"
            )
            candidate = self._corporate_action_candidate_run(
                temporary, fixture["reference"]
            )

            report = reconcile_engine_candidate(
                fixture["reference"], candidate
            )

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "pass")
            self.assertEqual(checks["corporate_actions"]["status"], "pass")
            summary = report["summary"]["corporate_actions"]
            self.assertTrue(summary["present"])
            self.assertEqual(summary["reference_explicit_action_count"], 1)
            self.assertEqual(summary["candidate_explicit_action_count"], 1)

    def test_hash_consistent_corporate_action_drift_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            fixture = build_curated_reference(
                temporary / "fixture", adjustment="none"
            )
            candidate = self._corporate_action_candidate_run(
                temporary, fixture["reference"]
            )
            actions_path = candidate / "corporate_actions.csv"
            actions_path.write_text(
                actions_path.read_text(encoding="utf-8").replace(
                    "10.00000000", "11.00000000", 1
                ),
                encoding="utf-8",
            )
            self._refresh_candidate_identity(
                candidate, "corporate_actions.csv"
            )

            report = reconcile_engine_candidate(
                fixture["reference"], candidate
            )

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                checks["candidate_file_integrity"]["status"], "pass"
            )
            self.assertEqual(checks["corporate_actions"]["status"], "blocked")

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

    def test_order_lifecycle_evidence_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._lifecycle_candidate_run(temporary, reference)

            report = reconcile_engine_candidate(reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "pass")
            self.assertEqual(checks["order_lifecycle"]["status"], "pass")
            self.assertEqual(
                report["summary"]["order_lifecycle"]["partial_order_count"],
                1,
            )

    def test_precheck_tradability_rejection_is_a_single_final_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._lifecycle_candidate_run(
                temporary,
                reference,
                precheck_reject_first=True,
            )

            report = reconcile_engine_candidate(reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(checks["order_lifecycle"]["status"], "pass")
            self.assertEqual(
                report["summary"]["order_lifecycle"]["rejected_order_count"],
                1,
            )

            events_path = candidate / "events.csv"
            events_path.write_text(
                events_path.read_text(encoding="utf-8").replace(
                    "order_tradability_reject", "order_pending_new", 1
                ),
                encoding="utf-8",
            )
            self._refresh_candidate_identity(candidate, "events.csv")

            tampered = reconcile_engine_candidate(reference, candidate)
            tampered_checks = {
                check["name"]: check for check in tampered["checks"]
            }
            self.assertEqual(
                tampered_checks["order_lifecycle"]["status"], "blocked"
            )

    def test_hash_consistent_lifecycle_drift_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._lifecycle_candidate_run(temporary, reference)
            orders_path = candidate / "orders.csv"
            orders_path.write_text(
                orders_path.read_text(encoding="utf-8").replace(
                    "CANCELLED,4,1", "FILLED,4,1"
                ),
                encoding="utf-8",
            )
            self._refresh_candidate_identity(candidate, "orders.csv")

            report = reconcile_engine_candidate(reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                checks["candidate_file_integrity"]["status"], "pass"
            )
            self.assertEqual(checks["order_lifecycle"]["status"], "blocked")

    def test_hash_consistent_non_trade_cost_drift_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            reference = self._reference_run(temporary)
            candidate = self._lifecycle_candidate_run(temporary, reference)
            events_path = candidate / "events.csv"
            events_path.write_text(
                events_path.read_text(encoding="utf-8").replace(
                    ",PENDING_NEW,0.00000000,0.00000000,0.00000000,",
                    ",PENDING_NEW,0.00000000,1.00000000,0.00000000,",
                    1,
                ),
                encoding="utf-8",
            )
            self._refresh_candidate_identity(candidate, "events.csv")

            report = reconcile_engine_candidate(reference, candidate)

            checks = {check["name"]: check for check in report["checks"]}
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                checks["candidate_file_integrity"]["status"], "pass"
            )
            self.assertEqual(checks["order_lifecycle"]["status"], "blocked")
            mismatches = checks["order_lifecycle"]["details"]["mismatches"]
            self.assertTrue(
                any(item["field"] == "non_trade_commission" for item in mismatches)
            )


if __name__ == "__main__":
    unittest.main()
