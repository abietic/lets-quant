import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lets_quant.cli import main
from lets_quant.experiment_verification import (
    ExperimentArtifactError,
    verify_experiment_artifacts,
)
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
        self.assertEqual(report["artifact_schema_version"], 1)
        self.assertFalse(report["legacy_schema_inferred"])
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
        self._rewrite_manifest(manifest)

        report = verify_experiment_artifacts(self.run)

        self.assertEqual(report["artifact_schema_version"], 0)
        self.assertTrue(report["legacy_schema_inferred"])

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


if __name__ == "__main__":
    unittest.main()
