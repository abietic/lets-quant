from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .data import DataError


SNAPSHOT_SCHEMA_VERSION = 1
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class RawSnapshot:
    directory: Path
    payload_path: Path
    manifest: Dict[str, Any]

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_segment(value: str, field: str) -> str:
    normalized = value.strip()
    if not _SAFE_SEGMENT.fullmatch(normalized):
        raise DataError(
            f"{field} must contain only letters, numbers, dot, dash, or underscore"
        )
    return normalized


def _load_provider_license(
    license_manifest_path: Path, provider: str
) -> tuple[Dict[str, Any], str]:
    try:
        raw = json.loads(license_manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataError(
            f"data provider license manifest not found: {license_manifest_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DataError(
            f"invalid JSON in {license_manifest_path}: "
            f"line {exc.lineno}, column {exc.colno}"
        ) from exc

    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise DataError("data provider license manifest schema_version must be 1")
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        raise DataError("data provider license manifest must contain providers")
    entry = providers.get(provider)
    if not isinstance(entry, dict):
        raise DataError(
            f"provider {provider!r} is missing from the license manifest"
        )
    required = {
        "display_name",
        "library_license",
        "data_rights",
        "allowed_use",
        "redistribution",
        "source_url",
        "terms_url",
        "notes",
    }
    missing = sorted(required - set(entry))
    if missing:
        raise DataError(
            f"provider {provider!r} license entry is missing: "
            f"{', '.join(missing)}"
        )
    if (
        not isinstance(entry["allowed_use"], list)
        or "personal_research" not in entry["allowed_use"]
    ):
        raise DataError(
            f"provider {provider!r} is not approved for personal_research"
        )
    return dict(entry), file_sha256(license_manifest_path)


def _snapshot_identity(
    *,
    provider: str,
    provider_version: str,
    dataset: str,
    request: Mapping[str, Any],
    payload_filename: str,
    payload_sha256: str,
    content_type: str,
    license_entry: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "provider": provider,
        "provider_version": provider_version,
        "dataset": dataset,
        "request": dict(request),
        "payload_filename": payload_filename,
        "payload_sha256": payload_sha256,
        "content_type": content_type,
        "license": dict(license_entry),
    }


def _identity_sha256(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def save_snapshot_bytes(
    *,
    content: bytes,
    payload_filename: str,
    provider: str,
    provider_version: str,
    dataset: str,
    request: Mapping[str, Any],
    license_manifest_path: Path,
    output_root: Path,
    content_type: str = "text/csv",
    fetched_at: Optional[datetime] = None,
) -> RawSnapshot:
    provider = _safe_segment(provider, "provider")
    dataset = _safe_segment(dataset, "dataset")
    provider_version = provider_version.strip()
    if not provider_version:
        raise DataError("provider_version must not be empty")
    if Path(payload_filename).name != payload_filename or not payload_filename:
        raise DataError("payload_filename must be a plain file name")
    try:
        canonical_json_bytes(dict(request))
    except (TypeError, ValueError) as exc:
        raise DataError("snapshot request must be JSON serializable") from exc

    license_entry, license_manifest_hash = _load_provider_license(
        license_manifest_path, provider
    )
    payload_hash = hashlib.sha256(content).hexdigest()
    identity = _snapshot_identity(
        provider=provider,
        provider_version=provider_version,
        dataset=dataset,
        request=request,
        payload_filename=payload_filename,
        payload_sha256=payload_hash,
        content_type=content_type,
        license_entry=license_entry,
    )
    snapshot_id = _identity_sha256(identity)
    destination = output_root / provider / dataset / snapshot_id
    if destination.exists():
        existing = load_snapshot(destination)
        if existing.snapshot_id != snapshot_id:
            raise DataError(f"snapshot directory collision: {destination}")
        return existing

    observed_at = fetched_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise DataError("fetched_at must include a timezone")

    manifest: Dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "artifact_type": "raw_data_snapshot",
        "snapshot_id": snapshot_id,
        "fetched_at": observed_at.isoformat(),
        "provider": provider,
        "provider_version": provider_version,
        "dataset": dataset,
        "request": dict(request),
        "content_type": content_type,
        "payload": {
            "filename": payload_filename,
            "sha256": payload_hash,
            "size_bytes": len(content),
        },
        "license_manifest_path": str(license_manifest_path.resolve()),
        "license_manifest_sha256": license_manifest_hash,
        "license": license_entry,
        "immutability": "content_addressed",
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{snapshot_id[:12]}-", dir=destination.parent)
    )
    try:
        (temporary / payload_filename).write_bytes(content)
        (temporary / "manifest.json").write_text(
            json.dumps(
                manifest, indent=2, sort_keys=True, ensure_ascii=False
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            temporary.rename(destination)
        except FileExistsError:
            existing = load_snapshot(destination)
            if existing.snapshot_id != snapshot_id:
                raise DataError(f"snapshot directory collision: {destination}")
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return load_snapshot(destination)


def snapshot_file(
    *,
    input_path: Path,
    provider: str,
    provider_version: str,
    dataset: str,
    request: Mapping[str, Any],
    license_manifest_path: Path,
    output_root: Path,
    content_type: str = "text/csv",
    fetched_at: Optional[datetime] = None,
) -> RawSnapshot:
    try:
        content = input_path.read_bytes()
    except FileNotFoundError as exc:
        raise DataError(f"snapshot input file not found: {input_path}") from exc
    if not input_path.is_file():
        raise DataError(f"snapshot input must be a file: {input_path}")
    return save_snapshot_bytes(
        content=content,
        payload_filename=input_path.name,
        provider=provider,
        provider_version=provider_version,
        dataset=dataset,
        request=request,
        license_manifest_path=license_manifest_path,
        output_root=output_root,
        content_type=content_type,
        fetched_at=fetched_at,
    )


def load_snapshot(path: Path) -> RawSnapshot:
    manifest_path = path if path.name == "manifest.json" else path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataError(f"snapshot manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(
            f"invalid snapshot manifest JSON: line {exc.lineno}, "
            f"column {exc.colno}"
        ) from exc
    if not isinstance(manifest, dict):
        raise DataError("snapshot manifest must be a JSON object")
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise DataError("unsupported snapshot schema_version")
    if manifest.get("artifact_type") != "raw_data_snapshot":
        raise DataError("path is not a raw data snapshot")

    payload = manifest.get("payload")
    if not isinstance(payload, dict):
        raise DataError("snapshot manifest payload is invalid")
    filename = payload.get("filename")
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or not filename
    ):
        raise DataError("snapshot payload filename is invalid")
    directory = manifest_path.parent
    payload_path = directory / filename
    if not payload_path.is_file():
        raise DataError(f"snapshot payload is missing: {payload_path}")
    actual_hash = file_sha256(payload_path)
    if actual_hash != payload.get("sha256"):
        raise DataError(f"snapshot payload hash mismatch: {payload_path}")

    identity = _snapshot_identity(
        provider=str(manifest.get("provider", "")),
        provider_version=str(manifest.get("provider_version", "")),
        dataset=str(manifest.get("dataset", "")),
        request=(
            manifest["request"]
            if isinstance(manifest.get("request"), dict)
            else {}
        ),
        payload_filename=filename,
        payload_sha256=actual_hash,
        content_type=str(manifest.get("content_type", "")),
        license_entry=(
            manifest["license"]
            if isinstance(manifest.get("license"), dict)
            else {}
        ),
    )
    expected_id = _identity_sha256(identity)
    if manifest.get("snapshot_id") != expected_id:
        raise DataError("snapshot manifest identity hash mismatch")
    if directory.name != expected_id:
        raise DataError("snapshot directory name does not match snapshot_id")
    return RawSnapshot(
        directory=directory, payload_path=payload_path, manifest=manifest
    )
