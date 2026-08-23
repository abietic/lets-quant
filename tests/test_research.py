import json
import tempfile
import unittest
from pathlib import Path

from lets_quant.research import ResearchPolicyError, load_research_policy


ROOT = Path(__file__).resolve().parents[1]


class ResearchPolicyTest(unittest.TestCase):
    def test_cn_etf_example_freezes_scope(self) -> None:
        policy = load_research_policy(
            ROOT / "config/research_policy.cn-etf.example.json"
        )

        self.assertEqual(policy.market, "CN")
        self.assertEqual(policy.adjustment, "hfq")
        self.assertEqual(
            policy.point_in_time_mode, "provider_publication"
        )
        self.assertEqual(policy.benchmark, "510300.XSHG")
        self.assertEqual(
            policy.tradable_symbols,
            {"510300.XSHG", "511010.XSHG"},
        )

    def test_benchmark_role_is_required(self) -> None:
        source = ROOT / "config/research_policy.cn-etf.example.json"
        raw = json.loads(source.read_text(encoding="utf-8"))
        raw["instruments"][0]["roles"] = ["tradable"]

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "research.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                ResearchPolicyError, "benchmark role"
            ):
                load_research_policy(path)


if __name__ == "__main__":
    unittest.main()
