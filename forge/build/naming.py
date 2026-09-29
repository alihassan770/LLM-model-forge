"""The naming and hierarchy contract.

This module is the reason a generated model is editable. It is enforced in code
rather than left to convention, because "every part individually selectable" is
a property of *how* the file is built - it cannot be added afterwards.

Three guarantees:

* Every part is its own mesh object, never merged into a neighbour.
* Object names are derived from the spec's part ids, so a designer looking at
  the outliner sees the same names the specification uses.
* Parts are grouped into collections by role, so a modeller can hide all the
  hardware and work on the frame.

Blender truncates object names at 63 characters and silently appends `.001` to
duplicates. Both would break the link back to the spec, so names are shortened
deliberately here and uniqueness is tracked rather than discovered.
"""

from __future__ import annotations

import hashlib

MAX_NAME = 60

# Blender refuses an IDProperty name longer than 63 characters, and refuses it
# by raising rather than by truncating. That is a different limit from MAX_NAME
# above, which is about object names, and conflating the two cost a whole
# build: a dimension the model had called "handle length and fixing centres"
# became `forge_dim_handle_length_and_fixing_centres`, which fits - and then
# `..._source` was appended to it, which does not. One over-long dimension name
# failed all 63 objects with "the length of IDProperty names is limited to 63
# characters", after the geometry had been built correctly.
MAX_PROPERTY_NAME = 63

SOURCE_SUFFIX = "_source"

# Role → collection. A modeller wants to isolate the frame from the upholstery,
# which is the split that matters when editing furniture.
COLLECTION_FOR_ROLE = {
    "structural": "Frame",
    "surface": "Surfaces",
    "upholstery": "Upholstery",
    "hardware": "Hardware",
    "decorative": "Details",
}

COLLECTION_ORDER = ["Frame", "Surfaces", "Upholstery", "Hardware", "Details"]


def collection_for(part: dict) -> str:
    return COLLECTION_FOR_ROLE.get(part.get("role", "structural"), "Details")


def sanitise(name: str) -> str:
    """A name Blender will keep verbatim."""
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    cleaned = cleaned.strip("._-") or "part"
    if len(cleaned) <= MAX_NAME:
        return cleaned
    # Keep the tail: the distinguishing part of `frame.rail.front_left` is at
    # the end, so truncating from the front preserves what identifies it.
    return cleaned[-MAX_NAME:].lstrip("._-")


class Names:
    """Hands out unique object names and remembers what they map to."""

    def __init__(self):
        self._used: set[str] = set()

    def unique(self, base: str) -> str:
        candidate = sanitise(base)
        if candidate not in self._used:
            self._used.add(candidate)
            return candidate
        n = 2
        while True:
            suffix = f"_{n}"
            trimmed = candidate[: MAX_NAME - len(suffix)]
            attempt = f"{trimmed}{suffix}"
            if attempt not in self._used:
                self._used.add(attempt)
                return attempt
            n += 1

    def instance(self, part_id: str, index: int, total: int) -> str:
        """Name for one instance of a part that has `total` copies.

        A single leg is `leg`; four legs are `leg_1 … leg_4`. Numbering only
        when there is more than one keeps the common case clean.
        """
        if total <= 1:
            return self.unique(part_id)
        return self.unique(f"{part_id}_{index + 1}")


# Custom properties written onto every object. A designer opening the .blend can
# see exactly which specification line produced that geometry, and a future
# round-trip importer can read edits back out.
def property_key(name: str, reserve: int = 0) -> str:
    """A custom-property name Blender will accept, and that stays unique.

    `reserve` is room to leave for a suffix the caller intends to add, so the
    longer of the two names is the one measured against the limit.

    Over-long names keep their head rather than their tail - the opposite of
    `sanitise` - because the head is `forge_dim_`, which is how anything
    reading the file back finds these at all. What is lost from the tail comes
    back as a short digest of the whole name, so two dimensions that truncate
    to the same prefix still get different keys instead of one quietly
    overwriting the other.
    """
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    cleaned = cleaned.strip("_").lower() or "prop"

    limit = MAX_PROPERTY_NAME - reserve
    if len(cleaned) <= limit:
        return cleaned

    digest = hashlib.blake2s(cleaned.encode("utf-8"), digest_size=3).hexdigest()
    return f"{cleaned[:limit - len(digest) - 1]}_{digest}"


def properties_for(part: dict, spec_revision: int, index: int) -> dict:
    props = {
        "forge_part_id": part["id"],
        "forge_display_name": part.get("display_name", part["id"]),
        "forge_role": part.get("role", ""),
        "forge_primitive": part.get("primitive", ""),
        "forge_material": part.get("material", ""),
        "forge_spec_revision": spec_revision,
        "forge_instance": index + 1,
        "forge_count": part.get("count", 1),
    }
    for d in part.get("dimensions", []):
        # Measured with room for the companion `_source` key, so the pair fits
        # or neither does.
        key = property_key(f"forge_dim_{d.get('name', 'size')}",
                           reserve=len(SOURCE_SUFFIX))
        props[key] = f"{d.get('value')}{d.get('unit', 'mm')}"
        props[key + SOURCE_SUFFIX] = d.get("source", "")
    return props
