from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from lets_quant.cli import main
from lets_quant.execution import (
    PaperAlertError,
    PaperExchange,
    audit_paper_exchange,
    dispatch_local_alerts,
    load_alert_policy,
    load_alert_state,
    load_delivery_log,
    load_paper_audit_input,
    load_paper_audit_report,
    replay_event_file,
    save_alert_state,
    save_delivery_log,
    save_paper_audit_report,
    synchronize_paper_alerts,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2025-01-03T09:35:30+08:00")


class PaperAlertOperationsTest(unittest.TestCase):
    @staticmethod
    def _canonical_sha256(value: object) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _exchange(self) -> PaperExchange:
        exchange = PaperExchange(initial_cash=100_000)
        replay_event_file(
            exchange, ROOT / "examples/paper/audit_events.jsonl"
        )
        return exchange

    def _input_payload(self) -> dict:
        return json.loads(
            (ROOT / "examples/paper/audit_input.json").read_text(
                encoding="utf-8"
            )
        )

    def _audit_input(self, payload: dict):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "audit-input.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_paper_audit_input(path)

    def _warning_report(self) -> dict:
        audit_input = load_paper_audit_input(
            ROOT / "examples/paper/audit_input.json"
        )
        return audit_paper_exchange(self._exchange(), audit_input)

    def _blocked_report(self) -> dict:
        payload = self._input_payload()
        payload["risk_state"] = {
            "frozen": True,
            "reasons": ["drawdown gate"],
        }
        return audit_paper_exchange(
            self._exchange(), self._audit_input(payload)
        )

    def _pass_report(self) -> dict:
        payload = self._input_payload()
        payload["as_of"] = "2025-01-03T09:36:00+08:00"
        payload["external_account"]["source_kind"] = "broker"
        payload["external_account"]["source"] = "normalized-broker-export"
        return audit_paper_exchange(
            self._exchange(), self._audit_input(payload)
        )

    def _policy(self):
        return load_alert_policy(
            ROOT / "config/paper_alert_policy.example.json"
        )

    def test_sync_and_local_dispatch_are_deterministic_and_idempotent(
        self,
    ) -> None:
        report = self._warning_report()
        state = synchronize_paper_alerts(report, self._policy(), NOW)

        self.assertFalse(state["automatic_external_delivery_allowed"])
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(state["alerts"][0]["status"], "open")
        self.assertEqual(len(state["pending_notifications"]), 1)
        repeated = synchronize_paper_alerts(
            report, self._policy(), NOW, previous_state=state
        )
        self.assertEqual(repeated, state)

        delivered_at = NOW + timedelta(seconds=1)
        dispatched, receipts = dispatch_local_alerts(
            state, [], delivered_at
        )
        self.assertEqual(len(receipts), 1)
        self.assertEqual(dispatched["pending_notifications"], [])
        self.assertEqual(dispatched["alerts"][0]["delivery_count"], 1)

        recovered, repeated_receipts = dispatch_local_alerts(
            state, receipts, delivered_at
        )
        self.assertEqual(recovered, dispatched)
        self.assertEqual(repeated_receipts, receipts)
        with self.assertRaisesRegex(PaperAlertError, "missing receipts"):
            dispatch_local_alerts(
                dispatched, [], delivered_at + timedelta(seconds=1)
            )

        blocked_state = synchronize_paper_alerts(
            self._blocked_report(), self._policy(), NOW
        )
        _, blocked_receipts = dispatch_local_alerts(
            blocked_state, [], delivered_at
        )
        with self.assertRaisesRegex(PaperAlertError, "another alert state"):
            dispatch_local_alerts(state, blocked_receipts, delivered_at)

    def test_acknowledgement_is_idempotent_and_report_recovery_resolves(
        self,
    ) -> None:
        report = self._warning_report()
        state = synchronize_paper_alerts(report, self._policy(), NOW)
        alert_id = state["alerts"][0]["alert_id"]
        action = {
            "action_id": "ack-1",
            "alert_id": alert_id,
            "action": "acknowledge",
            "actor": "operator",
            "occurred_at": (NOW + timedelta(seconds=10)).isoformat(),
            "reason": "reviewed fixture limitation",
        }

        acknowledged = synchronize_paper_alerts(
            report,
            self._policy(),
            NOW + timedelta(seconds=10),
            previous_state=state,
            actions=[action],
        )
        self.assertEqual(acknowledged["alerts"][0]["status"], "acknowledged")
        self.assertEqual(acknowledged["pending_notifications"], [])

        repeated = synchronize_paper_alerts(
            report,
            self._policy(),
            NOW + timedelta(seconds=20),
            previous_state=acknowledged,
            actions=[action],
        )
        self.assertEqual(len(repeated["applied_actions"]), 1)

        conflicting = {**action, "reason": "different content"}
        with self.assertRaisesRegex(PaperAlertError, "reused"):
            synchronize_paper_alerts(
                report,
                self._policy(),
                NOW + timedelta(seconds=30),
                previous_state=acknowledged,
                actions=[conflicting],
            )

        resolved = synchronize_paper_alerts(
            self._pass_report(),
            self._policy(),
            NOW + timedelta(minutes=1),
            previous_state=repeated,
        )
        self.assertEqual(resolved["alerts"][0]["status"], "resolved")
        self.assertEqual(resolved["pending_notifications"], [])

    def test_silence_suppresses_delivery_until_the_exact_deadline(self) -> None:
        report = self._warning_report()
        state = synchronize_paper_alerts(report, self._policy(), NOW)
        alert_id = state["alerts"][0]["alert_id"]
        action_at = NOW + timedelta(seconds=1)
        silence_until = NOW + timedelta(hours=1)
        silenced = synchronize_paper_alerts(
            report,
            self._policy(),
            action_at,
            previous_state=state,
            actions=[
                {
                    "action_id": "silence-1",
                    "alert_id": alert_id,
                    "action": "silence",
                    "actor": "operator",
                    "occurred_at": action_at.isoformat(),
                    "reason": "planned investigation window",
                    "silence_until": silence_until.isoformat(),
                }
            ],
        )
        self.assertEqual(silenced["alerts"][0]["status"], "silenced")
        self.assertEqual(silenced["pending_notifications"], [])

        before_deadline = synchronize_paper_alerts(
            report,
            self._policy(),
            silence_until - timedelta(seconds=1),
            previous_state=silenced,
        )
        self.assertEqual(before_deadline["pending_notifications"], [])

        reopened = synchronize_paper_alerts(
            report,
            self._policy(),
            silence_until,
            previous_state=before_deadline,
        )
        self.assertEqual(reopened["alerts"][0]["status"], "open")
        self.assertEqual(len(reopened["pending_notifications"]), 1)

    def test_unacknowledged_critical_alert_escalates_after_threshold(self) -> None:
        report = self._blocked_report()
        state = synchronize_paper_alerts(report, self._policy(), NOW)
        delivered, receipts = dispatch_local_alerts(
            state, [], NOW + timedelta(seconds=1)
        )
        critical = next(
            item for item in delivered["alerts"] if item["severity"] == "critical"
        )
        warning = next(
            item for item in delivered["alerts"] if item["severity"] == "warning"
        )
        self.assertEqual(critical["last_delivery_level"], "standard")
        self.assertEqual(warning["last_delivery_level"], "standard")

        escalated = synchronize_paper_alerts(
            report,
            self._policy(),
            NOW + timedelta(minutes=30),
            previous_state=delivered,
        )
        self.assertEqual(len(escalated["pending_notifications"]), 1)
        self.assertEqual(
            escalated["pending_notifications"][0]["alert_id"],
            critical["alert_id"],
        )
        self.assertEqual(
            escalated["pending_notifications"][0]["level"], "escalated"
        )
        self.assertEqual(len(receipts), 2)

    def test_checksums_and_policy_boundary_reject_tampering(self) -> None:
        report = self._warning_report()
        state = synchronize_paper_alerts(report, self._policy(), NOW)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "report.json"
            state_path = root / "state.json"
            log_path = root / "deliveries.jsonl"
            save_paper_audit_report(report, report_path)
            save_alert_state(state, state_path)

            loaded_report = load_paper_audit_report(report_path)
            loaded_state = load_alert_state(state_path)
            self.assertEqual(loaded_report, report)
            self.assertEqual(loaded_state, state)

            tampered_report = copy.deepcopy(report)
            tampered_report["status"] = "pass"
            report_path.write_text(json.dumps(tampered_report), encoding="utf-8")
            with self.assertRaisesRegex(PaperAlertError, "checksum"):
                load_paper_audit_report(report_path)

            inconsistent_report = copy.deepcopy(report)
            inconsistent_report["status"] = "pass"
            inconsistent_report["manual_review_required"] = False
            report_payload = {
                key: value
                for key, value in inconsistent_report.items()
                if key != "report_sha256"
            }
            inconsistent_report["report_sha256"] = self._canonical_sha256(
                report_payload
            )
            report_path.write_text(
                json.dumps(inconsistent_report), encoding="utf-8"
            )
            with self.assertRaisesRegex(PaperAlertError, "status is inconsistent"):
                load_paper_audit_report(report_path)

            tampered_state = copy.deepcopy(state)
            tampered_state["updated_at"] = (
                NOW + timedelta(seconds=5)
            ).isoformat()
            state_path.write_text(json.dumps(tampered_state), encoding="utf-8")
            with self.assertRaisesRegex(PaperAlertError, "checksum"):
                load_alert_state(state_path)

            inconsistent_state = copy.deepcopy(state)
            inconsistent_state["alerts"][0]["status"] = "acknowledged"
            state_payload = {
                key: value
                for key, value in inconsistent_state.items()
                if key != "state_sha256"
            }
            inconsistent_state["state_sha256"] = self._canonical_sha256(
                state_payload
            )
            state_path.write_text(
                json.dumps(inconsistent_state), encoding="utf-8"
            )
            with self.assertRaisesRegex(PaperAlertError, "status evidence"):
                load_alert_state(state_path)

            dispatched, receipts = dispatch_local_alerts(
                state, [], NOW + timedelta(seconds=1)
            )
            save_delivery_log(receipts, log_path)
            self.assertEqual(load_delivery_log(log_path), receipts)
            self.assertEqual(dispatched["deliveries"], receipts)

    def test_cli_syncs_and_dispatches_local_receipts(self) -> None:
        report = self._warning_report()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            report_path = root / "report.json"
            state_path = root / "alerts.json"
            delivery_path = root / "deliveries.jsonl"
            save_paper_audit_report(report, report_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                sync_exit = main(
                    [
                        "sync-paper-alerts",
                        "--report",
                        str(report_path),
                        "--policy",
                        str(ROOT / "config/paper_alert_policy.example.json"),
                        "--now",
                        NOW.isoformat(),
                        "--state-out",
                        str(state_path),
                    ]
                )
            sync_output = json.loads(stdout.getvalue())
            self.assertEqual(sync_exit, 0)
            self.assertEqual(sync_output["pending_notification_count"], 1)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                dispatch_exit = main(
                    [
                        "dispatch-paper-alerts",
                        "--state",
                        str(state_path),
                        "--delivery-log",
                        str(delivery_path),
                        "--delivered-at",
                        (NOW + timedelta(seconds=1)).isoformat(),
                        "--state-out",
                        str(state_path),
                    ]
                )
            dispatch_output = json.loads(stdout.getvalue())
            self.assertEqual(dispatch_exit, 0)
            self.assertEqual(dispatch_output["dispatched_notification_count"], 1)
            self.assertEqual(len(load_delivery_log(delivery_path)), 1)
            self.assertEqual(load_alert_state(state_path)["pending_notifications"], [])


if __name__ == "__main__":
    unittest.main()
