import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from lets_quant.execution import (
    PaperAuditError,
    PaperExchange,
    audit_paper_exchange,
    load_paper_audit_input,
    replay_event_file,
    save_paper_audit_report,
)


ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime.fromisoformat("2025-01-03T09:35:00+08:00")


class PaperExecutionAuditTest(unittest.TestCase):
    def _load_payload(self) -> dict:
        return json.loads(
            (ROOT / "examples/paper/audit_input.json").read_text(
                encoding="utf-8"
            )
        )

    def _load_input(self, payload: dict):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "audit-input.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_paper_audit_input(path)

    def _fixture_exchange(self) -> PaperExchange:
        exchange = PaperExchange(initial_cash=100_000)
        replay_event_file(
            exchange, ROOT / "examples/paper/audit_events.jsonl"
        )
        return exchange

    def test_fixture_audit_reconciles_but_does_not_claim_broker_truth(self) -> None:
        audit_input = load_paper_audit_input(
            ROOT / "examples/paper/audit_input.json"
        )
        report = audit_paper_exchange(self._fixture_exchange(), audit_input)

        self.assertEqual(report["status"], "review_required")
        self.assertFalse(report["automatic_execution_allowed"])
        self.assertEqual(report["summary"]["critical_alert_count"], 0)
        self.assertEqual(report["summary"]["warning_alert_count"], 1)
        self.assertEqual(
            [alert["code"] for alert in report["alerts"]],
            ["external_account_fixture"],
        )
        self.assertEqual(report["account_reconciliation"]["status"], "match")
        self.assertEqual(len(report["execution_quality"]), 2)
        self.assertEqual(len(report["report_sha256"]), 64)

        repeated = audit_paper_exchange(self._fixture_exchange(), audit_input)
        self.assertEqual(repeated, report)

    def test_normalized_broker_snapshot_can_pass_without_enabling_execution(
        self,
    ) -> None:
        payload = self._load_payload()
        payload["external_account"]["source_kind"] = "broker"
        payload["external_account"]["source"] = "normalized-broker-export"

        report = audit_paper_exchange(
            self._fixture_exchange(), self._load_input(payload)
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["alerts"], [])
        self.assertFalse(report["manual_review_required"])
        self.assertFalse(report["automatic_execution_allowed"])

    def test_operational_hazards_block_and_remain_auditable(self) -> None:
        exchange = PaperExchange(initial_cash=10_000)
        submitted_at = datetime.fromisoformat("2025-01-03T08:00:00+08:00")
        exchange.submit(
            event_id="submit-open",
            client_order_id="open-order",
            symbol="ASSET_A",
            side="BUY",
            quantity=100,
            occurred_at=submitted_at,
        )
        exchange.acknowledge(
            event_id="ack-open",
            client_order_id="open-order",
            venue_order_id="venue-open",
            occurred_at=submitted_at,
        )
        payload = self._load_payload()
        payload["as_of"] = "2025-01-03T10:00:00+08:00"
        payload["thresholds"]["max_open_order_age_seconds"] = 300
        payload["thresholds"]["max_task_age_seconds"] = 300
        payload["quotes"] = []
        payload["order_expectations"] = [
            {
                "client_order_id": "open-order",
                "decision_id": "decision-open",
                "symbol": "ASSET_A",
                "side": "BUY",
                "expected_order_quantity": 100,
                "expected_fill_quantity": 100,
                "expected_average_fill_price": 10,
                "expected_fees": 1,
                "expected_terminal_status": "FILLED",
                "expected_fill_by": "2025-01-03T09:00:00+08:00",
            }
        ]
        payload["task_checks"] = [
            {
                "task_id": "paper-event-replay",
                "status": "timeout",
                "observed_at": "2025-01-03T09:00:00+08:00",
                "details": "fixture timeout",
            }
        ]
        payload["risk_state"] = {
            "frozen": True,
            "reasons": ["drawdown gate"],
        }
        payload["external_account"] = None

        report = audit_paper_exchange(exchange, self._load_input(payload))
        codes = {alert["code"] for alert in report["alerts"]}

        self.assertEqual(report["status"], "blocked")
        self.assertTrue(report["manual_review_required"])
        self.assertTrue(
            {
                "external_account_missing",
                "fill_quantity_deviation",
                "missing_quote",
                "open_order_stale",
                "risk_frozen",
                "task_failed",
                "task_observation_stale",
                "terminal_status_overdue",
            }.issubset(codes)
        )

    def test_fill_and_external_account_deviations_block(self) -> None:
        exchange = PaperExchange(initial_cash=10_000)
        submitted_at = datetime.fromisoformat("2025-01-03T09:30:00+08:00")
        exchange.submit(
            event_id="submit",
            client_order_id="order-1",
            symbol="ASSET_A",
            side="BUY",
            quantity=100,
            occurred_at=submitted_at,
        )
        exchange.acknowledge(
            event_id="ack",
            client_order_id="order-1",
            venue_order_id="venue-1",
            occurred_at=submitted_at,
        )
        exchange.fill(
            event_id="fill",
            client_order_id="order-1",
            fill_id="fill-1",
            quantity=100,
            price=11,
            commission=2,
            occurred_at=datetime.fromisoformat("2025-01-03T09:31:00+08:00"),
        )
        payload = self._load_payload()
        payload["order_expectations"] = [
            {
                "client_order_id": "order-1",
                "decision_id": "decision-1",
                "symbol": "ASSET_A",
                "side": "BUY",
                "expected_order_quantity": 100,
                "expected_fill_quantity": 100,
                "expected_average_fill_price": 10,
                "expected_fees": 1,
                "expected_terminal_status": "FILLED",
                "expected_fill_by": "2025-01-03T09:35:00+08:00",
            }
        ]
        payload["external_account"] = {
            "source": "mismatched-broker-export",
            "source_kind": "broker",
            "observed_at": "2025-01-03T09:34:00+08:00",
            "cash": 10_000,
            "positions": {},
            "orders": [
                {
                    "client_order_id": "order-1",
                    "status": "ACKNOWLEDGED",
                    "filled_quantity": 0,
                    "venue_order_id": "wrong-venue",
                }
            ],
        }

        report = audit_paper_exchange(exchange, self._load_input(payload))
        codes = {alert["code"] for alert in report["alerts"]}

        self.assertEqual(report["status"], "blocked")
        self.assertTrue(
            {
                "account_cash_mismatch",
                "account_order_mismatch",
                "account_position_mismatch",
                "fill_fee_deviation",
                "fill_price_deviation",
            }.issubset(codes)
        )
        quality = report["execution_quality"][0]
        self.assertAlmostEqual(quality["adverse_slippage_bps"], 1000)
        self.assertAlmostEqual(quality["fee_deviation"], 1)

    def test_strict_input_and_report_checksum_reject_ambiguous_state(self) -> None:
        payload = self._load_payload()
        payload["unexpected"] = True
        with self.assertRaisesRegex(PaperAuditError, "unknown fields"):
            self._load_input(payload)

        payload = self._load_payload()
        payload["as_of"] = "2025-01-03T09:35:00"
        with self.assertRaisesRegex(PaperAuditError, "timezone"):
            self._load_input(payload)

        audit_input = load_paper_audit_input(
            ROOT / "examples/paper/audit_input.json"
        )
        report = audit_paper_exchange(self._fixture_exchange(), audit_input)
        report["status"] = "pass"
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(PaperAuditError, "checksum"):
                save_paper_audit_report(
                    report, Path(temp_dir) / "tampered-report.json"
                )


if __name__ == "__main__":
    unittest.main()
