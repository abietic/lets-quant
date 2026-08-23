import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lets_quant.cli import main
from lets_quant.experiment_comparison import (
    ExperimentComparisonError,
    compare_experiment_artifacts,
    verify_experiment_comparison_report,
    write_experiment_comparison_report,
)


ROOT = Path(__file__).resolve().parents[1]


class ExperimentComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls._root = Path(cls._temporary.name)
        cls.baseline = cls._run_experiment(
            scenario="regime_shift",
            experiment=ROOT / "config/experiment.m1_5.example.json",
        )
        cls.legacy_without_market = cls._root / "legacy-without-market"
        shutil.copytree(cls.baseline, cls.legacy_without_market)
        legacy_manifest_path = cls.legacy_without_market / "manifest.json"
        legacy_manifest = json.loads(
            legacy_manifest_path.read_text(encoding="utf-8")
        )
        legacy_manifest["artifact_schema_version"] = 1
        legacy_manifest.pop("replay_input")
        legacy_manifest["files"].remove("market.snapshot.json")
        legacy_manifest["file_sha256"].pop("market.snapshot.json")
        legacy_manifest_path.write_text(
            json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (cls.legacy_without_market / "market.snapshot.json").unlink()
        cls.candidate = cls._run_experiment(
            scenario="trend_up",
            experiment=ROOT / "config/experiment.m1_5.example.json",
        )

        partial_spec = json.loads(
            (ROOT / "config/experiment.m1_5.example.json").read_text(
                encoding="utf-8"
            )
        )
        partial_spec["windows"][0]["name"] = (
            "unaligned-" + partial_spec["windows"][0]["name"]
        )
        partial_path = cls._root / "experiment.partial.json"
        partial_path.write_text(
            json.dumps(partial_spec, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        cls.partial = cls._run_experiment(
            scenario="trend_up", experiment=partial_path
        )

        unaligned_spec = json.loads(json.dumps(partial_spec))
        for window in unaligned_spec["windows"]:
            if not window["name"].startswith("unaligned-"):
                window["name"] = "unaligned-" + window["name"]
        unaligned_path = cls._root / "experiment.unaligned.json"
        unaligned_path.write_text(
            json.dumps(unaligned_spec, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        cls.unaligned = cls._run_experiment(
            scenario="trend_up", experiment=unaligned_path
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    @classmethod
    def _run_experiment(cls, *, scenario: str, experiment: Path) -> Path:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "run-experiment",
                    "--policy",
                    str(ROOT / "config/policy.momentum.example.json"),
                    "--experiment",
                    str(experiment),
                    "--scenario",
                    scenario,
                    "--scenario-start",
                    "2022-01-03",
                    "--scenario-trading-days",
                    "780",
                    "--output-root",
                    str(cls._root / "experiments"),
                ]
            )
        if exit_code != 0:
            raise RuntimeError(stdout.getvalue())
        return Path(json.loads(stdout.getvalue())["artifact_directory"])

    def test_identical_comparison_is_deterministic_and_not_a_ranking(
        self,
    ) -> None:
        first = compare_experiment_artifacts(self.baseline, self.baseline)
        second = compare_experiment_artifacts(self.baseline, self.baseline)

        self.assertEqual(first, second)
        self.assertEqual(first["comparison_status"], "identical")
        self.assertEqual(first["summary"]["aligned_case_count"], 9)
        self.assertEqual(first["summary"]["changed_aligned_case_count"], 0)
        self.assertTrue(first["summary"]["experiment_input_id_equal"])
        self.assertTrue(first["summary"]["result_sha256_equal"])
        self.assertFalse(first["ranking_performed"])
        self.assertIsNone(first["preferred_experiment"])
        self.assertFalse(first["investment_validity_established"])
        verify_experiment_comparison_report(first)

        tampered = json.loads(json.dumps(first))
        tampered["summary"]["aligned_case_count"] = 0
        with self.assertRaisesRegex(
            ExperimentComparisonError, "report hash mismatch"
        ):
            verify_experiment_comparison_report(tampered)

    def test_missing_legacy_market_identity_is_unknown_not_equal(self) -> None:
        report = compare_experiment_artifacts(
            self.legacy_without_market, self.legacy_without_market
        )

        identity = report["input_comparison"]["market"]["identity_sha256"]
        self.assertIsNone(identity["baseline"])
        self.assertIsNone(identity["candidate"])
        self.assertFalse(identity["baseline_available"])
        self.assertFalse(identity["candidate_available"])
        self.assertIsNone(identity["equal"])

    def test_aligned_market_change_has_directional_case_deltas(self) -> None:
        report = compare_experiment_artifacts(
            self.baseline, self.candidate
        )
        reverse = compare_experiment_artifacts(
            self.candidate, self.baseline
        )

        self.assertEqual(
            report["comparison_status"], "aligned_with_differences"
        )
        self.assertTrue(report["summary"]["case_alignment_complete"])
        self.assertEqual(report["summary"]["aligned_case_count"], 9)
        self.assertEqual(report["summary"]["changed_aligned_case_count"], 9)
        self.assertTrue(report["input_comparison"]["policy"]["equal"])
        self.assertTrue(report["input_comparison"]["experiment"]["equal"])
        self.assertFalse(
            report["input_comparison"]["market"]["identity_sha256"]["equal"]
        )
        self.assertEqual(
            report["case_comparison"]["metric_delta_definition"],
            "candidate_minus_baseline",
        )
        forward_delta = report["case_comparison"]["aligned"][0]["metrics"][
            "total_return"
        ]["delta"]
        reverse_delta = reverse["case_comparison"]["aligned"][0]["metrics"][
            "total_return"
        ]["delta"]
        self.assertAlmostEqual(forward_delta, -reverse_delta, places=15)
        self.assertNotEqual(report["report_sha256"], reverse["report_sha256"])

    def test_changed_window_names_are_not_forced_into_alignment(self) -> None:
        report = compare_experiment_artifacts(
            self.baseline, self.unaligned
        )

        self.assertEqual(report["comparison_status"], "not_aligned")
        self.assertEqual(report["summary"]["aligned_case_count"], 0)
        self.assertEqual(report["summary"]["baseline_only_case_count"], 9)
        self.assertEqual(report["summary"]["candidate_only_case_count"], 9)
        self.assertFalse(report["summary"]["case_alignment_complete"])
        self.assertGreater(
            report["input_comparison"]["experiment"]["difference_count"], 0
        )

    def test_one_changed_window_is_reported_as_partial_alignment(self) -> None:
        report = compare_experiment_artifacts(self.baseline, self.partial)

        self.assertEqual(report["comparison_status"], "partially_aligned")
        self.assertEqual(report["summary"]["aligned_case_count"], 6)
        self.assertEqual(report["summary"]["baseline_only_case_count"], 3)
        self.assertEqual(report["summary"]["candidate_only_case_count"], 3)
        self.assertFalse(report["summary"]["case_alignment_complete"])

    def test_cli_writes_exclusive_report_outside_experiment_directories(
        self,
    ) -> None:
        report_path = self._root / "reports/comparison.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "compare-experiments",
                    "--baseline-run",
                    str(self.baseline),
                    "--candidate-run",
                    str(self.candidate),
                    "--report-out",
                    str(report_path),
                ]
            )
        payload = json.loads(stdout.getvalue())
        report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assertEqual(payload["comparison_status"], "aligned_with_differences")
        self.assertEqual(payload["report_sha256"], report["report_sha256"])
        verify_experiment_comparison_report(report)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            second_exit = main(
                [
                    "compare-experiments",
                    "--baseline-run",
                    str(self.baseline),
                    "--candidate-run",
                    str(self.candidate),
                    "--report-out",
                    str(report_path),
                ]
            )
        self.assertEqual(second_exit, 2)
        self.assertIn("already exists", stderr.getvalue())

        with self.assertRaisesRegex(
            ExperimentComparisonError,
            "must not be written inside an experiment directory",
        ):
            write_experiment_comparison_report(
                report, self.baseline / "comparison.json"
            )


if __name__ == "__main__":
    unittest.main()
