import json
import tempfile
import unittest
from pathlib import Path

from lets_quant.config import PolicyError, load_policy


ROOT = Path(__file__).resolve().parents[1]


class PolicyConfigTest(unittest.TestCase):
    def test_example_policy_is_valid(self) -> None:
        policy = load_policy(ROOT / "config/policy.example.json")

        self.assertEqual(policy.name, "synthetic-fixed-weight-demo")
        self.assertEqual(policy.execution.mode, "manual")
        self.assertAlmostEqual(
            sum(policy.strategy.target_weights.values()), 0.9
        )

    def test_live_execution_is_rejected(self) -> None:
        raw = json.loads(
            (ROOT / "config/policy.example.json").read_text(encoding="utf-8")
        )
        raw["execution"]["mode"] = "live"

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "live is unsupported"):
                load_policy(path)

    def test_unknown_keys_are_rejected(self) -> None:
        raw = json.loads(
            (ROOT / "config/policy.example.json").read_text(encoding="utf-8")
        )
        raw["execution"]["broker_token"] = "must-not-be-here"

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "unknown keys"):
                load_policy(path)

    def test_case_insensitive_duplicate_symbols_are_rejected(self) -> None:
        raw = json.loads(
            (ROOT / "config/policy.example.json").read_text(encoding="utf-8")
        )
        raw["strategy"]["target_weights"] = {"aaa": 0.4, "AAA": 0.4}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "unique"):
                load_policy(path)

    def test_zero_drawdown_limit_is_rejected(self) -> None:
        raw = json.loads(
            (ROOT / "config/policy.example.json").read_text(encoding="utf-8")
        )
        raw["risk"]["max_drawdown"] = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "must be > 0"):
                load_policy(path)

    def test_momentum_policy_is_valid_and_requires_lookback(self) -> None:
        policy = load_policy(ROOT / "config/policy.momentum.example.json")
        self.assertEqual(policy.strategy.kind, "momentum_filter")
        self.assertEqual(policy.strategy.lookback_trading_days, 60)

        raw = json.loads(
            (ROOT / "config/policy.momentum.example.json").read_text(
                encoding="utf-8"
            )
        )
        del raw["strategy"]["lookback_trading_days"]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "requires lookback"):
                load_policy(path)


if __name__ == "__main__":
    unittest.main()
