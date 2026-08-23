import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lets_quant.cli import main
from lets_quant.experiment_catalog import (
    ExperimentCatalogError,
    build_experiment_catalog,
    verify_experiment_catalog_report,
    write_experiment_catalog,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExperimentCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.experiments = cls.root / "experiments"
        cls.experiments.mkdir()
        cls.baseline = cls._run_experiment("regime_shift")
        cls.candidate = cls._run_experiment("trend_up")

        cls.repeated_baseline = cls.experiments / "repeated-baseline"
        shutil.copytree(cls.baseline, cls.repeated_baseline)
        cls.repeated_candidate = cls.experiments / "repeated-candidate"
        shutil.copytree(cls.candidate, cls.repeated_candidate)

        cls.legacy = cls.experiments / "legacy-nonportable"
        shutil.copytree(cls.baseline, cls.legacy)
        legacy_manifest_path = cls.legacy / "manifest.json"
        legacy_manifest = json.loads(
            legacy_manifest_path.read_text(encoding="utf-8")
        )
        legacy_manifest["artifact_schema_version"] = 1
        legacy_manifest.pop("replay_input")
        legacy_manifest["files"].remove("market.snapshot.json")
        legacy_manifest["file_sha256"].pop("market.snapshot.json")
        _write_json(legacy_manifest_path, legacy_manifest)
        (cls.legacy / "market.snapshot.json").unlink()

        cls.divergent = cls.experiments / "divergent-result"
        shutil.copytree(cls.baseline, cls.divergent)
        divergent_manifest_path = cls.divergent / "manifest.json"
        divergent_manifest = json.loads(
            divergent_manifest_path.read_text(encoding="utf-8")
        )
        divergent_result = "f" * 64
        divergent_manifest["result_sha256"] = divergent_result
        for snapshot_path in sorted(
            (cls.divergent / "cases").glob("*/case.snapshot.json")
        ):
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["experiment_result_sha256"] = divergent_result
            _write_json(snapshot_path, snapshot)
            relative = snapshot_path.relative_to(cls.divergent).as_posix()
            divergent_manifest["file_sha256"][relative] = _file_sha256(
                snapshot_path
            )
        _write_json(divergent_manifest_path, divergent_manifest)

        cls.invalid = cls.experiments / "invalid-tampered"
        shutil.copytree(cls.baseline, cls.invalid)
        invalid_summary_path = cls.invalid / "summary.json"
        invalid_summary = json.loads(
            invalid_summary_path.read_text(encoding="utf-8")
        )
        invalid_summary["case_count"] = 0
        _write_json(invalid_summary_path, invalid_summary)

        cls.unverifiable_legacy = cls.experiments / "legacy-unverifiable"
        shutil.copytree(cls.baseline, cls.unverifiable_legacy)
        unverifiable_manifest_path = cls.unverifiable_legacy / "manifest.json"
        unverifiable_manifest = json.loads(
            unverifiable_manifest_path.read_text(encoding="utf-8")
        )
        unverifiable_manifest.pop("artifact_schema_version")
        unverifiable_manifest.pop("file_sha256")
        _write_json(unverifiable_manifest_path, unverifiable_manifest)

        cls.symlink = cls.experiments / "symlink-artifact"
        cls.symlink.symlink_to(cls.baseline, target_is_directory=True)

        cls.valid_root = cls.root / "valid-experiments"
        cls.valid_root.mkdir()
        shutil.copytree(cls.candidate, cls.valid_root / "candidate-a")
        shutil.copytree(cls.candidate, cls.valid_root / "candidate-b")
        cls.empty_root = cls.root / "empty-experiments"
        cls.empty_root.mkdir()
        cls.manifest_symlink_root = cls.root / "manifest-symlink-experiments"
        cls.manifest_symlink_root.mkdir()
        linked_artifact = cls.manifest_symlink_root / "linked-manifest"
        shutil.copytree(cls.baseline, linked_artifact)
        (linked_artifact / "manifest.json").unlink()
        (linked_artifact / "manifest.json").symlink_to(
            cls.baseline / "manifest.json"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    @classmethod
    def _run_experiment(cls, scenario: str) -> Path:
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
                    scenario,
                    "--scenario-start",
                    "2022-01-03",
                    "--scenario-trading-days",
                    "780",
                    "--output-root",
                    str(cls.experiments),
                ]
            )
        if exit_code != 0:
            raise RuntimeError(stdout.getvalue())
        return Path(json.loads(stdout.getvalue())["artifact_directory"])

    def test_catalog_is_deterministic_and_surfaces_cross_run_issues(self) -> None:
        first = build_experiment_catalog(self.experiments)
        second = build_experiment_catalog(self.experiments)

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "attention_required")
        self.assertEqual(first["summary"]["candidate_directory_count"], 9)
        self.assertEqual(first["summary"]["verified_artifact_count"], 6)
        self.assertEqual(first["summary"]["invalid_artifact_count"], 3)
        self.assertEqual(
            first["summary"]["unverifiable_legacy_artifact_count"], 1
        )
        self.assertEqual(first["summary"]["artifact_schema_counts"], {"1": 1, "2": 5})
        self.assertEqual(first["summary"]["portable_replay_artifact_count"], 5)
        self.assertEqual(first["summary"]["experiment_identity_count"], 2)
        self.assertEqual(first["summary"]["repeated_experiment_group_count"], 2)
        self.assertEqual(first["summary"]["consistent_repeated_group_count"], 1)
        self.assertEqual(first["summary"]["redundant_verified_artifact_count"], 1)
        self.assertEqual(first["summary"]["inconsistent_result_group_count"], 1)
        self.assertEqual(first["summary"]["blocking_issue_count"], 4)
        self.assertFalse(first["ranking_performed"])
        self.assertIsNone(first["preferred_experiment"])
        self.assertFalse(first["automatic_cleanup_performed"])
        verify_experiment_catalog_report(first)

        codes = [item["code"] for item in first["review_items"]]
        self.assertEqual(codes.count("artifact_verification_failed"), 2)
        self.assertEqual(codes.count("legacy_artifact_unverifiable"), 1)
        self.assertIn("inconsistent_results_for_experiment_id", codes)
        self.assertIn("repeated_verified_experiment", codes)
        self.assertIn("verified_but_nonportable_replay_input", codes)

    def test_verified_repeated_runs_do_not_fail_the_catalog(self) -> None:
        catalog = build_experiment_catalog(self.valid_root)

        self.assertEqual(catalog["status"], "pass")
        self.assertEqual(catalog["summary"]["verified_artifact_count"], 2)
        self.assertEqual(catalog["summary"]["blocking_issue_count"], 0)
        self.assertEqual(catalog["summary"]["redundant_verified_artifact_count"], 1)
        self.assertEqual(
            [item["code"] for item in catalog["review_items"]],
            ["repeated_verified_experiment"],
        )

    def test_empty_root_is_a_successful_empty_catalog(self) -> None:
        catalog = build_experiment_catalog(self.empty_root)

        self.assertEqual(catalog["status"], "empty")
        self.assertEqual(catalog["entries"], [])
        self.assertEqual(catalog["invalid_entries"], [])
        self.assertEqual(catalog["summary"]["candidate_directory_count"], 0)
        verify_experiment_catalog_report(catalog)

    def test_symbolic_link_root_is_rejected(self) -> None:
        linked_root = self.root / "linked-experiments"
        linked_root.symlink_to(self.valid_root, target_is_directory=True)

        with self.assertRaisesRegex(ExperimentCatalogError, "symbolic link"):
            build_experiment_catalog(linked_root)

    def test_failed_artifact_diagnosis_does_not_follow_manifest_symlink(self) -> None:
        catalog = build_experiment_catalog(self.manifest_symlink_root)

        self.assertEqual(catalog["status"], "attention_required")
        self.assertEqual(len(catalog["invalid_entries"]), 1)
        invalid = catalog["invalid_entries"][0]
        self.assertEqual(invalid["verification_state"], "verification_failed")
        self.assertNotIn("format_hint", invalid)

    def test_catalog_hash_and_derived_sections_detect_tampering(self) -> None:
        catalog = build_experiment_catalog(self.valid_root)
        tampered = json.loads(json.dumps(catalog))
        tampered["summary"]["verified_artifact_count"] = 0

        with self.assertRaisesRegex(ExperimentCatalogError, "summary is inconsistent"):
            verify_experiment_catalog_report(tampered)

    def test_cli_writes_exclusive_catalog_outside_scanned_root(self) -> None:
        catalog_path = self.root / "catalogs/catalog.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "catalog-experiments",
                    "--experiments-root",
                    str(self.experiments),
                    "--catalog-out",
                    str(catalog_path),
                ]
            )
        payload = json.loads(stdout.getvalue())
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 3)
        self.assertEqual(payload["status"], "attention_required")
        self.assertEqual(payload["catalog_sha256"], catalog["catalog_sha256"])
        verify_experiment_catalog_report(catalog)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            second_exit = main(
                [
                    "catalog-experiments",
                    "--experiments-root",
                    str(self.experiments),
                    "--catalog-out",
                    str(catalog_path),
                ]
            )
        self.assertEqual(second_exit, 2)
        self.assertIn("already exists", stderr.getvalue())

        with self.assertRaisesRegex(
            ExperimentCatalogError,
            "must not be written inside the experiments root",
        ):
            write_experiment_catalog(
                catalog, self.experiments / "catalog.json"
            )


if __name__ == "__main__":
    unittest.main()
