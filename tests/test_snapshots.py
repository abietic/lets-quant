import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lets_quant.data import DataError
from lets_quant.snapshots import load_snapshot, snapshot_file


ROOT = Path(__file__).resolve().parents[1]


class RawSnapshotTest(unittest.TestCase):
    def test_changed_payload_creates_a_new_snapshot_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            source = temporary / "bars.csv"
            source.write_text("date,value\n2025-01-02,1\n", encoding="utf-8")
            arguments = {
                "input_path": source,
                "provider": "local_csv",
                "provider_version": "1",
                "dataset": "test_bars",
                "request": {"symbol": "AAA"},
                "license_manifest_path": (
                    ROOT / "config/data_providers.example.json"
                ),
                "output_root": temporary / "raw",
                "fetched_at": datetime(2025, 1, 3, tzinfo=timezone.utc),
            }

            first = snapshot_file(**arguments)
            duplicate = snapshot_file(**arguments)
            source.write_text("date,value\n2025-01-02,2\n", encoding="utf-8")
            revised = snapshot_file(**arguments)

            self.assertEqual(first.snapshot_id, duplicate.snapshot_id)
            self.assertNotEqual(first.snapshot_id, revised.snapshot_id)
            self.assertEqual(
                first.payload_path.read_text(encoding="utf-8"),
                "date,value\n2025-01-02,1\n",
            )

    def test_tampered_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary = Path(temp_dir)
            source = temporary / "bars.csv"
            source.write_text("a,b\n1,2\n", encoding="utf-8")
            snapshot = snapshot_file(
                input_path=source,
                provider="local_csv",
                provider_version="1",
                dataset="test_bars",
                request={},
                license_manifest_path=(
                    ROOT / "config/data_providers.example.json"
                ),
                output_root=temporary / "raw",
            )
            snapshot.payload_path.write_text("changed\n", encoding="utf-8")

            with self.assertRaisesRegex(DataError, "hash mismatch"):
                load_snapshot(snapshot.directory)


if __name__ == "__main__":
    unittest.main()
