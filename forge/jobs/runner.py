"""Run a build: spec → Blender → artifacts in storage.

Blender is invoked as a subprocess, not imported. That is deliberate:

* a crash in a geometry operation kills the subprocess, not the web worker;
* a runaway build is killed by a wall-clock timeout we control;
* the Python inside Blender is Blender's own 3.11, independent of ours.

The specification is passed as a file rather than on the command line, because
a long spec would exceed the argument limit and a shell-quoted JSON blob is a
quoting bug waiting to happen.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .. import audit, db
from ..config import REPO_ROOT
from ..storage import keys
from ..storage.backend import for_org

# Named `logger`, not `log`: `run_job` already binds `log` to the Blender
# output it stores on the job row, and a module logger shadowed by a string
# fails only on the error path - the one place it is needed.
logger = logging.getLogger(__name__)

# A simple object takes a couple of seconds; the cap is for a spec that makes
# Blender pathological, not for normal work.
TIMEOUT_SECONDS = 300

DEFAULT_FORMATS = ["blend", "fbx", "obj", "glb", "stl"]

PREVIEW_PREFIX = "preview_"

MEDIA_TYPES = {
    "blend": "application/octet-stream",
    "fbx": "application/octet-stream",
    "obj": "text/plain",
    "glb": "model/gltf-binary",
    "stl": "model/stl",
    "mtl": "text/plain",
}


def find_blender() -> str | None:
    """Locate the Blender executable.

    FORGE_BLENDER wins so a container can point at its own install; otherwise
    PATH, then the places a local download usually lands.
    """
    explicit = os.environ.get("FORGE_BLENDER", "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None

    found = shutil.which("blender")
    if found:
        return found

    for candidate in sorted(Path.home().glob(".local/blender-*/blender"), reverse=True):
        if candidate.exists():
            return str(candidate)
    for candidate in ("/usr/local/bin/blender", "/opt/blender/blender"):
        if Path(candidate).exists():
            return candidate
    return None


def engine_version(executable: str) -> str:
    try:
        out = subprocess.run([executable, "--version"], capture_output=True,
                             text=True, timeout=30).stdout
        return out.splitlines()[0].strip() if out else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def create_job(org_id: str, item_id: str, spec_row: dict, user_id: str) -> dict:
    return db.fetch_one(
        """
        INSERT INTO jobs (org_id, item_id, spec_id, spec_revision, requested_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (org_id, item_id, spec_row["id"], spec_row["revision"], user_id),
    )


