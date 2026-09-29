"""Two faults that reached production, and the checks that keep them there.

Both were silent in development and loud only to whoever was using the
deployed app. Neither would have been caught by testing the happy path,
because in development the happy path was genuinely happy: the dependency was
installed by accident, and the dimension names in the local fixtures were
short enough to fit.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from forge.build import naming

ROOT = Path(__file__).parents[1]


# --------------------------------------------------------------------------- #
# Declared dependencies
# --------------------------------------------------------------------------- #

def declared() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return [d.lower() for d in data["project"]["dependencies"]]


def test_pillow_is_a_declared_dependency():
    """It is imported at runtime, so it has to be installed by the install.

    The container is built from this list and nothing else. Pillow was missing
    from it while being present in a development virtualenv by accident, so
    every question crop 404'd in production and none of it showed up locally.
    Both call sites catch ImportError and return None, which is the right
    behaviour and also the reason nobody noticed for so long.
    """
    assert any(d.startswith("pillow") for d in declared())


def test_every_third_party_import_is_declared():
    """The general form of the bug above.

    A module that imports something the install does not provide works on any
    machine that happens to have it and fails on a clean one. Checking the
    imports against the dependency list is the only thing that notices, since
    the code itself runs fine either way.
    """
    import ast

    # Distribution name on the left, the module it provides on the right.
    provides = {
        "fastapi": {"fastapi"},
        "uvicorn": {"uvicorn"},
        "jinja2": {"jinja2"},
        "python-multipart": {"multipart"},
        "psycopg": {"psycopg", "psycopg_pool"},
        "argon2-cffi": {"argon2"},
        "cryptography": {"cryptography"},
        "anthropic": {"anthropic"},
        "httpx": {"httpx"},
        "boto3": {"boto3", "botocore"},
        "pillow": {"PIL"},
    }
    available = set()
    for dist in declared():
        name = dist.split(">")[0].split("=")[0].split("[")[0].strip()
        available |= provides.get(name, {name.replace("-", "_")})

    # Provided by the runtime rather than by the install. `bmesh` is Blender's
    # mesh-editing API, used to taper a part and to lathe a turned profile -
    # ships with Blender exactly as bpy and mathutils do, and is importable
    # nowhere else.
    runtime = {"bpy", "mathutils", "bmesh"}

    import sys
    stdlib = set(sys.stdlib_module_names)

    missing = {}
    for path in (ROOT / "forge").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # relative import, our own code
                    continue
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                if not root or root in stdlib or root in runtime:
                    continue
                if root == "forge" or root in available:
                    continue
                missing.setdefault(root, str(path.relative_to(ROOT)))

    assert missing == {}, f"imported but not declared: {missing}"


# --------------------------------------------------------------------------- #
# Blender custom-property names
# --------------------------------------------------------------------------- #

LONG = ("Overall width measured across the outside faces of both side panels "
        "including the applied edging")


def test_a_long_dimension_name_still_fits_blenders_limit():
    """63 characters, and Blender raises rather than truncating.

    A build that had made all 63 objects correctly died attaching a label to
    one of them: "KeyError: the length of IDProperty names is limited to 63
    characters". The name had been shortened to 60 - and then `_source` was
    appended to it.
    """
    part = {"id": "carcass.side", "dimensions": [
        {"name": LONG, "value": 1800, "unit": "mm", "source": "user"}]}
    for key in naming.properties_for(part, 1, 0):
        assert len(key) <= naming.MAX_PROPERTY_NAME, key


def test_the_value_and_source_keys_are_measured_together():
    """The pair fits or neither does; measuring only one is what broke."""
    key = naming.property_key("forge_dim_" + "x" * 200,
                              reserve=len(naming.SOURCE_SUFFIX))
    assert len(key + naming.SOURCE_SUFFIX) <= naming.MAX_PROPERTY_NAME


def test_shortened_keys_stay_distinct():
    """Two long names that share a prefix must not collapse into one key.

    Truncation alone would silently overwrite one dimension with another, so
    the object would carry a number belonging to a different measurement.
    """
    a = naming.property_key("forge_dim_" + "overall width across " * 5 + "left")
    b = naming.property_key("forge_dim_" + "overall width across " * 5 + "right")
    assert a != b


def test_a_shortened_key_keeps_the_prefix_that_identifies_it():
    """`sanitise` keeps the tail, which for these keys drops `forge_dim_`."""
    key = naming.property_key("forge_dim_" + "y" * 200)
    assert key.startswith("forge_dim_")


def test_properties_survive_a_part_with_no_dimensions():
    assert naming.properties_for({"id": "x"}, 1, 0)["forge_part_id"] == "x"


# --------------------------------------------------------------------------- #
# The crop endpoint's dependency
# --------------------------------------------------------------------------- #

def test_cropping_reports_a_missing_pillow_rather_than_going_quiet(caplog):
    """Returning None is right; doing it without a word is what hid the bug."""
    import sys

    class Block:
        """A meta-path finder that makes PIL unimportable.

        `find_spec`, not the `find_module` that was removed in 3.12 - a
        finder with only the old method is simply ignored, so the import
        succeeds and the test passes without testing anything.
        """

        def find_spec(self, name, path=None, target=None):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError(f"No module named {name!r}")
            return None

    from forge.ingest import crop

    saved = {k: v for k, v in sys.modules.items() if k.startswith("PIL")}
    for key in saved:
        del sys.modules[key]
    sys.meta_path.insert(0, Block())
    try:
        with caplog.at_level("ERROR"):
            assert crop.crop_region(b"x", 0.1, 0.1, 0.2, 0.2) is None
        assert "Pillow" in caplog.text
    finally:
        sys.meta_path.pop(0)
        sys.modules.update(saved)


def test_cropping_a_real_image_produces_a_png():
    from PIL import Image
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 100, 50)).save(buffer, format="PNG")

    from forge.ingest import crop
    out = crop.crop_region(buffer.getvalue(), 0.25, 0.25, 0.3, 0.3)
    assert out is not None
    assert out[:8] == b"\x89PNG\r\n\x1a\n"
