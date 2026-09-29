"""Turn uploaded files into a PartSpec."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .. import audit, db, settings_store
from ..ingest import grid
from ..ingest import pdf as pdf_reader
from ..spec import schema as spec_schema
from ..storage import keys
from ..storage.backend import for_org
from .client import complete_json

PROMPT = (Path(__file__).parent / "prompts" / "extract.md").read_text()

# What we send per extraction. A drawing with several views is worth more than
# several photos of the same angle, but the cost is per image either way.
MAX_IMAGES = 4

# Of that budget, how much one PDF may take. A four-page spec sheet is the
# whole story; a four-page PDF plus three photos is not worth crowding out the
# photos for, because the photos are what show the object as it really is.
MAX_PDF_PAGES = 3


def _coerce(data: dict) -> dict:
    """Make a nearly-right document right.

    Only for providers whose JSON mode guarantees valid JSON but not the schema.
    This fills in absent optional structure and drops junk - it never invents a
    measurement, because a fabricated number is exactly what the rest of the
    system is built to avoid.
    """
    out = {
        "object": {},
        "overall": [],
        "parts": [],
        "joints": [],
        "missing_dimensions": [],
        "assumptions": [],
        "readability": {},
    }
    if not isinstance(data, dict):
        return out

    obj = data.get("object") if isinstance(data.get("object"), dict) else {}
    out["object"] = {
        "category": str(obj.get("category", "unknown"))[:60],
        "subtype": str(obj.get("subtype", ""))[:120],
        "style_notes": str(obj.get("style_notes", ""))[:2000],
        "summary": str(obj.get("summary", ""))[:600],
    }

    def measurements(raw):
        result = []
        for m in raw if isinstance(raw, list) else []:
            if not isinstance(m, dict) or "value" not in m:
                continue
            try:
                value = float(m["value"])
            except (TypeError, ValueError):
                continue
            result.append({
                "name": str(m.get("name", "size"))[:60],
                "value": value,
                "unit": m.get("unit") if m.get("unit") in spec_schema.UNITS else "mm",
                "source": (m.get("source") if m.get("source") in spec_schema.SOURCES
                           else "estimated"),
                "confidence": max(0.0, min(1.0, float(m.get("confidence", 0.4) or 0))),
                "note": str(m.get("note", ""))[:400],
            })
        return result

    out["overall"] = measurements(data.get("overall"))

    def num_or(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def placement(raw, count: int):
        """Normalise a placement, or drop it entirely.

        Returned as None when there is nothing usable, because the geometry
        treats "no placement" and "a placement I do not believe" differently:
        the first falls back to its own conventions, the second puts the part
        beside the object. Inventing a centred, zero-tilt placement here would
        collapse that distinction and quietly assert that every part is in the
        middle of the object, which is a much worse answer than silence.
        """
        if not isinstance(raw, dict):
            return None

        def frac(key, default):
            try:
                return max(-0.5, min(1.5, float(raw.get(key, default))))
            except (TypeError, ValueError):
                return default

        def align(key):
            value = str(raw.get(key, "center")).lower()
            return value if value in spec_schema.ALIGNMENTS else "center"

        def degrees(key, limit):
            try:
                return max(-limit, min(limit, float(raw.get(key, 0.0) or 0.0)))
            except (TypeError, ValueError):
                return 0.0

        axis = str(raw.get("run_axis", "")).lower()
        pattern = str(raw.get("pattern", "")).lower()
        if pattern not in spec_schema.PATTERNS:
            # A count above one with no arrangement is the case worth guessing
            # at: stacking every copy in one place is never what was meant, and
            # four of something in furniture is four corners far more often
            # than it is anything else.
            pattern = "single" if count <= 1 else (
                "mirror_xy" if count == 4 else "row_x")

        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5

        return {
            "anchor_x": frac("anchor_x", 0.5),
            "anchor_y": frac("anchor_y", 0.5),
            "anchor_z": frac("anchor_z", 0.5),
            "align_x": align("align_x"),
            "align_y": align("align_y"),
            "align_z": align("align_z"),
            "run_axis": axis if axis in spec_schema.RUN_AXES else "",
            "tilt_x": degrees("tilt_x", 90.0),
            "tilt_y": degrees("tilt_y", 90.0),
            "tilt_z": degrees("tilt_z", 180.0),
            "pattern": pattern,
            "spacing_mm": max(0.0, min(10000.0, num_or(raw.get("spacing_mm"), 0.0))),
            "confidence": confidence,
        }

    def appearance(raw):
        if not isinstance(raw, dict):
            return None
        # A colour that does not parse is dropped rather than repaired. The
        # builder's material table is a better guess than half a hex code.
        colour = spec_schema.parse_hex(raw.get("hex"))
        finish = str(raw.get("finish", "")).lower()
        source = str(raw.get("source", "")).lower()
        hex_value = str(raw.get("hex") or "").strip()
        if colour is None and not finish:
            return None
        return {
            "hex": hex_value if colour is not None else "",
            "finish": finish if finish in spec_schema.FINISHES else "",
            "source": source if source in ("observed", "assumed") else "assumed",
            "note": str(raw.get("note", ""))[:400],
        }

    def artwork(raw):
        """Printed artwork on one face, or nothing.

        Dropped unless it names a region with real area: a sliver stretched
        across a poster panel is worse than the flat colour it replaces.
        """
        if not isinstance(raw, dict) or not raw.get("found"):
            return None
        width = max(0.0, min(1.0, num_or(raw.get("w"), 0.0)))
        height = max(0.0, min(1.0, num_or(raw.get("h"), 0.0)))
        if width < 0.01 or height < 0.01:
            return None
        face = str(raw.get("face", "front")).lower()
        try:
            index = max(0, int(raw.get("image_index", 0)))
        except (TypeError, ValueError):
            index = 0
        return {
            "image_index": index,
            "x": max(0.0, min(1.0, num_or(raw.get("x"), 0.0))),
            "y": max(0.0, min(1.0, num_or(raw.get("y"), 0.0))),
            "w": width,
            "h": height,
            "face": face if face in spec_schema.FACES else "front",
            "found": True,
            "describes": str(raw.get("describes", ""))[:200],
        }

    def shape(raw):
        if not isinstance(raw, dict):
            return None
        taper = max(0.05, min(3.0, num_or(raw.get("taper"), 1.0)))
        radius = max(0.0, min(200.0, num_or(raw.get("edge_radius_mm"), 0.0)))
        points = []
        for point in raw.get("profile") if isinstance(raw.get("profile"), list) else []:
            if not isinstance(point, dict):
                continue
            points.append({
                "at": max(0.0, min(1.0, num_or(point.get("at"), 0.0))),
                "scale": max(0.02, min(4.0, num_or(point.get("scale"), 1.0))),
            })
        points.sort(key=lambda p: p["at"])
        if len(points) < 2:
            points = []
        if taper == 1.0 and radius == 0.0 and not points:
            return None
        return {"taper": taper, "edge_radius_mm": radius, "profile": points}

    for p in data.get("parts") if isinstance(data.get("parts"), list) else []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        count = max(1, int(p.get("count", 1) or 1))
        part = {
            "id": str(p["id"])[:80],
            "display_name": str(p.get("display_name", p["id"]))[:120],
            "parent_id": str(p.get("parent_id", ""))[:80],
            "role": p.get("role") if p.get("role") in spec_schema.ROLES else "structural",
            "primitive": (p.get("primitive")
                          if p.get("primitive") in spec_schema.PRIMITIVES else "box"),
            "material": str(p.get("material", "unspecified"))[:60],
            "count": count,
            "dimensions": measurements(p.get("dimensions")),
            "notes": str(p.get("notes", ""))[:800],
        }
        # Omitted rather than filled with defaults, so a spec carries only what
        # was actually read. Everything downstream copes with their absence.
        for key, value in (("placement", placement(p.get("placement"), count)),
                           ("appearance", appearance(p.get("appearance"))),
                           ("shape", shape(p.get("shape"))),
                           ("artwork", artwork(p.get("artwork")))):
            if value is not None:
                part[key] = value
        out["parts"].append(part)

    for j in data.get("joints") if isinstance(data.get("joints"), list) else []:
        if not isinstance(j, dict) or not j.get("type"):
            continue
        parts_named = [str(x)[:80] for x in (j.get("parts") or [])
                       if isinstance(x, (str, int))]
        out["joints"].append({
            "id": str(j.get("id") or f"j{len(out['joints']) + 1}")[:80],
            "type": (j["type"] if j["type"] in spec_schema.JOINT_TYPES
                     else "butt_screwed"),
            "parts": parts_named,
            "count": max(1, int(j.get("count", 1) or 1)),
            "notes": str(j.get("notes", ""))[:400],
        })

    raw_missing = data.get("missing_dimensions")
    for q in raw_missing if isinstance(raw_missing, list) else []:
        if not isinstance(q, dict) or not q.get("dimension"):
            continue
        def num(key):
            try:
                return float(q.get(key))
            except (TypeError, ValueError):
                return 0.0
        region = q.get("region") if isinstance(q.get("region"), dict) else {}
        def frac(key, default=0.0):
            try:
                return max(0.0, min(1.0, float(region.get(key, default))))
            except (TypeError, ValueError):
                return default

        try:
            # Absent or unparseable means the first image, which is the one a
            # single-file upload always has.
            index = max(0, int(region.get("image_index", 0)))
        except (TypeError, ValueError):
            index = 0

        out["missing_dimensions"].append({
            "part_id": str(q.get("part_id", ""))[:80],
            "region": {
                "image_index": index,
                "x": frac("x"), "y": frac("y"),
                "w": frac("w", 1.0), "h": frac("h", 1.0),
                "found": bool(region.get("found", False)),
            },
            "dimension": str(q["dimension"])[:60],
            "question": str(q.get("question", ""))[:500],
            "why_needed": str(q.get("why_needed", ""))[:500],
            "unit": q.get("unit") if q.get("unit") in spec_schema.UNITS else "mm",
            "suggested_min": num("suggested_min"),
            "suggested_max": num("suggested_max"),
            "blocking": bool(q.get("blocking", True)),
        })

    assumptions = data.get("assumptions")
    out["assumptions"] = [str(a)[:400]
                          for a in (assumptions if isinstance(assumptions, list) else [])]

    read = data.get("readability") if isinstance(data.get("readability"), dict) else {}
    out["readability"] = {
        "notes": str(read.get("notes", ""))[:1500],
        "labels_found": bool(read.get("labels_found", False)),
    }
    return out


def relink(spec: dict) -> list[str]:
    """Clear parent references that name no part, and say what was cleared.

    A model describing a chest of drawers naturally writes `parent_id:
    "drawer"` on the front, sides, base and runners - and then does not emit a
    "drawer" part, because a drawer is a grouping rather than a piece of
    timber. The builder skips a parent it cannot resolve, so the parts are
    still built and still separately selectable; they are simply not nested.

    Left in place the reference is a lie in the spec that `problems()` reports
    on every revision and nothing can act on. Cleared, the spec says what is
    actually true of the model that gets built.

    Real nesting needs a part that carries no geometry - an empty transform
    the children hang from - which the primitive set cannot express yet. Until
    it can, this is the honest representation rather than the flattering one.
    """
    ids = {p["id"] for p in spec.get("parts", [])}
    cleared: dict[str, list[str]] = {}
    for part in spec.get("parts", []):
        parent = part.get("parent_id") or ""
        if parent and parent not in ids:
            cleared.setdefault(parent, []).append(part["display_name"])
            part["parent_id"] = ""

    notes = []
    for parent, children in sorted(cleared.items()):
        notes.append(
            f"{len(children)} part(s) were described as belonging to a "
            f"“{parent}” sub-assembly that was not itself listed, so they are "
            f"built as separate objects at the top level: "
            f"{', '.join(children[:6])}"
            + ("…" if len(children) > 6 else "") + "."
        )
    spec.setdefault("assumptions", []).extend(notes)
    return notes


def problems(spec: dict) -> list[str]:
    """Structural complaints about a spec, in plain language."""
    issues = []
    if not spec.get("parts"):
        issues.append("No parts were identified.")

    ids = {p["id"] for p in spec.get("parts", [])}
    for p in spec.get("parts", []):
        if p.get("parent_id") and p["parent_id"] not in ids:
            issues.append(
                f"Part “{p['display_name']}” refers to a parent that does not "
                f"exist ({p['parent_id']})."
            )
        for d in p.get("dimensions", []):
            if d["value"] <= 0:
                issues.append(
                    f"“{p['display_name']}” has a {d['name']} of {d['value']}, "
                    f"which cannot be built."
                )
    for q in spec.get("missing_dimensions", []):
        if q["part_id"] and q["part_id"] not in ids:
            issues.append(
                f"A question refers to an unknown part ({q['part_id']})."
            )
    return issues


class Gathered:
    """Everything the model will be shown, and a record of what it was.

    `images` is what goes to the provider. `sources` describes the same list
    position by position, so a question's `region.image_index` can be turned
    back into a picture long after the call. `text` is the PDF text layer,
    which is quoted into the prompt rather than read off a raster.
    """

    def __init__(self):
        self.images: list[tuple[bytes, str]] = []
        self.sources: list[dict] = []
        self.text_blocks: list[str] = []
        self.notes: list[str] = []

    def add(self, data: bytes, mime: str, *, asset_id: str, label: str,
            kind: str, page: int | None = None, storage_key: str = "") -> None:
        # The model is shown a copy with a labelled 0-1 grid drawn on it, so a
        # region can be READ off the picture rather than estimated from it.
        # Crops are always cut from the original bytes, which is why the
        # original is what gets recorded in `sources` - no gridline can ever
        # end up printed on a model.
        self.images.append((grid.annotate(data), "image/png"))
        self.sources.append({
            "asset_id": str(asset_id), "page": page, "kind": kind,
            "label": label, "storage_key": storage_key,
        })

    @property
    def text(self) -> str:
        return "\n\n".join(self.text_blocks)

    @property
    def manifest(self) -> str:
        """The 'Image N:' list, which region.image_index refers to."""
        return "\n".join(f"Image {i}: {s['label']}"
                         for i, s in enumerate(self.sources))


def gather_sources(org_id: str, assets: list[dict]) -> Gathered:
    """Turn the item's files into images the model can see, plus exact text.

    Photographs go through as they are. PDFs are read for their text layer and
    rasterised page by page: the text is the authoritative copy of any stated
    dimension, and the raster is how the model sees which part it belongs to.

    Images are numbered from 0 in the order they are added here, and that
    numbering is what the prompt shows and what a crop region indexes into.
    """
    storage = for_org(org_id)
    out = Gathered()

    # Photographs first, so that when the budget runs out it is a PDF page
    # that is dropped: a picture of the actual object is the harder thing to
    # reconstruct from anything else.
    photos = [a for a in assets if a["detected_mime"] != "application/pdf"]
    pdfs = [a for a in assets if a["detected_mime"] == "application/pdf"]

    for a in photos:
        if len(out.images) >= MAX_IMAGES:
            out.notes.append(f"“{a['original_name']}” was not read - only "
                             f"{MAX_IMAGES} images are read per run.")
            continue
        out.add(storage.get(a["storage_key"]), a["detected_mime"],
                asset_id=a["id"], label=a["original_name"], kind="image",
                storage_key=a["storage_key"])

    for a in pdfs:
        room = MAX_IMAGES - len(out.images)
        if room <= 0:
            out.notes.append(f"“{a['original_name']}” was not read - the image "
                             f"budget was already full.")
            continue

        data = storage.get(a["storage_key"])
        read = pdf_reader.read(data, max_pages=min(room, MAX_PDF_PAGES))

        if read.text_layer:
            out.text_blocks.append(
                f"### Text layer of “{a['original_name']}”\n"
                f"{read.text_layer}"
            )
        if read.scanned:
            out.notes.append(
                f"“{a['original_name']}” has no text layer - it is a scan or "
                f"an exported drawing, so every number in it has to be read "
                f"off the page."
            )
        if read.skipped:
            out.notes.append(
                f"“{a['original_name']}” has {read.total_pages} pages; pages "
                f"{', '.join(str(p.number) for p in read.pages)} were chosen "
                f"as the ones describing the object."
            )

        for page in read.pages:
            if page.image is None:
                continue
            # Kept, so a crop for a question does not mean rendering the PDF
            # again on a request that has a person waiting on it.
            key = keys.pdf_page(org_id, str(a["id"]), page.number,
                                dpi=pdf_reader.RENDER_DPI)
            try:
                storage.put(key, page.image, "image/png")
            except Exception:
                key = ""
            out.add(page.image, "image/png", asset_id=a["id"],
                    label=f"{a['original_name']} - page {page.number}"
                          f" of {read.total_pages}",
                    kind="pdf_page", page=page.number, storage_key=key)

    return out


def _describe(item: dict, gathered: Gathered) -> str:
    """The user turn: what the object is, what the images are, exact text."""
    parts = [
        f"This is “{item['name']}”"
        + (f", a {item['category']}." if item["category"] else "."),
    ]
    if item.get("project_name"):
        parts.append(f"It is part of the project “{item['project_name']}”.")
    if item["notes"]:
        parts.append(f"Notes from the user: {item['notes']}")

    parts.append(f"You are shown {len(gathered.images)} image(s), numbered "
                 f"from 0. `region.image_index` refers to these numbers:\n"
                 f"{gathered.manifest}")

    if gathered.text:
        # Placed after the manifest and called out as exact: this text is a
        # copy of what the document says, not a reading of a picture, and a
        # dimension found here is the most reliable input the app ever gets.
        parts.append(
            "The following text was extracted directly from the PDF's text "
            "layer. It is **exact** - not read off an image. Any dimension "
            "stated here must be used as given, with source "
            "`pdf_text_layer`, and must not be re-estimated from the page "
            "rasters. Where the text and a drawing disagree, the text wins.\n\n"
            + gathered.text
        )

    if gathered.notes:
        parts.append("About these files:\n"
                     + "\n".join(f"- {n}" for n in gathered.notes))

    parts.append("Read the object and return the part specification.")
    return "\n\n".join(parts)


def run(org_id: str, item_id: str, user_id: str) -> dict:
    """Extract a PartSpec for one item. Returns a result summary."""
    from .. import items

    assets = items.assets(item_id)
    if not assets:
        return {"ok": False, "error": "This project has no files to read."}

    # Before resolving the model, make sure there is a catalogue to resolve it
    # against. Without this, a dynamic provider on a cold process falls back
    # to an invented model - wrong output ceiling, reasoning flagged off, no
    # price - and the extraction fails on a token limit that was never the
    # real one. See `openrouter.ensure`. This runs in a worker thread, so the
    # one-off fetch costs nobody else anything.
    from ..ai import openrouter
    openrouter.ensure()

    config = settings_store.ai_config(org_id)
    provider, model, key = config["provider"], config["model"], config["key"]
    effort = config.get("effort", "")

    if not key:
        return {"ok": False,
                "error": f"No API key is configured for {provider.label}. "
                         f"Add one in Settings."}
    if not model.vision:
        return {"ok": False,
                "error": f"{model.label} cannot read images. Choose a vision "
                         f"model in Settings."}

    try:
        gathered = gather_sources(org_id, assets)
    except pdf_reader.PdfError as exc:
        return {"ok": False, "error": str(exc)}

    if not gathered.images:
        return {"ok": False,
                "error": "Nothing readable is attached. Add a photo or a PDF."}

    item = items.get(org_id, item_id)
    user_text = _describe(item, gathered)

    db.execute("UPDATE items SET status = 'extracting' WHERE id = %s", (item_id,))

    result = complete_json(
        provider=provider, model=model, api_key=key,
        system=PROMPT, user_text=user_text, images=gathered.images,
        schema=spec_schema.PARTSPEC_SCHEMA,
        max_tokens=model.max_output,
        effort=effort,
    )

    # Recorded on failure as well as success: a provider outage is exactly when
    # the spend and error history matters.
    db.execute(
        """
        INSERT INTO llm_calls
            (org_id, actor_user_id, provider, model, purpose, input_tokens,
             output_tokens, cached_tokens, cost_usd, stop_reason, latency_ms,
             ok, error)
        VALUES (%s,%s,%s,%s,'extract',%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (org_id, user_id, provider.id, model.id, result.input_tokens,
         result.output_tokens, result.cached_tokens, result.cost_usd,
         result.stop_reason, result.latency_ms, result.ok, result.error or None),
    )

    if not result.ok:
        db.execute("UPDATE items SET status = 'failed' WHERE id = %s", (item_id,))
        audit.record("extract.failed", org_id=org_id, actor_user_id=user_id,
                     target_type="item", target_id=item_id,
                     metadata={"provider": provider.id, "model": model.id,
                               "error": result.error[:200]})
        return {"ok": False, "error": result.error, "raw": result.raw_text}

    spec = _coerce(result.data)
    # Before validating: relink drops references the builder could never have
    # honoured, so `issues` lists only things a person could act on.
    relink(spec)
    issues = problems(spec)

    revision = db.fetch_one(
        "SELECT coalesce(max(revision), 0) + 1 AS n FROM specs WHERE item_id = %s",
        (item_id,),
    )["n"]

    row = db.fetch_one(
        """
        INSERT INTO specs (id, org_id, item_id, revision, body, sources,
                           created_by, provider, model, input_tokens,
                           output_tokens, cost_usd)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (str(uuid.uuid4()), org_id, item_id, revision, json.dumps(spec),
         json.dumps(gathered.sources), user_id, provider.id, model.id,
         result.input_tokens, result.output_tokens, result.cost_usd),
    )

    blocking = spec_schema.blocking_questions(spec)
    status = "questions" if blocking else "ready"
    db.execute("UPDATE items SET status = %s, updated_at = now() WHERE id = %s",
               (status, item_id))

    audit.record(
        "extract.completed", org_id=org_id, actor_user_id=user_id,
        target_type="item", target_id=item_id,
        metadata={
            "model": model.id,
            "parts": len(spec["parts"]),
            "questions": len(spec["missing_dimensions"]),
            "cost_usd": round(result.cost_usd, 4),
        },
    )

    return {
        "ok": True,
        "spec": spec,
        "spec_id": row["id"],
        "revision": revision,
        "issues": issues,
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
        },
    }