def _fit_to_face(data: bytes, part: dict, face: str) -> bytes:
    """Pad a crop out to the shape of the face it will be printed on.

    The projection in `artwork.apply` stretches an image across the whole face,
    so a crop whose proportions differ from the panel's is distorted by exactly
    that difference. On a display stand the Dove wordmark was cut as a roughly
    square 118 x 92 and printed onto a 900 x 160 crown - stretched four and a
    half times sideways, which is precisely the smeared lettering you see.

    Correcting it here rather than in the shader keeps one projection for every
    part and works in the preview and in every exported format at once: the
    image handed to Blender simply already has the panel's proportions.

    The padding is the part's own colour, so the artwork sits on the panel the
    way a printed decal does, with the panel showing around it.
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return data

    from ..spec import dims as spec_dims  # noqa: PLC0415
    from ..spec import schema as spec_schema  # noqa: PLC0415

    axes = spec_schema.FACE_AXES.get(face)
    if axes is None:
        return data
    extent = spec_dims.box_extent(part)
    across, up = extent[axes[0]], extent[axes[1]]
    if across <= 0 or up <= 0:
        return data

    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return data

    width, height = image.size
    if width <= 0 or height <= 0:
        return data

    # How much taller or wider the face is than the crop.
    face_ratio = across / up
    crop_ratio = width / height
    if abs(face_ratio - crop_ratio) < 0.02:
        return data                      # already the right shape

    if face_ratio > crop_ratio:          # face is wider: pad left and right
        target = (int(round(height * face_ratio)), height)
    else:                                # face is taller: pad top and bottom
        target = (width, int(round(width / face_ratio)))
    if target[0] < width or target[1] < height:
        return data

    colour = spec_schema.appearance_of(part)["rgb"]
    background = (tuple(int(round(c * 255)) for c in colour) if colour
                  else (255, 255, 255))

    canvas = Image.new("RGB", target, background)
    canvas.paste(image, ((target[0] - width) // 2, (target[1] - height) // 2))

    buffer = io.BytesIO()
    canvas.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def _cut_artwork(job: dict, spec: dict, workdir: Path) -> dict:
    """Crop each part's printed artwork out of the picture it was read from.

    Returns {part id: path to a PNG}. Everything here is best-effort: a logo
    that cannot be cut leaves the panel its flat colour, which is the model as
    it was before artwork existed. Losing a whole build over a crop would be a
    poor trade for a graphic.
    """
    from ..ingest import crop as crop_mod  # noqa: PLC0415
    from ..spec import schema as spec_schema  # noqa: PLC0415
    from ..web.routes.items import _region_source  # noqa: PLC0415

    wanted = [(p["id"], spec_schema.artwork_of(p)) for p in spec.get("parts", [])]
    wanted = [(pid, art) for pid, art in wanted if art]
    if not wanted:
        return {}

    spec_row = db.fetch_one("SELECT * FROM specs WHERE id = %s",
                            (job["spec_id"],))
    if spec_row is None:
        return {}

    art_dir = workdir / "artwork"
    art_dir.mkdir(exist_ok=True)

    # One source image is usually shared by every part on a display, so it is
    # fetched once rather than once per logo.
    cache: dict[int, bytes | None] = {}
    out: dict[str, str] = {}

    for part_id, art in wanted:
        index = art["image_index"]
        if index not in cache:
            try:
                cache[index] = _region_source(
                    job["org_id"], str(job["item_id"]), spec_row,
                    {"image_index": index})
            except Exception:  # noqa: BLE001
                cache[index] = None
        source = cache[index]
        if not source:
            continue
        try:
            cropped = crop_mod.crop_region(source, art["x"], art["y"],
                                           art["w"], art["h"])
        except Exception:  # noqa: BLE001
            cropped = None
        if not cropped:
            continue

        part = next((q for q in spec["parts"] if q["id"] == part_id), None)
        if part is not None:
            cropped = _fit_to_face(cropped, part, art["face"])

        # The part id is a dotted path and goes straight into a filename.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in part_id)
        path = art_dir / f"{safe}.png"
        path.write_bytes(cropped)
        out[part_id] = str(path)

    return out


def run_job(job_id: str, spec: dict, revision: int, model_name: str) -> dict:
    """Build one job to completion. Returns a summary dict."""
    job = db.fetch_one("SELECT * FROM jobs WHERE id = %s", (job_id,))
    if job is None:
        return {"ok": False, "error": "That job no longer exists."}

    blender = find_blender()
    if blender is None:
        message = (
            "Blender was not found on this machine. Set FORGE_BLENDER to its "
            "path, or install it - the worker container ships with it."
        )
        db.execute(
            "UPDATE jobs SET state='failed', error=%s, finished_at=now() WHERE id=%s",
            (message, job_id),
        )
        return {"ok": False, "error": message}

    db.execute(
        """
        UPDATE jobs SET state='running', started_at=now(), heartbeat_at=now(),
                        attempts = attempts + 1, engine=%s
        WHERE id=%s
        """,
        (engine_version(blender), job_id),
    )

    started = time.monotonic()
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_"
                        for c in (model_name or "model"))[:48] or "model"

    with tempfile.TemporaryDirectory(prefix="forge-build-") as workdir:
        spec_path = Path(workdir) / "spec.json"
        out_dir = Path(workdir) / "out"
        out_dir.mkdir()
        # Cut any printed artwork out of the uploaded pictures, so Blender is
        # handed real image files rather than coordinates it cannot resolve.
        # Done here rather than inside Blender because storage, the database
        # and Pillow all live on this side of the subprocess boundary.
        art_files = _cut_artwork(job, spec, Path(workdir))

        spec_path.write_text(json.dumps({
            "spec": spec, "revision": revision,
            "name": safe_name, "formats": DEFAULT_FORMATS,
            "artwork": art_files,
        }))

        command = [
            blender, "--background", "--factory-startup",
            "--python", str(REPO_ROOT / "forge" / "build" / "build.py"),
            "--", "--spec", str(spec_path), "--out", str(out_dir),
        ]

        try:
            completed = subprocess.run(
                command, capture_output=True, text=True,
                timeout=TIMEOUT_SECONDS, cwd=str(REPO_ROOT),
            )
        except subprocess.TimeoutExpired:
            message = f"The build exceeded {TIMEOUT_SECONDS} seconds and was stopped."
            db.execute(
                "UPDATE jobs SET state='failed', error=%s, finished_at=now() WHERE id=%s",
                (message, job_id),
            )
            return {"ok": False, "error": message}

        stdout, stderr = completed.stdout or "", completed.stderr or ""
        # Blender writes a great deal around our output, so the payload is
        # delimited rather than merely printed.
        payload = None
        if "FORGE_RESULT_BEGIN" in stdout:
            chunk = stdout.split("FORGE_RESULT_BEGIN", 1)[1]
            chunk = chunk.split("FORGE_RESULT_END", 1)[0].strip()
            try:
                payload = json.loads(chunk)
            except json.JSONDecodeError:
                payload = None

        log = (stdout[-6000:] + ("\n--- stderr ---\n" + stderr[-4000:] if stderr else ""))

        if payload is None:
            message = "Blender ran but produced no result. See the log."
            db.execute(
                """
                UPDATE jobs SET state='failed', error=%s, log=%s, finished_at=now()
                WHERE id=%s
                """,
                (message, log, job_id),
            )
            return {"ok": False, "error": message, "log": log}

        # --- store the files -------------------------------------------------
        # The job row carries the org, so a build writes into that
        # workspace's own bucket rather than the deployment default.
        storage = for_org(job["org_id"])
        stored, failed = [], []

        for entry in payload.get("exports", []):
            if not entry.get("ok"):
                failed.append(entry)
                continue
            source = out_dir / entry["file"]
            if not source.exists():
                failed.append({**entry, "ok": False, "error": "file missing"})
                continue

            data = source.read_bytes()
            key = keys.export(job["org_id"], job_id, safe_name, entry["format"])
            storage.put(key, data, MEDIA_TYPES.get(entry["format"],
                                                   "application/octet-stream"))
            db.execute(
                """
                INSERT INTO artifacts (org_id, job_id, format, filename,
                                       storage_key, bytes)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (job_id, format) DO UPDATE
                    SET storage_key = EXCLUDED.storage_key,
                        bytes = EXCLUDED.bytes
                """,
                (job["org_id"], job_id, entry["format"], entry["file"], key,
                 len(data)),
            )
            stored.append(entry["format"])

        # --- preview renders -------------------------------------------------
        # Stored as artifacts too, with a `preview_` format prefix so the
        # download list can separate pictures from deliverables without a
        # second table.
        for entry in payload.get("previews", []):
            if entry.get("error") or not entry.get("file"):
                continue
            source = out_dir / entry["file"]
            if not source.exists():
                continue
            data = source.read_bytes()
            key = keys.preview(job["org_id"], job_id, entry["view"])
            storage.put(key, data, "image/png")
            db.execute(
                """
                INSERT INTO artifacts (org_id, job_id, format, filename,
                                       storage_key, bytes)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (job_id, format) DO UPDATE
                    SET storage_key = EXCLUDED.storage_key, bytes = EXCLUDED.bytes
                """,
                (job["org_id"], job_id, f"preview_{entry['view']}",
                 entry["file"], key, len(data)),
            )

        # The .obj needs its .mtl alongside or materials are lost on import.
        mtl = out_dir / f"{safe_name}.mtl"
        if mtl.exists():
            key = keys.export(job["org_id"], job_id, safe_name, "mtl")
            storage.put(key, mtl.read_bytes(), "text/plain")
            db.execute(
                """
                INSERT INTO artifacts (org_id, job_id, format, filename,
                                       storage_key, bytes)
                VALUES (%s,%s,'mtl',%s,%s,%s)
                ON CONFLICT (job_id, format) DO UPDATE
                    SET storage_key = EXCLUDED.storage_key, bytes = EXCLUDED.bytes
                """,
                (job["org_id"], job_id, mtl.name, key, mtl.stat().st_size),
            )

        duration = int((time.monotonic() - started) * 1000)
        db.execute(
            """
            UPDATE jobs
            SET state='done', finished_at=now(), duration_ms=%s,
                objects_built=%s, result=%s, log=%s
            WHERE id=%s
            """,
            (duration, payload.get("objects", 0), json.dumps(payload), log, job_id),
        )

        # Said in the service log as well as returned, because the interesting
        # case is a build that succeeded and produced something wrong. A job
        # that reports "ok" and 49 objects looks identical to a good one until
        # somebody opens the file; one extra line here names the disagreement
        # between the model and its own stated dimensions at the moment it
        # happens.
        checks = payload.get("checks", [])
        errors = [c for c in checks if c.get("severity") == "error"]
        if errors:
            logger.error("job %s: geometry checks found %d error(s): %s",
                      job_id, len(errors),
                      "; ".join(c["detail"] for c in errors[:3]))

        return {
            "ok": True,
            "objects": payload.get("objects", 0),
            "formats": stored,
            "failed": failed,
            "skipped": payload.get("skipped", []),
            "checks": checks,
            "duration_ms": duration,
        }


def build_now(org_id: str, item_id: str, spec_row: dict, user_id: str,
              item_name: str) -> dict:
    """Create a job and run it immediately.

    Inline rather than queued, because a build of an ordinary object takes a
    couple of seconds - well within a request. The job row is written either
    way, so moving this onto the worker later changes where `run_job` is called
    from and nothing else.
    """
    job = create_job(org_id, item_id, spec_row, user_id)
    db.execute("UPDATE items SET status='building' WHERE id=%s", (item_id,))

    result = run_job(str(job["id"]), spec_row["body"], spec_row["revision"],
                     item_name)

    db.execute(
        "UPDATE items SET status=%s, updated_at=now() WHERE id=%s",
        ("complete" if result["ok"] else "failed", item_id),
    )

    audit.record(
        "build.finished" if result["ok"] else "build.failed",
        org_id=org_id, actor_user_id=user_id,
        target_type="job", target_id=str(job["id"]),
        metadata={
            "spec_revision": spec_row["revision"],
            "objects": result.get("objects"),
            "formats": result.get("formats"),
            "duration_ms": result.get("duration_ms"),
            "error": result.get("error"),
        },
    )

    result["job_id"] = str(job["id"])
    return result


# --------------------------------------------------------------------------- #
# Queue primitives - used by `forge worker` once builds move off the request
# --------------------------------------------------------------------------- #

def claim_next(worker_id: str) -> dict | None:
    """Take the oldest queued job.

    FOR UPDATE SKIP LOCKED is what makes several workers safe against each
    other with no lock service and no Redis: each transaction takes a different
    row instead of queueing behind the same one.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET state='running', claimed_by=%s, started_at=now(),
                            heartbeat_at=now(), attempts = attempts + 1
            WHERE id = (
                SELECT id FROM jobs
                WHERE state='queued'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *
            """,
            (worker_id,),
        )
        row = cur.fetchone()
        conn.commit()
        return row


def heartbeat(job_id: str) -> None:
    db.execute("UPDATE jobs SET heartbeat_at=now() WHERE id=%s", (job_id,))


def requeue_stalled(older_than_seconds: int = 120) -> int:
    """Return jobs whose worker died to the queue.

    Without this a killed container leaves work permanently 'running' and
    nothing ever picks it up again.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET state='queued', claimed_by=NULL
            WHERE state='running'
              AND heartbeat_at < now() - (%s || ' seconds')::interval
              AND attempts < 3
            """,
            (older_than_seconds,),
        )
        conn.commit()
        return cur.rowcount
