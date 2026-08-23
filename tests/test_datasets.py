import json
import tempfile
import unittest
from pathlib import Path

from lets_quant.data import DataError
from lets_quant.datasets import (
    build_curated_dataset,
    load_curated_dataset,
    parse_timestamp,
    validate_manual_planning_source,
    validate_strategy_scope,
)
from lets_quant.snapshots import file_sha256, snapshot_file

from tests.helpers import make_policy


ROOT = Path(__file__).resolve().parents[1]


class CuratedDatasetTest(unittest.TestCase):
    def _build(self, temporary: Path, as_of: str):
        snapshot = snapshot_file(
            input_path=ROOT / "examples/m1/bars.csv",
            provider="local_csv",
            provider_version="1",
            dataset="etf_daily_bars",
            request={"fixture": "test"},
            license_manifest_path=(
                ROOT / "config/data_providers.example.json"
            ),
            output_root=temporary / "raw",
        )
        return build_curated_dataset(
            snapshot_path=snapshot.directory,
            research_policy_path=(
                ROOT / "config/research_policy.cn-etf.example.json"
            ),
            calendar_path=ROOT / "examples/m1/calendar.csv",
            instruments_path=ROOT / "examples/m1/instruments.csv",
            suspensions_path=ROOT / "examples/m1/suspensions.csv",
            corporate_actions_path=(
                ROOT / "examples/m1/corporate_actions.csv"
            ),
            as_of=parse_timestamp(as_of),
            output_root=temporary / "curated",
        )

    def test_as_of_excludes_future_rows_and_preserves_market_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._build(
                Path(temp_dir), "2025-01-08T23:59:59+08:00"
            )
            dataset = load_curated_dataset(result.directory)

            self.assertEqual(result.status, "pass")
            self.assertEqual(len(dataset.market.dates), 5)
            self.assertNotIn(
                "2025-01-09",
                (result.directory / "prices.csv").read_text(encoding="utf-8"),
            )
            self.assertFalse(
                dataset.market.is_tradable(
                    dataset.market.dates[2], "511010.XSHG"
                )
            )
            point_in_time = next(
                check
                for check in result.quality_report["checks"]
                if check["name"] == "point_in_time_filter"
            )
            self.assertEqual(point_in_time["count"], 2)

    def test_earlier_as_of_builds_a_distinct_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            early = self._build(
                temporary, "2025-01-03T23:59:59+08:00"
            )
            later = self._build(
                temporary, "2025-01-08T23:59:59+08:00"
            )

            self.assertNotEqual(early.dataset_id, later.dataset_id)
            self.assertEqual(
                len(load_curated_dataset(early.directory).market.dates), 2
            )

    def test_strategy_must_match_frozen_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._build(
                Path(temp_dir), "2025-01-08T23:59:59+08:00"
            )
            dataset = load_curated_dataset(result.directory)
            policy = make_policy(weights={"OUTSIDE": 0.5})

            with self.assertRaisesRegex(ValueError, "outside"):
                validate_strategy_scope(policy, dataset.manifest)

    def test_adjusted_dataset_cannot_drive_manual_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._build(
                Path(temp_dir), "2025-01-08T23:59:59+08:00"
            )
            dataset = load_curated_dataset(result.directory)

            with self.assertRaisesRegex(DataError, "unadjusted"):
                validate_manual_planning_source(dataset.manifest)

    def test_unadjusted_dataset_uses_explicit_corporate_action_ledger(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            bars = temporary / "bars.csv"
            bars.write_text(
                (ROOT / "examples/m1/bars.csv")
                .read_text(encoding="utf-8")
                .replace(",hfq", ",none"),
                encoding="utf-8",
            )
            raw_policy = json.loads(
                (
                    ROOT / "config/research_policy.cn-etf.example.json"
                ).read_text(encoding="utf-8")
            )
            raw_policy["adjustment"] = "none"
            research_policy = temporary / "research.json"
            research_policy.write_text(
                json.dumps(raw_policy), encoding="utf-8"
            )
            snapshot = snapshot_file(
                input_path=bars,
                provider="local_csv",
                provider_version="1",
                dataset="etf_daily_bars",
                request={"fixture": "unadjusted"},
                license_manifest_path=(
                    ROOT / "config/data_providers.example.json"
                ),
                output_root=temporary / "raw",
            )
            result = build_curated_dataset(
                snapshot_path=snapshot.directory,
                research_policy_path=research_policy,
                calendar_path=ROOT / "examples/m1/calendar.csv",
                instruments_path=ROOT / "examples/m1/instruments.csv",
                suspensions_path=ROOT / "examples/m1/suspensions.csv",
                corporate_actions_path=(
                    ROOT / "examples/m1/corporate_actions.csv"
                ),
                as_of=parse_timestamp("2025-01-08T23:59:59+08:00"),
                output_root=temporary / "curated",
            )

            dataset = load_curated_dataset(result.directory)
            self.assertEqual(result.status, "pass")
            self.assertEqual(dataset.market.price_adjustment, "none")
            self.assertTrue(dataset.market.corporate_actions_by_date)
            self.assertEqual(
                dataset.manifest["price_semantics"][
                    "corporate_action_handling"
                ],
                "explicit_ledger",
            )
            validate_manual_planning_source(dataset.manifest)

    def test_failed_quality_dataset_cannot_be_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            bars = temporary / "bars.csv"
            bars.write_text(
                (ROOT / "examples/m1/bars.csv")
                .read_text(encoding="utf-8")
                .replace(",hfq", ",qfq"),
                encoding="utf-8",
            )
            snapshot = snapshot_file(
                input_path=bars,
                provider="local_csv",
                provider_version="1",
                dataset="etf_daily_bars",
                request={"fixture": "wrong-adjustment"},
                license_manifest_path=(
                    ROOT / "config/data_providers.example.json"
                ),
                output_root=temporary / "raw",
            )
            result = build_curated_dataset(
                snapshot_path=snapshot.directory,
                research_policy_path=(
                    ROOT / "config/research_policy.cn-etf.example.json"
                ),
                calendar_path=ROOT / "examples/m1/calendar.csv",
                instruments_path=ROOT / "examples/m1/instruments.csv",
                suspensions_path=ROOT / "examples/m1/suspensions.csv",
                corporate_actions_path=(
                    ROOT / "examples/m1/corporate_actions.csv"
                ),
                as_of=parse_timestamp("2025-01-08T23:59:59+08:00"),
                output_root=temporary / "curated",
            )

            self.assertEqual(result.status, "fail")
            with self.assertRaisesRegex(DataError, "quality"):
                load_curated_dataset(result.directory)

    def test_local_observation_mode_rejects_retrospective_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            raw_policy = json.loads(
                (
                    ROOT / "config/research_policy.cn-etf.example.json"
                ).read_text(encoding="utf-8")
            )
            raw_policy["point_in_time_mode"] = "local_observation"
            research_policy = temporary / "research.json"
            research_policy.write_text(
                json.dumps(raw_policy), encoding="utf-8"
            )
            snapshot = snapshot_file(
                input_path=ROOT / "examples/m1/bars.csv",
                provider="local_csv",
                provider_version="1",
                dataset="etf_daily_bars",
                request={"fixture": "retrospective"},
                license_manifest_path=(
                    ROOT / "config/data_providers.example.json"
                ),
                output_root=temporary / "raw",
            )

            result = build_curated_dataset(
                snapshot_path=snapshot.directory,
                research_policy_path=research_policy,
                calendar_path=ROOT / "examples/m1/calendar.csv",
                instruments_path=ROOT / "examples/m1/instruments.csv",
                suspensions_path=ROOT / "examples/m1/suspensions.csv",
                corporate_actions_path=(
                    ROOT / "examples/m1/corporate_actions.csv"
                ),
                as_of=parse_timestamp("2025-01-08T23:59:59+08:00"),
                output_root=temporary / "curated",
            )

            self.assertEqual(result.status, "fail")
            observation_check = next(
                check
                for check in result.quality_report["checks"]
                if check["name"] == "snapshot_observation_basis"
            )
            self.assertEqual(observation_check["status"], "fail")

    def test_curated_output_cannot_be_rehashed_under_the_same_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._build(
                Path(temp_dir), "2025-01-08T23:59:59+08:00"
            )
            prices = result.directory / "prices.csv"
            prices.write_text(
                prices.read_text(encoding="utf-8").replace(
                    "3.9400000000", "9.9900000000", 1
                ),
                encoding="utf-8",
            )
            manifest_path = result.directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["prices.csv"] = file_sha256(prices)
            manifest_path.write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with self.assertRaisesRegex(DataError, "protected file"):
                load_curated_dataset(result.directory)


if __name__ == "__main__":
    unittest.main()
