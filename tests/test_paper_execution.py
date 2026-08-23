import hashlib
import json
import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lets_quant.execution import (
    PaperExchange,
    PaperExecutionError,
    replay_event_file,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2025, 1, 2, tzinfo=timezone.utc)


class PaperExecutionTest(unittest.TestCase):
    def _acknowledged_buy(self) -> PaperExchange:
        exchange = PaperExchange(initial_cash=10_000)
        exchange.submit(
            event_id="submit-1",
            client_order_id="order-1",
            symbol="AAA",
            side="BUY",
            quantity=100,
            occurred_at=NOW,
        )
        exchange.acknowledge(
            event_id="ack-1",
            client_order_id="order-1",
            venue_order_id="venue-1",
            occurred_at=NOW,
        )
        return exchange

    def test_partial_fills_and_duplicates_are_idempotent(self) -> None:
        exchange = self._acknowledged_buy()
        exchange.fill(
            event_id="fill-event-1",
            client_order_id="order-1",
            fill_id="fill-1",
            quantity=40,
            price=10,
            commission=1,
            occurred_at=NOW,
        )
        exchange.fill(
            event_id="fill-event-1",
            client_order_id="order-1",
            fill_id="fill-1",
            quantity=40,
            price=10,
            commission=1,
            occurred_at=NOW,
        )
        exchange.fill(
            event_id="fill-event-duplicate",
            client_order_id="order-1",
            fill_id="fill-1",
            quantity=40,
            price=10,
            commission=1,
            occurred_at=NOW,
        )
        order = exchange.fill(
            event_id="fill-event-2",
            client_order_id="order-1",
            fill_id="fill-2",
            quantity=60,
            price=11,
            commission=1,
            occurred_at=NOW,
        )

        self.assertEqual(order.status, "FILLED")
        self.assertEqual(order.filled_quantity, 100)
        self.assertAlmostEqual(order.average_fill_price, 10.6)
        self.assertEqual(exchange.positions["AAA"], 100)
        self.assertEqual(exchange.cash, 8_938.0)
        self.assertEqual(exchange.reconciliation()["status"], "pass")
        self.assertEqual(len(order.fills), 2)

    def test_invalid_transitions_and_id_reuse_fail_closed(self) -> None:
        exchange = PaperExchange(initial_cash=100)
        exchange.submit(
            event_id="event-1",
            client_order_id="order-1",
            symbol="AAA",
            side="BUY",
            quantity=10,
            occurred_at=NOW,
        )
        with self.assertRaisesRegex(PaperExecutionError, "cannot fill"):
            exchange.fill(
                event_id="event-2",
                client_order_id="order-1",
                fill_id="fill-1",
                quantity=1,
                price=10,
                occurred_at=NOW,
            )
        with self.assertRaisesRegex(PaperExecutionError, "different content"):
            exchange.submit(
                event_id="event-1",
                client_order_id="order-1",
                symbol="AAA",
                side="BUY",
                quantity=11,
                occurred_at=NOW,
            )

    def test_account_and_order_limits_are_enforced(self) -> None:
        exchange = self._acknowledged_buy()
        with self.assertRaisesRegex(PaperExecutionError, "available cash"):
            exchange.fill(
                event_id="fill-too-large",
                client_order_id="order-1",
                fill_id="fill-too-large",
                quantity=100,
                price=101,
                occurred_at=NOW,
            )
        with self.assertRaisesRegex(PaperExecutionError, "remaining"):
            exchange.fill(
                event_id="overfill",
                client_order_id="order-1",
                fill_id="overfill",
                quantity=101,
                price=1,
                occurred_at=NOW,
            )

        rounding_exchange = PaperExchange(initial_cash=0.3)
        rounding_exchange.submit(
            event_id="rounding-submit",
            client_order_id="rounding-order",
            symbol="AAA",
            side="BUY",
            quantity=3,
            occurred_at=NOW,
        )
        rounding_exchange.acknowledge(
            event_id="rounding-ack",
            client_order_id="rounding-order",
            venue_order_id="rounding-venue",
            occurred_at=NOW,
        )
        rounding_exchange.fill(
            event_id="rounding-fill",
            client_order_id="rounding-order",
            fill_id="rounding-fill",
            quantity=3,
            price=0.1,
            occurred_at=NOW,
        )
        self.assertEqual(rounding_exchange.cash, 0.0)
        self.assertEqual(rounding_exchange.reconciliation()["status"], "pass")

    def test_snapshot_restart_preserves_idempotency_and_detects_tampering(
        self,
    ) -> None:
        exchange = self._acknowledged_buy()
        exchange.fill(
            event_id="fill-event-1",
            client_order_id="order-1",
            fill_id="fill-1",
            quantity=40,
            price=10,
            occurred_at=NOW,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "paper-state.json"
            exchange.save(state_path)
            resumed = PaperExchange.load(state_path)
            resumed.fill(
                event_id="fill-event-1",
                client_order_id="order-1",
                fill_id="fill-1",
                quantity=40,
                price=10,
                occurred_at=NOW,
            )
            self.assertEqual(resumed.orders["order-1"].filled_quantity, 40)
            self.assertEqual(resumed.reconciliation()["status"], "pass")

            tampered = json.loads(state_path.read_text(encoding="utf-8"))
            tampered["cash"] += 1
            state_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(PaperExecutionError, "checksum"):
                PaperExchange.load(state_path)

            exchange.save(state_path)
            tampered = json.loads(state_path.read_text(encoding="utf-8"))
            tampered["orders"][0]["symbol"] = "BBB"
            tampered["positions"] = {"BBB": 40}
            tampered["reconciliation"]["positions"] = {"BBB": 40}
            tampered["reconciliation"]["expected_positions"] = {"BBB": 40}
            payload = {
                key: value
                for key, value in tampered.items()
                if key != "state_sha256"
            }
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            tampered["state_sha256"] = hashlib.sha256(encoded).hexdigest()
            state_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(
                PaperExecutionError, "replayed event history"
            ):
                PaperExchange.load(state_path)

    def test_terminal_transitions_and_global_fill_ids_fail_closed(self) -> None:
        exchange = self._acknowledged_buy()
        exchange.fill(
            event_id="fill-event-1",
            client_order_id="order-1",
            fill_id="global-fill-1",
            quantity=40,
            price=10,
            occurred_at=NOW,
        )
        canceled = exchange.cancel(
            event_id="cancel-1",
            client_order_id="order-1",
            occurred_at=NOW,
        )
        self.assertEqual(canceled.status, "CANCELED")
        with self.assertRaisesRegex(PaperExecutionError, "cannot fill"):
            exchange.fill(
                event_id="late-fill",
                client_order_id="order-1",
                fill_id="late-fill",
                quantity=1,
                price=10,
                occurred_at=NOW,
            )

        exchange.submit(
            event_id="submit-2",
            client_order_id="order-2",
            symbol="BBB",
            side="BUY",
            quantity=1,
            occurred_at=NOW,
        )
        exchange.acknowledge(
            event_id="ack-2",
            client_order_id="order-2",
            venue_order_id="venue-2",
            occurred_at=NOW,
        )
        exchange.submit(
            event_id="submit-3",
            client_order_id="order-3",
            symbol="CCC",
            side="BUY",
            quantity=1,
            occurred_at=NOW,
        )
        with self.assertRaisesRegex(PaperExecutionError, "another order"):
            exchange.acknowledge(
                event_id="ack-3",
                client_order_id="order-3",
                venue_order_id="venue-2",
                occurred_at=NOW,
            )
        with self.assertRaisesRegex(PaperExecutionError, "another order"):
            exchange.fill(
                event_id="fill-event-2",
                client_order_id="order-2",
                fill_id="global-fill-1",
                quantity=1,
                price=10,
                occurred_at=NOW,
            )
        rejected = exchange.reject(
            event_id="reject-2",
            client_order_id="order-2",
            reason="venue rejected order",
            occurred_at=NOW,
        )
        self.assertEqual(rejected.status, "REJECTED")
        with self.assertRaisesRegex(PaperExecutionError, "cannot cancel"):
            exchange.cancel(
                event_id="cancel-2",
                client_order_id="order-2",
                occurred_at=NOW,
            )

    def test_seeded_event_sequences_survive_restart(self) -> None:
        random_source = random.Random(20250820)
        exchange = PaperExchange(
            initial_cash=1_000_000,
            initial_positions={"AAA": 500},
        )
        for index in range(30):
            side = "BUY" if index % 2 == 0 else "SELL"
            quantity = random_source.randint(2, 20)
            price = float(random_source.randint(50, 150))
            order_id = f"random-order-{index}"
            exchange.submit(
                event_id=f"random-submit-{index}",
                client_order_id=order_id,
                symbol="AAA",
                side=side,
                quantity=quantity,
                occurred_at=NOW,
            )
            exchange.acknowledge(
                event_id=f"random-ack-{index}",
                client_order_id=order_id,
                venue_order_id=f"random-venue-{index}",
                occurred_at=NOW,
            )
            first_quantity = quantity // 2
            exchange.fill(
                event_id=f"random-fill-{index}-1",
                client_order_id=order_id,
                fill_id=f"random-fill-id-{index}-1",
                quantity=first_quantity,
                price=price,
                commission=0.1,
                occurred_at=NOW,
            )
            exchange.fill(
                event_id=f"random-fill-{index}-2",
                client_order_id=order_id,
                fill_id=f"random-fill-id-{index}-2",
                quantity=quantity - first_quantity,
                price=price + 0.5,
                commission=0.1,
                tax=0.1 if side == "SELL" else 0.0,
                occurred_at=NOW,
            )
            self.assertEqual(exchange.reconciliation()["status"], "pass")

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "random-state.json"
            exchange.save(state_path)
            resumed = PaperExchange.load(state_path)

        self.assertEqual(resumed.to_snapshot(), exchange.to_snapshot())

    def test_event_file_replay_round_trips(self) -> None:
        exchange = PaperExchange(initial_cash=100_000)
        processed = replay_event_file(
            exchange, ROOT / "examples/paper/events.jsonl"
        )

        self.assertEqual(processed, 9)
        self.assertEqual(exchange.orders["paper-buy-001"].status, "FILLED")
        self.assertEqual(exchange.orders["paper-buy-002"].status, "REJECTED")
        self.assertEqual(exchange.positions["ASSET_A"], 80)
        self.assertEqual(exchange.reconciliation()["status"], "pass")
        self.assertFalse(exchange.to_snapshot()["automatic_execution_allowed"])


if __name__ == "__main__":
    unittest.main()
