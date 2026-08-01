"""Loading and validation for the project-local approved asset catalog."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

AssetKind = Literal["template", "model", "material", "action"]
_GROUPS: dict[str, AssetKind] = {
    "templates": "template",
    "models": "model",
    "materials": "material",
    "actions": "action",
}


class AssetCatalogError(ValueError):
    """The catalog is malformed or references an unapproved path."""


class AssetRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: AssetKind
    path: Path
    version: str
    license: str
    source: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AssetCatalog:
    """Immutable deterministic view of approved project assets."""

    def __init__(self, project_root: Path, records: tuple[AssetRecord, ...]) -> None:
        self.project_root = project_root
        self._records = tuple(sorted(records, key=lambda item: (item.kind, item.id)))
        self._by_id = {record.id: record for record in self._records}

    @classmethod
    def load(
        cls,
        project_root: str | Path,
        catalog_path: str | Path = "assets/catalog.toml",
    ) -> "AssetCatalog":
        root = Path(project_root).expanduser().resolve(strict=True)
        relative_catalog = Path(catalog_path)
        if relative_catalog.is_absolute():
            raise AssetCatalogError("catalog path must be project-relative")
        catalog = _resolve_inside(root, relative_catalog, must_exist=True)
        try:
            with catalog.open("rb") as handle:
                document = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise AssetCatalogError(f"unable to read asset catalog: {exc}") from exc

        if document.get("schema_version") != "1":
            raise AssetCatalogError("asset catalog schema_version must be '1'")
        unknown_groups = set(document) - {"schema_version", *_GROUPS}
        if unknown_groups:
            raise AssetCatalogError(
                f"unknown asset catalog keys: {', '.join(sorted(unknown_groups))}"
            )

        records: list[AssetRecord] = []
        seen: set[str] = set()
        for group, kind in _GROUPS.items():
            entries = document.get(group, [])
            if not isinstance(entries, list):
                raise AssetCatalogError(f"{group} must be an array of TOML tables")
            for raw in entries:
                if not isinstance(raw, dict):
                    raise AssetCatalogError(f"{group} entries must be TOML tables")
                try:
                    record = AssetRecord.model_validate({**raw, "kind": kind})
                except ValidationError as exc:
                    raise AssetCatalogError(
                        f"invalid {kind} asset metadata: {exc}"
                    ) from exc
                if not record.id or record.id.strip() != record.id:
                    raise AssetCatalogError("asset id must be a non-empty trimmed string")
                if record.id in seen:
                    raise AssetCatalogError(f"duplicate asset id: {record.id}")
                if record.path.is_absolute():
                    raise AssetCatalogError(
                        f"asset path must be project-relative: {record.path}"
                    )
                resolved_asset = _resolve_inside(
                    root,
                    record.path,
                    must_exist=True,
                )
                _verify_asset_hash(record, resolved_asset)
                seen.add(record.id)
                records.append(record)
        return cls(root, tuple(records))

    def list(self, kind: str | None = None) -> tuple[AssetRecord, ...]:
        if kind is None:
            return self._records
        normalized = kind.removesuffix("s")
        if normalized not in set(_GROUPS.values()):
            raise AssetCatalogError(f"unsupported asset kind: {kind}")
        return tuple(record for record in self._records if record.kind == normalized)

    def get(self, asset_id: str) -> AssetRecord:
        try:
            return self._by_id[asset_id]
        except KeyError as exc:
            raise AssetCatalogError(f"asset is not approved: {asset_id}") from exc

    def resolve(self, asset_id: str) -> Path:
        record = self.get(asset_id)
        resolved = _resolve_inside(
            self.project_root,
            record.path,
            must_exist=True,
        )
        _verify_asset_hash(record, resolved)
        return resolved


def _resolve_inside(root: Path, relative: Path, *, must_exist: bool) -> Path:
    if relative.is_absolute():
        raise AssetCatalogError("path must be project-relative")
    try:
        resolved = (root / relative).resolve(strict=must_exist)
    except OSError as exc:
        raise AssetCatalogError(f"catalog path does not exist: {relative}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AssetCatalogError(f"catalog path escapes project root: {relative}") from exc
    return resolved


def _verify_asset_hash(record: AssetRecord, path: Path) -> None:
    if record.sha256 is None:
        return
    if not path.is_file():
        raise AssetCatalogError(f"hashed asset must be a regular file: {record.id}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AssetCatalogError(f"unable to hash approved asset: {record.id}") from exc
    if digest.hexdigest() != record.sha256:
        raise AssetCatalogError(f"asset sha256 mismatch: {record.id}")
