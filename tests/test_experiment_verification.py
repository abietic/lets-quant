import contextlib
import csv
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from lets_quant.cli import main
from lets_quant.experiment_verification import (
    ExperimentArtifactError,
    verify_experiment_artifacts,
)
from lets_quant.experiment_replay import (
    ExperimentReplayError,
    load_embedded_market_snapshot,
    replay_experiment_artifacts,
)
from lets_quant.experiments import market_identity
from lets_quant.models import CorporateAction, MarketData
from lets_quant.snapshots import file_sha256


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class ExperimentArtifactVerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._base_temp = tempfile.TemporaryDirectory()
        output_root = Path(cls._base_temp.name) / "experiments"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "run-experiment",
                    "--policy",
                    str(ROOT / "config/policy.momentum.example.json"),
                    "--experiment",
                    str(ROOT / "config/experiment.m1_5.example.json"),
                    "--scenario",
                    "regime_shift",
                    "--scenario-start",
                    "2022-01-03",
                    "--scenario-trading-days",
                    "780",
                    "--output-root",
                    str(output_root),
                ]
            )
        if exit_code != 0:
            raise RuntimeError(stdout.getvalue())
        cls._base_run = Path(json.loads(stdout.getvalue())["artifact_directory"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._base_temp.cleanup()

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.run = Path(self._temp.name) / "experiment-run"
        shutil.copytree(self._base_run, self.run)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _manifest(self) -> dict:
        return json.loads(
            (self.run / "manifest.json").read_text(encoding="utf-8")
        )

    def _rewrite_manifest(self, manifest: dict) -> None:
        _write_json(self.run / "manifest.json", manifest)

    def _rehash(self, relative_path: str) -> None:
        manifest = self._manifest()
        manifest["file_sha256"][relative_path] = file_sha256(
            self.run / relative_path
        )
        self._rewrite_manifest(manifest)

    def test_valid_artifact_and_cli_report_boundaries(self) -> None:
        report = verify_experiment_artifacts(self.run)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["artifact_schema_version"], 2)
        self.assertFalse(report["legacy_schema_inferred"])
        self.assertTrue(report["replay_input_available"])
        self.assertTrue(report["replay_input_verified"])
        self.assertEqual(report["case_count"], 9)
        self.assertEqual(report["test_case_count"], 3)
        self.assertEqual(report["bootstrap_enabled_test_case_count"], 3)
        self.assertTrue(report["file_hashes_verified"])
        self.assertTrue(report["cross_file_consistency_verified"])
        self.assertFalse(report["replay_performed"])
        self.assertFalse(report["artifact_authenticity_verified"])
        self.assertFalse(report["investment_validity_established"])
        self.assertFalse(report["automatic_execution_allowed"])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                ["verify-experiment", "--experiment-run", str(self.run)]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "pass")

    def test_v015_manifest_without_schema_remains_verifiable(self) -> None:
        manifest = self._manifest()
        del manifest["artifact_schema_version"]
        del manifest["replay_input"]
        self._rewrite_manifest(manifest)

        report = verify_experiment_artifacts(self.run)

        self.assertEqual(report["artifact_schema_version"], 0)
        self.assertTrue(report["legacy_schema_inferred"])
        self.assertFalse(report["replay_input_verified"])

    def test_v017_schema_one_synthetic_artifact_remains_replayable(self) -> None:
        manifest = self._manifest()
        manifest["artifact_schema_version"] = 1
        del manifest["replay_input"]
        self._rewrite_manifest(manifest)

        verification = verify_experiment_artifacts(self.run)
        replay = replay_experiment_artifacts(self.run)

        self.assertFalse(verification["replay_input_verified"])
        self.assertEqual(replay["artifact_schema_version"], 1)
        self.assertFalse(replay["portable_replay_input_verified"])
        self.assertTrue(replay["replay_performed"])

    def test_schema_version_and_replay_descriptor_cannot_be_mixed(self) -> None:
        manifest = self._manifest()
        manifest["artifact_schema_version"] = 1
        self._rewrite_manifest(manifest)

        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "replay_input requires artifact schema version 2",
        ):
            verify_experiment_artifacts(self.run)

    def test_schema_two_replay_market_hash_drift_is_rejected(self) -> None:
        manifest = self._manifest()
        manifest["replay_input"]["market_sha256"] = "0" * 64
        self._rewrite_manifest(manifest)

        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "market_sha256 does not match the snapshot",
        ):
            verify_experiment_artifacts(self.run)

    def test_changed_file_fails_hash_verification(self) -> None:
        summary_path = self.run / "summary.json"
        summary_path.write_text(
            summary_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "integrity failed for summary.json",
        ):
            verify_experiment_artifacts(self.run)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                ["verify-experiment", "--experiment-run", str(self.run)]
            )
        self.assertEqual(exit_code, 2)
        self.assertIn("integrity failed for summary.json", stderr.getvalue())

    def test_missing_and_extra_files_fail_closed(self) -> None:
        case_directory = next((self.run / "cases").iterdir())
        missing_path = case_directory / "bootstrap_uncertainty.json"
        missing_path.unlink()
        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "missing declared files",
        ):
            verify_experiment_artifacts(self.run)

        shutil.copy2(
            self._base_run
            / "cases"
            / case_directory.name
            / "bootstrap_uncertainty.json",
            missing_path,
        )
        (self.run / "undeclared.txt").write_text("unexpected\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "undeclared files",
        ):
            verify_experiment_artifacts(self.run)

    def test_manifest_path_traversal_is_rejected(self) -> None:
        manifest = self._manifest()
        manifest["files"].append("../outside.json")
        manifest["files"].sort()
        manifest["file_sha256"]["../outside.json"] = "0" * 64
        self._rewrite_manifest(manifest)

        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "canonical relative POSIX path",
        ):
            verify_experiment_artifacts(self.run)

    def test_manifest_experiment_id_must_bind_input_hashes(self) -> None:
        manifest = self._manifest()
        manifest["experiment_id"] = "0" * 64
        self._rewrite_manifest(manifest)

        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "does not match its bound input hashes",
        ):
            verify_experiment_artifacts(self.run)

    def test_symbolic_link_is_rejected(self) -> None:
        policy_path = self.run / "policy.snapshot.json"
        policy_path.unlink()
        policy_path.symlink_to("experiment.snapshot.json")

        with self.assertRaisesRegex(ExperimentArtifactError, "symbolic link"):
            verify_experiment_artifacts(self.run)

    def test_rehashed_bootstrap_drift_fails_cross_file_consistency(self) -> None:
        case_directory = next((self.run / "cases").iterdir())
        bootstrap_path = case_directory / "bootstrap_uncertainty.json"
        bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
        bootstrap["seed_sha256"] = "0" * 64
        _write_json(bootstrap_path, bootstrap)
        relative_path = bootstrap_path.relative_to(self.run).as_posix()
        self._rehash(relative_path)

        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "bootstrap summaries do not match",
        ):
            verify_experiment_artifacts(self.run)

    def test_rehashed_summary_metric_drift_fails_consistency(self) -> None:
        summary_path = self.run / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["cases"][0]["total_return"] += 0.01
        _write_json(summary_path, summary)
        self._rehash("summary.json")

        with self.assertRaisesRegex(
            ExperimentArtifactError,
            "metric total_return does not match",
        ):
            verify_experiment_artifacts(self.run)

    def test_self_contained_experiment_replays_through_cli(self) -> None:
        report = replay_experiment_artifacts(self.run)

        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["replay_performed"])
        self.assertTrue(report["python_version_match"])
        self.assertTrue(report["experiment_input_id_match"])
        self.assertTrue(report["result_sha256_match"])
        self.assertTrue(report["summary_match"])
        self.assertEqual(report["replayed_case_count"], 9)
        self.assertFalse(report["artifact_authenticity_verified"])
        self.assertFalse(report["investment_validity_established"])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                ["replay-experiment", "--experiment-run", str(self.run)]
            )
        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["replay_performed"])

    def test_replay_requires_exact_recorded_python_version(self) -> None:
        manifest = self._manifest()
        manifest["python_version"] = "0.0.0"
        self._rewrite_manifest(manifest)

        with self.assertRaisesRegex(
            ExperimentReplayError,
            "requires the recorded Python version",
        ):
            replay_experiment_artifacts(self.run)

    def test_coordinated_result_hash_drift_is_caught_by_replay(self) -> None:
        manifest = self._manifest()
        changed_result_hash = "0" * 64
        manifest["result_sha256"] = changed_result_hash
        for case_directory in (self.run / "cases").iterdir():
            snapshot_path = case_directory / "case.snapshot.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["experiment_result_sha256"] = changed_result_hash
            _write_json(snapshot_path, snapshot)
            relative_path = snapshot_path.relative_to(self.run).as_posix()
            manifest["file_sha256"][relative_path] = file_sha256(snapshot_path)
        self._rewrite_manifest(manifest)
        self.assertEqual(verify_experiment_artifacts(self.run)["status"], "pass")

        with self.assertRaisesRegex(
            ExperimentReplayError,
            "replayed result_sha256 differs",
        ):
            replay_experiment_artifacts(self.run)

    def test_coordinated_market_drift_changes_replayed_input_id(self) -> None:
        snapshot_path = self.run / "market.snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        first_date = snapshot["market"]["dates"][0]
        first_symbol = sorted(snapshot["market"]["prices"][first_date])[0]
        snapshot["market"]["prices"][first_date][first_symbol] += 1.0
        _write_json(snapshot_path, snapshot)

        manifest = self._manifest()
        relative_path = snapshot_path.relative_to(self.run).as_posix()
        manifest["file_sha256"][relative_path] = file_sha256(snapshot_path)
        source_sha256 = hashlib.sha256(
            json.dumps(
                snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        manifest["market_source"]["sha256"] = source_sha256
        manifest["replay_input"]["file_sha256"] = file_sha256(snapshot_path)
        manifest["replay_input"]["market_sha256"] = hashlib.sha256(
            json.dumps(
                snapshot["market"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        manifest["replay_input"]["source_sha256"] = source_sha256
        manifest["experiment_id"] = hashlib.sha256(
            (
                manifest["experiment_input_id"]
                + manifest["source_tree_sha256"]
                + manifest["policy_sha256"]
                + manifest["experiment_sha256"]
                + source_sha256
            ).encode("utf-8")
        ).hexdigest()
        self._rewrite_manifest(manifest)
        self.assertEqual(verify_experiment_artifacts(self.run)["status"], "pass")

        with self.assertRaisesRegex(
            ExperimentReplayError,
            "replayed experiment_input_id differs",
        ):
            replay_experiment_artifacts(self.run)

    def test_legacy_artifact_without_embedded_market_remains_verify_only(
        self,
    ) -> None:
        snapshot_path = self.run / "market.snapshot.json"
        snapshot_path.unlink()
        manifest = self._manifest()
        manifest["artifact_schema_version"] = 1
        del manifest["replay_input"]
        manifest["files"].remove("market.snapshot.json")
        del manifest["file_sha256"]["market.snapshot.json"]
        self._rewrite_manifest(manifest)
        self.assertEqual(verify_experiment_artifacts(self.run)["status"], "pass")

        with self.assertRaisesRegex(
            ExperimentReplayError,
            "requires an embedded market.snapshot.json",
        ):
            replay_experiment_artifacts(self.run)

    def test_standalone_csv_is_frozen_and_portably_replayed(self) -> None:
        source_snapshot = json.loads(
            (self._base_run / "market.snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            prices_path = temporary / "prices.csv"
            with prices_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["date", "symbol", "close"]
                )
                writer.writeheader()
                for trading_date in source_snapshot["market"]["dates"]:
                    for symbol, close in sorted(
                        source_snapshot["market"]["prices"][
                            trading_date
                        ].items()
                    ):
                        writer.writerow(
                            {
                                "date": trading_date,
                                "symbol": symbol,
                                "close": close,
                            }
                        )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "run-experiment",
                        "--policy",
                        str(ROOT / "config/policy.momentum.example.json"),
                        "--experiment",
                        str(ROOT / "config/experiment.m1_5.example.json"),
                        "--prices",
                        str(prices_path),
                        "--output-root",
                        str(temporary / "experiments"),
                    ]
                )
            self.assertEqual(exit_code, 0, stdout.getvalue())
            experiment_directory = Path(
                json.loads(stdout.getvalue())["artifact_directory"]
            )
            manifest = json.loads(
                (experiment_directory / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            snapshot = json.loads(
                (experiment_directory / "market.snapshot.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(manifest["artifact_schema_version"], 2)
            self.assertEqual(
                manifest["replay_input"]["source_type"],
                "standalone_prices_csv",
            )
            self.assertEqual(
                snapshot["metadata"]["type"], "frozen_experiment_market"
            )
            self.assertFalse(
                snapshot["metadata"]["source_authenticity_verified"]
            )

            prices_path.unlink()
            report = replay_experiment_artifacts(experiment_directory)
            self.assertEqual(
                report["market_source_type"], "standalone_prices_csv"
            )
            self.assertTrue(report["portable_replay_input_verified"])
            self.assertTrue(report["replay_performed"])

            snapshot["metadata"]["source_authenticity_verified"] = True
            snapshot_path = experiment_directory / "market.snapshot.json"
            _write_json(snapshot_path, snapshot)
            manifest["file_sha256"]["market.snapshot.json"] = file_sha256(
                snapshot_path
            )
            manifest["replay_input"]["file_sha256"] = file_sha256(
                snapshot_path
            )
            _write_json(experiment_directory / "manifest.json", manifest)
            with self.assertRaisesRegex(
                ExperimentArtifactError,
                "cannot establish source authenticity",
            ):
                verify_experiment_artifacts(experiment_directory)

    def test_embedded_market_with_corporate_action_is_reversible(self) -> None:
        dates = [date(2025, 1, 2), date(2025, 1, 3)]
        action = CorporateAction(
            symbol="ASSET_A",
            event_type="cash_dividend",
            ex_date=dates[1],
            announced_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            cash_amount=0.2,
            available_at=datetime(2025, 1, 1, 8, tzinfo=timezone.utc),
        )
        market = MarketData(
            dates=dates,
            prices_by_date={
                dates[0]: {"ASSET_A": 10.0},
                dates[1]: {"ASSET_A": 9.8},
            },
            tradable_by_date={value: {"ASSET_A"} for value in dates},
            corporate_actions_by_date={dates[1]: [action]},
        )
        snapshot_path = Path(self._temp.name) / "embedded-market.json"
        _write_json(
            snapshot_path,
            {
                "metadata": {
                    "type": "deterministic_synthetic_market",
                    "investment_validity": False,
                },
                "market": market_identity(market),
            },
        )

        reconstructed = load_embedded_market_snapshot(snapshot_path)

        self.assertEqual(market_identity(reconstructed), market_identity(market))


if __name__ == "__main__":
    unittest.main()
