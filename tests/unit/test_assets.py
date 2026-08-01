import hashlib
from pathlib import Path

import pytest

from blender_mcp.assets import AssetCatalog, AssetCatalogError


def test_catalog_lists_approved_assets_deterministically(tmp_path: Path) -> None:
    (tmp_path / "templates").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "templates" / "base.blend").touch()
    (tmp_path / "models" / "hero.blend").touch()
    (tmp_path / "assets").mkdir()
    catalog_path = tmp_path / "assets" / "catalog.toml"
    catalog_path.write_text(
        """
schema_version = "1"

[[models]]
id = "hero"
path = "models/hero.blend"
version = "2"
license = "CC0-1.0"

[[templates]]
id = "base"
path = "templates/base.blend"
version = "1"
license = "Proprietary"
""".strip()
    )

    catalog = AssetCatalog.load(tmp_path)

    assert [record.id for record in catalog.list()] == ["hero", "base"]
    assert [record.id for record in catalog.list("template")] == ["base"]
    assert catalog.get("hero").path == Path("models/hero.blend")


def test_catalog_rejects_duplicate_ids(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "same.blend").touch()
    (tmp_path / "assets" / "catalog.toml").write_text(
        """
schema_version = "1"
[[models]]
id = "same"
path = "same.blend"
version = "1"
license = "CC0-1.0"
[[actions]]
id = "same"
path = "same.blend"
version = "1"
license = "CC0-1.0"
""".strip()
    )

    with pytest.raises(AssetCatalogError, match="duplicate asset id"):
        AssetCatalog.load(tmp_path)


def test_catalog_rejects_asset_path_escape(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    outside = tmp_path.parent / "outside.blend"
    outside.touch(exist_ok=True)
    (tmp_path / "assets" / "catalog.toml").write_text(
        """
schema_version = "1"
[[templates]]
id = "unsafe"
path = "../outside.blend"
version = "1"
license = "unknown"
""".strip()
    )

    with pytest.raises(AssetCatalogError, match="escapes project root"):
        AssetCatalog.load(tmp_path)


def test_catalog_rejects_symlink_escape(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    outside = tmp_path.parent / "unapproved.blend"
    outside.touch(exist_ok=True)
    (tmp_path / "linked.blend").symlink_to(outside)
    (tmp_path / "assets" / "catalog.toml").write_text(
        """
schema_version = "1"
[[models]]
id = "linked"
path = "linked.blend"
version = "1"
license = "unknown"
""".strip()
    )

    with pytest.raises(AssetCatalogError, match="escapes project root"):
        AssetCatalog.load(tmp_path)


def test_catalog_verifies_hash_on_load_and_resolve(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    asset = tmp_path / "template.blend"
    asset.write_bytes(b"approved")
    digest = hashlib.sha256(b"approved").hexdigest()
    (tmp_path / "assets" / "catalog.toml").write_text(
        f"""
schema_version = "1"
[[templates]]
id = "verified"
path = "template.blend"
version = "1"
license = "Proprietary"
sha256 = "{digest}"
""".strip()
    )
    catalog = AssetCatalog.load(tmp_path)

    assert catalog.resolve("verified") == asset

    asset.write_bytes(b"replaced")
    with pytest.raises(AssetCatalogError, match="sha256 mismatch"):
        catalog.resolve("verified")


def test_catalog_rejects_hash_mismatch_during_load(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "template.blend").write_bytes(b"not-approved")
    (tmp_path / "assets" / "catalog.toml").write_text(
        f"""
schema_version = "1"
[[templates]]
id = "mismatch"
path = "template.blend"
version = "1"
license = "Proprietary"
sha256 = "{'0' * 64}"
""".strip()
    )

    with pytest.raises(AssetCatalogError, match="sha256 mismatch"):
        AssetCatalog.load(tmp_path)
