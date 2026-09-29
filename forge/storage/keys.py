"""Every storage path in the system, in one place.

org_id leads every key. That makes per-tenant lifecycle rules, cost attribution
and delete-on-request trivial, and it means a misrouted read lands in a prefix
that simply does not exist rather than in another customer's data.
"""

from __future__ import annotations


def upload(org_id: str, asset_id: str, ext: str) -> str:
    return f"{org_id}/uploads/{asset_id}.{ext}"


def normalized(org_id: str, asset_id: str) -> str:
    return f"{org_id}/uploads/{asset_id}/normalized.png"


def pdf_page(org_id: str, asset_id: str, page_no: int, dpi: int = 300) -> str:
    return f"{org_id}/uploads/{asset_id}/pages/{page_no:04d}@{dpi}dpi.png"


def crop(org_id: str, asset_id: str, crop_id: str) -> str:
    return f"{org_id}/uploads/{asset_id}/crops/{crop_id}.png"


def spec(org_id: str, job_id: str) -> str:
    return f"{org_id}/jobs/{job_id}/spec.json"


def export(org_id: str, job_id: str, name: str, ext: str) -> str:
    return f"{org_id}/jobs/{job_id}/exports/{name}.{ext}"


def preview(org_id: str, job_id: str, view: str) -> str:
    return f"{org_id}/jobs/{job_id}/preview/{view}.png"
