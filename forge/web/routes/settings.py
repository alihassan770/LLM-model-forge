"""Organisation settings.

The AI provider and its key are org-wide and admin-only: one configuration
applies to every member, which is what makes 'the whole team is using the same
model' true rather than a convention people have to remember.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.concurrency import run_in_threadpool

from ... import audit, orgs, settings_store
from ...ai import openrouter, providers
from ...ai.verify import verify
from ...auth import users
from ...storage.backend import S3Storage, forget_org, storage_summary
from ..deps import client_ip, current_org, require_role, require_user, verify_csrf

router = APIRouter(prefix="/settings")


def _save_effort(org_id: str, model, effort: str, user_id: str) -> None:
    """Store the thinking level, ignoring anything the model cannot do.

    The selector is hidden for a model without reasoning, so a posted value
    there is either a stale form or someone editing the request. Either way the
    right response is to leave the setting alone rather than record a level
    that will never be sent.
    """
    if not model.reasoning:
        return
    if effort in model.reasoning_efforts:
        settings_store.set_value(org_id, "ai.effort", effort, user_id)


def _page(request: Request, sess: dict, org: dict, **extra) -> HTMLResponse:
    from ..app import templates

    config = settings_store.ai_config(org["id"])

    # `cached()`, never `catalog()`: this helper renders, it does not fetch.
    # It is called from every branch of every route on this page, including
    # ones that have nothing to do with models, and a network call on the
    # event loop here would block the whole application. Warming the cache is
    # the GET route's job, done in a thread.
    or_models = openrouter.cached()

    context = {
        "session": sess,
        "org": org,
        "orgs": users.orgs_for_user(sess["user_id"]),
        "providers": providers.PROVIDERS,
        "config": config,
        "or_vendors": openrouter.vendors([m for m in or_models if m.vision]),
        "or_status": openrouter.status(),
        "effort_label": providers.EFFORT_LABEL,
        "effort_short": providers.EFFORT_SHORT,
        "storage": settings_store.storage_config(org["id"]),
        "deployment_storage": storage_summary(),
        "active": "settings",
        **extra,
    }
    return templates.TemplateResponse(
        request=request, name="settings_ai.html", context=context
    )


@router.get("", response_class=HTMLResponse)
async def settings_home(
    request: Request,
    sess: dict = Depends(require_user),
    org: dict = Depends(require_role("manager")),
):
    # The one place the catalogue is warmed, and in a thread: this is a
    # network call, and on the event loop it would hold up every other
    # request while it ran. It only actually fetches when the copy we have is
    # cold or stale, and it never raises.
    await run_in_threadpool(openrouter.catalog)
    return _page(request, sess, org)


@router.post("/ai")
async def save_ai(
    request: Request,
    provider: str = Form(...),
    model: str = Form(""),
    model_custom: str = Form(""),
    effort: str = Form(""),
    api_key: str = Form(""),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    # A manager configures the product; changing who may do so is admin-only
    # and lives on the members page.
    org: dict = Depends(require_role("manager")),
):
    """Save and verify in one action.

    The rule is simple and worth keeping: **nothing is saved unless it works.**
    A key that is stored but rejected looks configured on every screen and then
    fails on the first upload, which is the worst of both worlds - so when
    verification fails we save nothing and say why, leaving the form exactly as
    the user left it so they can correct the key.
    """
    verify_csrf(request, csrf_token)

    # A typed id wins over the dropdown. On OpenRouter the list is a cached
    # snapshot of someone else's catalogue, so there has to be a way to name a
    # model it does not happen to contain - one released this week, or one
    # private to this account. Everywhere else the field is not rendered.
    chosen_model = (model_custom or "").strip() or (model or "").strip()
    if not chosen_model:
        return _page(request, sess, org, error="Choose a model.")

    resolved = providers.resolve(provider, chosen_model)
    if resolved is None:
        return _page(
            request, sess, org,
            error="That provider and model combination is not supported.",
        )
    prov, mdl = resolved

    # Every stage of this pipeline starts by looking at a picture, so a
    # text-only model is not a choice this app can honour. The dropdown only
    # lists vision models; this catches a typed id and a stale form.
    if not mdl.vision:
        return _page(
            request, sess, org,
            error=f"{mdl.label} cannot read images, so it cannot be used for "
                  f"extraction. Choose a vision model.",
        )

    typed_key = (api_key or "").strip()
    stored_key = settings_store.get_secret(org["id"], f"ai.key.{prov.id}")
    key = typed_key or stored_key

    # Nothing to verify: let the settings through so the provider can be chosen
    # before its key exists, and say plainly what is still missing.
    if not key:
        settings_store.set_value(org["id"], "ai.provider", prov.id, sess["user_id"])
        settings_store.set_value(org["id"], "ai.model", mdl.id, sess["user_id"])
        _save_effort(org["id"], mdl, effort, sess["user_id"])
        audit.record(
            "settings.ai_updated",
            org_id=org["id"], actor_user_id=sess["user_id"],
            metadata={"provider": prov.id, "model": mdl.id, "key_replaced": False,
                      "verified": None},
            ip=client_ip(request),
        )
        return _page(
            request, sess, org,
            warn=f"Saved {mdl.label}. Add an API key for {prov.label} to start "
                 f"analysing uploads.",
        )

    # Fifteen seconds of network at worst, and on the event loop that is
    # fifteen seconds of nobody else being served.
    result = await run_in_threadpool(verify, prov, key, mdl.id)

    audit.record(
        "settings.ai_verified",
        org_id=org["id"], actor_user_id=sess["user_id"],
        metadata={"provider": prov.id, "model": mdl.id, "ok": result.ok},
        ip=client_ip(request),
    )

    if not result.ok:
        return _page(request, sess, org, test_result=result)

    settings_store.set_value(org["id"], "ai.provider", prov.id, sess["user_id"])
    settings_store.set_value(org["id"], "ai.model", mdl.id, sess["user_id"])
    _save_effort(org["id"], mdl, effort, sess["user_id"])
    if typed_key:
        settings_store.set_secret(
            org["id"], f"ai.key.{prov.id}", typed_key, sess["user_id"]
        )

    audit.record(
        "settings.ai_updated",
        org_id=org["id"], actor_user_id=sess["user_id"],
        # The key itself is never recorded - only that one was replaced.
        metadata={"provider": prov.id, "model": mdl.id,
                  "key_replaced": bool(typed_key), "verified": True},
        ip=client_ip(request),
    )

    return _page(request, sess, org, test_result=result, saved_model=mdl.label)


@router.post("/ai/models/refresh")
async def refresh_models(
    request: Request,
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(require_role("manager")),
):
    """Re-read OpenRouter's catalogue now, rather than waiting out the cache.

    The list is cached for hours because it barely changes; this exists for
    the day it does, when someone has come here specifically to use a model
    that was announced this morning.
    """
    verify_csrf(request, csrf_token)
    before = openrouter.status()["count"]
    models = await run_in_threadpool(openrouter.catalog, True)
    problem = openrouter.status()["error"]

    if problem:
        return _page(request, sess, org,
                     error=f"Could not reach OpenRouter ({problem}). The list "
                           f"below is the last one we fetched.")
    added = len(models) - before
    note = f"{len(models)} models available"
    note += f", {added} new." if added > 0 else "."
    return _page(request, sess, org, warn=f"Catalogue refreshed: {note}")


@router.post("/org")
async def rename_org(
    request: Request,
    org_name: str = Form(""),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(require_role("manager")),
):
    """Rename the workspace.

    On Settings rather than Account because it is not yours - it is the name
    everyone in the workspace sees, which is the same test every other thing
    on this page passes.
    """
    verify_csrf(request, csrf_token)
    try:
        stored = orgs.rename(org["id"], org_name)
    except orgs.OrgError as exc:
        return _page(request, sess, org, org_error=str(exc), org_value=org_name)

    audit.record(
        "org.renamed", org_id=org["id"], actor_user_id=sess["user_id"],
        target_type="org", target_id=org["id"],
        metadata={"from": org["name"], "to": stored}, ip=client_ip(request),
    )
    return _page(request, sess, {**org, "name": stored}, org_saved=stored)


@router.post("/storage")
async def save_storage(
    request: Request,
    bucket: str = Form(""),
    region: str = Form(""),
    prefix: str = Form("forge"),
    endpoint: str = Form(""),
    access_key_id: str = Form(""),
    secret_access_key: str = Form(""),
    csrf_token: str = Form(...),
    sess: dict = Depends(require_user),
    org: dict = Depends(require_role("manager")),
):
    """Point this workspace's files at its own bucket.

    Same rule as the API key: **nothing is saved unless it works.** The bucket
    is proved writable first, by writing and deleting one small object with the
    credentials given. A bucket that is stored but unusable would look
    configured on this page and then fail on the next upload, which is the
    worst of both outcomes.

    Clearing the bucket field puts the workspace back on the deployment-wide
    storage. Existing files are not moved: the keys already written point at
    the old bucket, so switching applies to what happens next.
    """
    verify_csrf(request, csrf_token)

    bucket = bucket.strip()
    existing = settings_store.storage_config(org["id"])

    if not bucket:
        for key in ("storage.bucket", "storage.region", "storage.prefix",
                    "storage.endpoint"):
            settings_store.clear(org["id"], key)
        settings_store.delete_secret(org["id"], "storage.access_key_id")
        settings_store.delete_secret(org["id"], "storage.secret_access_key")
        forget_org(org["id"])
        audit.record("storage.cleared", org_id=org["id"],
                     actor_user_id=sess["user_id"], target_type="org",
                     target_id=org["id"], metadata={}, ip=client_ip(request))
        return _page(request, sess, org,
                     storage_warn="Cleared. This workspace uses the server's "
                                  "own storage again.")

    # Typed values win; a blank key field means keep the stored one, so the
    # bucket or region can be corrected without re-entering the secret.
    key_id = access_key_id.strip() or existing["access_key"]
    secret = secret_access_key.strip() or existing["secret_key"]

    try:
        candidate = S3Storage(
            bucket=bucket, prefix=prefix.strip() or "forge",
            region=region.strip() or None,
            access_key=key_id or None, secret_key=secret or None,
            endpoint=endpoint.strip() or None,
        )
        problem = candidate.check()
    except Exception as exc:  # noqa: BLE001
        problem = f"{type(exc).__name__}: {exc}"[:300]

    audit.record("storage.verified", org_id=org["id"],
                 actor_user_id=sess["user_id"], target_type="org",
                 target_id=org["id"],
                 metadata={"bucket": bucket, "ok": not problem},
                 ip=client_ip(request))

    if problem:
        return _page(request, sess, org, storage_error=problem,
                     storage_typed={"bucket": bucket, "region": region,
                                    "prefix": prefix, "endpoint": endpoint})

    settings_store.set_value(org["id"], "storage.bucket", bucket, sess["user_id"])
    settings_store.set_value(org["id"], "storage.region", region.strip(), sess["user_id"])
    settings_store.set_value(org["id"], "storage.prefix", prefix.strip() or "forge",
                             sess["user_id"])
    settings_store.set_value(org["id"], "storage.endpoint", endpoint.strip(),
                             sess["user_id"])
    if access_key_id.strip():
        settings_store.set_secret(org["id"], "storage.access_key_id",
                                  access_key_id.strip(), sess["user_id"])
    if secret_access_key.strip():
        settings_store.set_secret(org["id"], "storage.secret_access_key",
                                  secret_access_key.strip(), sess["user_id"])

    # Without this the new bucket would not be used until the process
    # restarted, and the save button would look broken.
    forget_org(org["id"])

    audit.record("storage.updated", org_id=org["id"],
                 actor_user_id=sess["user_id"], target_type="org",
                 target_id=org["id"], metadata={"bucket": bucket},
                 ip=client_ip(request))

    return _page(request, sess, org,
                 storage_saved=f"{bucket} verified. New uploads and models go "
                               f"there from now on.")
