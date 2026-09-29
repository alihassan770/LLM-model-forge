"""Projects - the grouping level.

Listing, creating and totalling projects, and creating the items inside them.
Everything that happens *to* an item lives in routes/items.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ... import audit, items, projects
from ...auth import users
from ...bom import service as bom_service
from ..deps import client_ip, current_org, require_user, verify_csrf

router = APIRouter()


def _ctx(sess: dict, org: dict, **extra) -> dict:
    return {
        "session": sess,
        "org": org,
        "orgs": users.orgs_for_user(sess["user_id"]),
        "active": "projects",
        **extra,
    }


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #

@router.get("/projects", response_class=HTMLResponse)
async def index(
    request: Request,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    from ..app import templates

    return templates.TemplateResponse(
        request=request, name="projects.html",
        context=_ctx(sess, org, projects=projects.listing(org["id"])),
    )


@router.get("/projects/new", response_class=HTMLResponse)
async def new_project(
    request: Request,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    from ..app import templates

    return templates.TemplateResponse(
        request=request, name="project_new.html", context=_ctx(sess, org),
    )


@router.post("/projects")
async def create_project(
    request: Request,
    name: str = Form(""),
    client: str = Form(""),
    notes: str = Form(""),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    from ..app import templates

    verify_csrf(request, csrf_token)
    try:
        project = projects.create(org["id"], sess["user_id"], name, client, notes)
    except projects.ProjectError as exc:
        return templates.TemplateResponse(
            request=request, name="project_new.html", status_code=400,
            context=_ctx(sess, org, error=str(exc), name=name,
                         client=client, notes=notes),
        )

    audit.record(
        "project.created", org_id=org["id"], actor_user_id=sess["user_id"],
        target_type="project", target_id=project["id"],
        metadata={"name": project["name"]}, ip=client_ip(request),
    )
    # Straight on to adding the first item: an empty project is not yet useful,
    # and adding something to it is what the person came to do.
    return RedirectResponse(f"/projects/{project['id']}/items/new",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def detail(
    request: Request,
    project_id: str,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    from ..app import templates

    project = projects.get(org["id"], project_id)
    if project is None:
        return templates.TemplateResponse(
            request=request, name="error.html", status_code=404,
            context={"code": 404, "message": "That project does not exist."},
        )

    return templates.TemplateResponse(
        request=request, name="project_detail.html",
        context=_ctx(sess, org, project=project,
                     items=items.for_project(org["id"], project_id),
                     status_label=items.STATUS_LABEL),
    )


@router.post("/projects/{project_id}/archive")
async def archive_project(
    request: Request,
    project_id: str,
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    verify_csrf(request, csrf_token)
    project = projects.get(org["id"], project_id)
    if project is not None:
        projects.archive(org["id"], project_id)
        audit.record(
            "project.archived", org_id=org["id"], actor_user_id=sess["user_id"],
            target_type="project", target_id=project_id,
            metadata={"name": project["name"]}, ip=client_ip(request),
        )
    return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Items, created within a project
# --------------------------------------------------------------------------- #

@router.get("/projects/{project_id}/items/new", response_class=HTMLResponse)
async def new_item(
    request: Request,
    project_id: str,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    from ..app import templates

    project = projects.get(org["id"], project_id)
    if project is None:
        return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request, name="item_new.html",
        context=_ctx(sess, org, project=project),
    )


@router.post("/projects/{project_id}/items")
async def create_item(
    request: Request,
    project_id: str,
    name: str = Form(""),
    category: str = Form(""),
    notes: str = Form(""),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    from ..app import templates

    verify_csrf(request, csrf_token)

    project = projects.get(org["id"], project_id)
    if project is None:
        return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)

    try:
        item = items.create(org["id"], project_id, sess["user_id"], name,
                            category, notes)
    except items.ItemError as exc:
        return templates.TemplateResponse(
            request=request, name="item_new.html", status_code=400,
            context=_ctx(sess, org, project=project, error=str(exc),
                         name=name, category=category, notes=notes),
        )

    audit.record(
        "item.created", org_id=org["id"], actor_user_id=sess["user_id"],
        target_type="item", target_id=item["id"],
        metadata={"name": item["name"], "project": project["name"]},
        ip=client_ip(request),
    )
    return RedirectResponse(f"/items/{item['id']}",
                            status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Project-wide bill of materials
# --------------------------------------------------------------------------- #

@router.get("/projects/{project_id}/materials.csv")
async def project_materials_csv(
    project_id: str,
    sess: dict = Depends(require_user),
    org: dict = Depends(current_org),
):
    """Every item's materials in one file.

    A fit-out is ordered in one go, so the useful export is the whole project
    with an item column - not one CSV per chair.
    """
    import csv
    import io

    project = projects.get(org["id"], project_id)
    if project is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Item", "Category", "Material", "Description", "Quantity",
                     "Unit", "Sizes"])

    for row in items.for_project(org["id"], project_id):
        bom = bom_service.latest(str(row["id"]))
        if bom is None:
            writer.writerow([row["name"], row["category"], "",
                             "no materials calculated yet", "", "", ""])
            continue
        for line in bom["lines"]:
            detail = line["detail"] or {}
            sizes = ""
            if detail.get("pieces"):
                sizes = (f"cut {', '.join(str(x) + 'mm' for x in detail['pieces'])} "
                         f"from {detail.get('stock_length_mm', 0):.0f}mm")
            elif detail.get("sheet_size_mm"):
                sizes = f"sheets {detail['sheet_size_mm']}"
            writer.writerow([row["name"], line["category"], line["material_code"],
                             line["description"], line["quantity"], line["unit"],
                             sizes])

    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project["name"])
    return Response(
        content=out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{safe}-materials.csv"'},
    )
