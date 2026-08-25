from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_publication_boundary.py"
REQUIRED_IGNORES = """\
.env
.env.*
artifacts/
data/curated/
data/raw/
private/
secrets/
"""


class PublicationBoundaryTest(unittest.TestCase):
    def run_check(
        self,
        files: dict[str, str],
        *,
        gitignore: str = REQUIRED_IGNORES,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text(gitignore, encoding="utf-8")
            for path, content in files.items():
                file_path = root / path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-f", "."], check=True)
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--root", str(root)],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_safe_repository_passes(self) -> None:
        result = self.run_check({"README.md": "synthetic fixture only\n"})

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("publication boundary: pass", result.stdout)

    def test_private_paths_and_credential_containers_are_blocked(self) -> None:
        for path in (
            "artifacts/catalogs/catalog.json",
            "data/raw/quotes.csv",
            "private/account.json",
            "keys/broker.pem",
            ".ssh/id_ed25519",
            ".npmrc",
        ):
            with self.subTest(path=path):
                result = self.run_check({path: "placeholder\n"})

                self.assertEqual(1, result.returncode)
                self.assertIn(path, result.stderr)

    def test_sensitive_content_is_blocked(self) -> None:
        local_path = "/" + "Users/alice/accounts.csv"
        token = "ghp_" + "A" * 36
        private_key = "-" * 5 + "BEGIN OPENSSH PRIVATE KEY" + "-" * 5
        result = self.run_check(
            {"notes.txt": f"{local_path}\n{token}\n{private_key}\n"}
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("local absolute path", result.stderr)
        self.assertIn("GitHub token candidate", result.stderr)
        self.assertIn("private key material", result.stderr)

    def test_symbolic_links_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text(REQUIRED_IGNORES, encoding="utf-8")
            (root / "safe.txt").write_text("safe\n", encoding="utf-8")
            (root / "linked.txt").symlink_to("safe.txt")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--root", str(root)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(1, result.returncode)
        self.assertIn("tracked symbolic link", result.stderr)

    def test_required_ignore_rules_cannot_be_removed(self) -> None:
        result = self.run_check({"README.md": "safe\n"}, gitignore=".env\n")

        self.assertEqual(1, result.returncode)
        self.assertIn("required .gitignore rule missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
