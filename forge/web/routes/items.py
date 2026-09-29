"""Items: upload source files, extract, answer, build, cost.

Everything that happens to one physical object. The project router above this
only groups items; all the work is here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.concurrency import run_in_threadpool

from ... import audit, db, items, projects
from ...spec import merge, schema as spec_schema, validate
from ...vision import extract
from ...auth import users
from ...ingest import crop as crop_mod, detect, pdf as pdf_reader
from ...bom import service as bom_service
from ...jobs import runner
from ...storage.backend import for_org
from ..deps import client_ip, current_org, require_user, verify_csrf

router = APIRouter()

# Read in bounded chunks rather than one .read(). An UploadFile is backed by a
# spooled temp file, so this keeps a hostile Content-Length from turning into
# an allocation the size of the claim.
CHUNK = 1024 * 1024
HARD_CAP = 40 * 1024 * 1024


async def _read_capped(upload: UploadFile) -> bytes:
    buf = bytearray()
    while chunk := await upload.read(CHUNK):
        buf.extend(chunk)
        if len(buf) > HARD_CAP:
            raise detect.UnsupportedFile(
                f"That file is larger than {HARD_CAP // 1024 // 1024} MB."
            )
    return bytes(buf)


def _ctx(request: Request, sess: dict, org: dict, **extra) -> dict:
    return {
        "session": sess,
        "org": org,
        "orgs": users.orgs_for_user(sess["user_id"]),
        "active": "projects",
        **extra,
    }


def _assets_for_display(org_id: str, item_id: str) -> list[dict]:
    """The item's files, each with a URL and whether its bytes are still there.

    `present` is the interesting one. On a container platform the filesystem is
    wiped on every deploy, so local storage loses the bytes while the database
    keeps the name, the size and the pixel dimensions. The page then renders a
    fully detailed card around a broken image icon, which reads as "this
    application is failing" rather than "this file is gone" - and telling those
    two apart is the whole of the fix. There is nothing else on the page that
    can say it.

    Checked only for local storage, where it is a stat. Against S3 it would be
    a network round trip per asset on every page load, and S3 is also the
    configuration in which this does not happen.
    """
    rows = items.assets(item_id)
    storage = for_org(org_id)
    for row in rows:
        row["url"] = storage.url(row["storage_key"])
        row["present"] = True
        if storage.name == "local":
            try:
                row["present"] = storage.exists(row["storage_key"])
            except Exception:  # noqa: BLE001
                row["present"] = False
    return rows


@router.get("/items/{item_id}", response_class=HTMLResponse)
async def detail(
    request: Request,
    item_id: str,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
    notice: str | None = None,
):
    from ..app import templates

    item = items.get(org["id"], item_id)
    if item is None:
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=404,
            context={"code": 404, "message": "That item does not exist."},
        )

    rows = _assets_for_display(org["id"], item_id)

    spec_row = items.latest_spec(item_id)
    report = validate.check(spec_row["body"]) if spec_row else None
    build_jobs = items.jobs_for(item_id)
    bom = bom_service.latest(item_id)

    return templates.TemplateResponse(
        request=request, name="item_detail.html",
        context=_ctx(request, sess, org,
                     item=item, assets=rows,
                     spec_row=spec_row,
                     spec=spec_row["body"] if spec_row else None,
                     report=report,
                     jobs=build_jobs,
                     bom=bom,
                     category_label=bom_service.CATEGORY_LABEL,
                     blender=runner.find_blender(),
                     units=spec_schema.UNITS,
                     band=spec_schema.confidence_band,
                     accepted=detect.ACCEPTED, labels=detect.LABELS,
                     role_label=items.ROLE_LABEL,
                     status_label=items.STATUS_LABEL,
                     human_size=items.human_size,
                     format_label=items.FORMAT_LABEL,
                     format_use=items.FORMAT_USE,
                     notice=notice),
    )


@router.post("/items/{item_id}/answers")
async def answers(
    request: Request,
    item_id: str,
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """Record every answer on the page in one revision.

    One form, one save. Answering questions one at a time meant a page reload
    per answer, and anything typed into the other fields was thrown away - which is exactly the moment someone gives up on a ten-question form.
    """
    verify_csrf(request, csrf_token)

    item = items.get(org["id"], item_id)
    spec_row = items.latest_spec(item_id)
    if item is None or spec_row is None:
        return RedirectResponse(f"/items/{item_id}",
                                status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    spec = spec_row["body"]
    answered, skipped = 0, 0

    # Iterate the spec's own questions rather than whatever the form contained:
    # the spec is the authority on what may be answered, so a crafted field
    # cannot introduce a dimension nobody asked for.
    for index, question in enumerate(list(spec.get("missing_dimensions", []))):
        raw = (form.get(f"value_{index}") or "").strip()
        unit = form.get(f"unit_{index}") or question.get("unit", "mm")
        if unit not in spec_schema.UNITS:
            unit = "mm"

        if not raw:
            continue

        if raw.lower() == "skip":
            if not question.get("blocking"):
                spec = merge.apply_default(spec, question["part_id"],
                                           question["dimension"])
                skipped += 1
            continue

        try:
            number = float(raw)
        except ValueError:
            continue
        if number <= 0:
            continue

        spec = merge.apply_answer(spec, question["part_id"],
                                  question["dimension"], number, unit)
        answered += 1

    if answered or skipped:
        merge.save_revision(org["id"], item_id, spec, sess["user_id"],
                            spec_row["revision"], spec_row["provider"],
                            spec_row["model"])
        report = validate.check(spec)
        db.execute(
            "UPDATE items SET status = %s, updated_at = now() WHERE id = %s",
            ("ready" if report.ok else "questions", item_id),
        )
        audit.record(
            "question.answered", org_id=org["id"], actor_user_id=sess["user_id"],
            target_type="item", target_id=item_id,
            metadata={"answered": answered, "skipped": skipped},
            ip=client_ip(request),
        )

    return RedirectResponse(f"/items/{item_id}",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/items/{item_id}/build")
async def build(
    request: Request,
    item_id: str,
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """Generate the 3D model from the current specification."""
    verify_csrf(request, csrf_token)

    item = items.get(org["id"], item_id)
    spec_row = items.latest_spec(item_id)
    if item is None or spec_row is None:
        return RedirectResponse(f"/items/{item_id}",
                                status_code=status.HTTP_303_SEE_OTHER)

    # The gate runs here as well as in the template. A disabled button is a
    # hint, not a guard - this endpoint can be reached directly.
    report = validate.check(spec_row["body"])
    if not report.ok:
        return RedirectResponse(f"/items/{item_id}#gate",
                                status_code=status.HTTP_303_SEE_OTHER)

    # In a thread for the same reason as `analyse`: Blender runs for minutes
    # and this is an async route, so calling it inline would block the event
    # loop and take every other page down with it.
    await run_in_threadpool(runner.build_now, org["id"], item_id, spec_row,
                            sess["user_id"], item["name"])

    return RedirectResponse(f"/items/{item_id}#downloads",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/items/{item_id}/bom")
async def generate_bom(
    request: Request,
    item_id: str,
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """Compute the bill of materials for the current specification."""
    verify_csrf(request, csrf_token)

    item = items.get(org["id"], item_id)
    spec_row = items.latest_spec(item_id)
    if item is None or spec_row is None:
        return RedirectResponse(f"/items/{item_id}",
                                status_code=status.HTTP_303_SEE_OTHER)

    jobs = items.jobs_for(item_id, limit=1)
    job_id = str(jobs[0]["id"]) if jobs and jobs[0]["state"] == "done" else None

    bom_service.build_bill(org["id"], item_id, spec_row, sess["user_id"],
                           job_id=job_id)

    return RedirectResponse(f"/items/{item_id}#bom",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.get("/items/{item_id}/bom.csv")
async def bom_csv(
    item_id: str,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    item = items.get(org["id"], item_id)
    bom = bom_service.latest(item_id)
    if item is None or bom is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in item["name"])
    return Response(
        content=bom_service.to_csv(bom),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{safe}-materials.csv"'},
    )


@router.get("/items/{item_id}/download/{artifact_id}")
async def download(
    item_id: str,
    artifact_id: str,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """Hand over one built file."""
    row = db.fetch_one(
        """
        SELECT a.*, j.item_id
        FROM artifacts a JOIN jobs j ON j.id = a.job_id
        WHERE a.id = %s AND a.org_id = %s AND j.item_id = %s
        """,
        (artifact_id, org["id"], item_id),
    )
    if row is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    storage = for_org(org["id"])
    if not storage.exists(row["storage_key"]):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    is_preview = row["format"].startswith(runner.PREVIEW_PREFIX)
    return Response(
        content=storage.get(row["storage_key"]),
        media_type="image/png" if is_preview else runner.MEDIA_TYPES.get(
            row["format"], "application/octet-stream"),
        headers={
            # Previews render in an <img>; deliverables download. Marking a
            # preview as an attachment would make the gallery download four
            # files instead of showing them.
            "Content-Disposition":
                (f'inline; filename="{row["filename"]}"' if is_preview
                 else f'attachment; filename="{row["filename"]}"'),
            "Cache-Control": "private, max-age=600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/items/{item_id}/model.glb")
async def model_glb(
    item_id: str,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """The newest built model, served inline for the in-page viewer.

    Separate from the download route rather than adding a flag to it, because
    the two want opposite headers: a download must be an attachment or the
    browser renders it, and a viewer source must be inline or the browser
    saves it. One route with a query parameter deciding which would be a
    smaller diff and a worse route.

    Always the newest build, so the URL is stable while the artifact behind it
    changes. That means the viewer does not need to know an artifact id, and a
    rebuild shows in the viewer on the next load.
    """
    row = db.fetch_one(
        """
        SELECT a.storage_key, a.filename
        FROM artifacts a
        JOIN jobs j ON j.id = a.job_id
        WHERE j.item_id = %s AND a.org_id = %s AND a.format = 'glb'
          AND j.state = 'done'
        ORDER BY a.created_at DESC
        LIMIT 1
        """,
        (item_id, org["id"]),
    )
    if row is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    storage = for_org(org["id"])
    if not storage.exists(row["storage_key"]):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    return Response(
        content=storage.get(row["storage_key"]),
        media_type="model/gltf-binary",
        headers={
            "Content-Disposition": f'inline; filename="{row["filename"]}"',
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _region_source(org_id: str, item_id: str, spec_row: dict,
                   region: dict) -> bytes | None:
    """The image a crop region was measured against.

    `region.image_index` indexes the spec's recorded `sources` list, which for
    a PDF names a rendered page rather than the uploaded file. Falling back to
    "the first attached photo" would silently crop the wrong picture whenever
    more than one file is attached, which is worse than showing none: the
    person answering would be looking at the wrong leg.
    """
    storage = for_org(org_id)
    sources = spec_row.get("sources") or []

    try:
        index = int(region.get("image_index", 0))
    except (TypeError, ValueError):
        index = 0

    if 0 <= index < len(sources):
        source = sources[index]
        if source.get("storage_key"):
            try:
                return storage.get(source["storage_key"])
            except Exception:
                pass
        # The page was not kept (an older spec, or the write failed). Render it
        # again from the PDF rather than showing the wrong image.
        if source.get("kind") == "pdf_page" and source.get("page"):
            asset = next((a for a in items.assets(item_id)
                          if str(a["id"]) == str(source.get("asset_id"))), None)
            if asset is not None:
                try:
                    return pdf_reader.render_page(
                        storage.get(asset["storage_key"]), int(source["page"]))
                except Exception:
                    return None
        return None

    # Specs written before sources were recorded have no list. One attached
    # image is unambiguous; several are not, so guessing stops here.
    images = [a for a in items.assets(item_id)
              if a["detected_mime"] != "application/pdf"]
    if len(images) == 1:
        try:
            return storage.get(images[0]["storage_key"])
        except Exception:
            return None
    return None


@router.get("/items/{item_id}/crop/{index}")
async def question_crop(
    item_id: str,
    index: int,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """The slice of the source image a question is about."""
    item = items.get(org["id"], item_id)
    spec_row = items.latest_spec(item_id)
    if item is None or spec_row is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    questions = spec_row["body"].get("missing_dimensions", [])
    if index < 0 or index >= len(questions):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    region = questions[index].get("region") or {}
    if not region.get("found"):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    data = _region_source(org["id"], item_id, spec_row, region)
    if data is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    cropped = crop_mod.crop_region(
        data, region.get("x", 0), region.get("y", 0),
        region.get("w", 1), region.get("h", 1),
    )
    if cropped is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    return Response(content=cropped, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=600",
                             "X-Content-Type-Options": "nosniff"})


@router.post("/items/{item_id}/analyse")
async def analyse(
    request: Request,
    item_id: str,
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """Read the attached files into a part specification.

    Still synchronous from the caller's point of view - the request carries
    the extraction and returns the finished page - but it runs in a worker
    thread, which is not the same thing and matters a great deal.

    `extract.run` blocks on the model for minutes; the measured runs here are
    two to four. An `async def` route body runs *on the event loop*, so doing
    that here froze the entire application for the duration: every other
    page, for every user, including the settings page you would go to in
    order to pick a faster model. The symptom was indistinguishable from the
    server being down.

    A thread keeps the blocking where it belongs. This still occupies the
    request for minutes, which is its own problem - it moves to the job queue
    when PDF rasterising and Blender arrive, because those cannot be carried
    by a request at all.
    """
    from ..app import templates

    verify_csrf(request, csrf_token)

    item = items.get(org["id"], item_id)
    if item is None:
        return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)

    result = await run_in_threadpool(extract.run, org["id"], item_id,
                                     sess["user_id"])

    if result["ok"]:
        items.sync_questions(org["id"], item_id, result["spec_id"],
                                result["spec"])

    rows = _assets_for_display(org["id"], item_id)

    spec_row = items.latest_spec(item_id)
    return templates.TemplateResponse(
        request=request, name="item_detail.html",
        context=_ctx(request, sess, org,
                     item=items.get(org["id"], item_id),
                     assets=rows,
                     spec_row=spec_row,
                     spec=spec_row["body"] if spec_row else None,
                     report=validate.check(spec_row["body"]) if spec_row else None,
                     jobs=items.jobs_for(item_id),
                     bom=bom_service.latest(item_id),
                     category_label=bom_service.CATEGORY_LABEL,
                     blender=runner.find_blender(),
                     units=spec_schema.UNITS,
                     band=spec_schema.confidence_band,
                     accepted=detect.ACCEPTED, labels=detect.LABELS,
                     role_label=items.ROLE_LABEL,
                     status_label=items.STATUS_LABEL,
                     human_size=items.human_size,
                     format_label=items.FORMAT_LABEL,
                     format_use=items.FORMAT_USE,
                     extract_error=None if result["ok"] else result["error"],
                     extract_issues=result.get("issues"),
                     usage=result.get("usage"),
                     notice="Analysis complete." if result["ok"] else None),
    )


@router.post("/items/{item_id}/upload")
async def upload(
    request: Request,
    item_id: str,
    role: str = Form("primary"),
    csrf_token: str = Form(...),
    files: list[UploadFile] = File(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    from ..app import templates

    verify_csrf(request, csrf_token)

    item = items.get(org["id"], item_id)
    if item is None:
        return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)

    accepted, rejected = [], []

    # Named `sent` rather than `item`: the item row is already bound above, and
    # a loop variable called `item` shadowed it. The audit line below then read
    # `item["name"]` off an UploadFile and every successful upload ended in a
    # 500. Only successful ones, because a rejected file continues before that
    # line, which is why re-uploading the same file appeared to fix it.
    for sent in files:
        if not sent.filename:
            continue
        try:
            data = await _read_capped(sent)
            asset = items.add_asset(
                org["id"], item_id, sess["user_id"],
                sent.filename, sent.content_type or "", data, role,
            )
        except (detect.UnsupportedFile, items.ItemError) as exc:
            rejected.append((sent.filename, str(exc)))
            continue

        accepted.append(asset)
        audit.record(
            "asset.uploaded", org_id=org["id"], actor_user_id=sess["user_id"],
            target_type="asset", target_id=asset["id"],
            metadata={
                "item": item["name"],
                "file": asset["original_name"],
                "type": detect.LABELS[asset["detected_mime"]],
                "bytes": asset["bytes"],
                # Recorded because it is how a mislabelled or renamed file
                # shows up later without anyone having to go looking.
                "declared_mime": asset["declared_mime"],
            },
            ip=client_ip(request),
        )

    rows = _assets_for_display(org["id"], item_id)

    notice = None
    if accepted:
        notice = (f"{len(accepted)} file{'' if len(accepted) == 1 else 's'} "
                  f"added.")

    return templates.TemplateResponse(
        request=request, name="item_detail.html",
        context=_ctx(request, sess, org,
                     item=items.get(org["id"], item_id),
                     assets=rows, accepted=detect.ACCEPTED, labels=detect.LABELS,
                     role_label=items.ROLE_LABEL,
                     status_label=items.STATUS_LABEL,
                     human_size=items.human_size,
                     format_label=items.FORMAT_LABEL,
                     format_use=items.FORMAT_USE,
                     notice=notice, rejected=rejected),
    )


@router.post("/items/{item_id}/assets/{asset_id}/delete")
async def remove_asset(
    request: Request,
    item_id: str,
    asset_id: str,
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    verify_csrf(request, csrf_token)
    row = items.delete_asset(org["id"], asset_id)
    if row is not None:
        audit.record(
            "asset.removed", org_id=org["id"], actor_user_id=sess["user_id"],
            target_type="asset", target_id=asset_id,
            metadata={"file": row["original_name"]}, ip=client_ip(request),
        )
    return RedirectResponse(f"/items/{item_id}",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/items/{item_id}/archive")
async def archive(
    request: Request,
    item_id: str,
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    verify_csrf(request, csrf_token)
    item = items.get(org["id"], item_id)
    if item is not None:
        items.archive(org["id"], item_id)
        audit.record(
            "item.archived", org_id=org["id"], actor_user_id=sess["user_id"],
            target_type="item", target_id=item_id,
            metadata={"name": item["name"]}, ip=client_ip(request),
        )
    return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Local-storage file serving
# --------------------------------------------------------------------------- #

@router.get("/files/{key:path}")
async def serve_file(
    key: str,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """Serve a locally-stored object.

    Only used by the development backend - with S3 the browser is handed a
    presigned URL and the bytes never pass through here. The tenant check is
    the important part: keys begin with the owning org's id, so a key belonging
    to another tenant is refused rather than read.
    """
    if not key.startswith(f"{org['id']}/"):
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    storage = for_org(org["id"])
    if not storage.exists(key):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    ext = key.rsplit(".", 1)[-1].lower()
    media = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "pdf": "application/pdf",
    }.get(ext, "application/octet-stream")

    return Response(
        content=storage.get(key),
        media_type=media,
        headers={
            "Cache-Control": "private, max-age=300",
            # Never let an uploaded file be interpreted as something else.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'",
        },
    )
