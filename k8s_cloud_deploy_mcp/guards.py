"""Input guards for names, label selectors, and immutable image digests."""

from __future__ import annotations

import re

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_DNS_SUBDOMAIN = re.compile(
    r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$"
)
_SELECTOR = re.compile(r"^[A-Za-z0-9._\-=,!/ ()]*$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_LATEST = re.compile(r"(?i)(^|[/:])latest($|@)")
_UNSAFE_IMAGE = re.compile(r"[\s'\"`;|&$<>\\]")
_RESOURCE_TYPES = {
    "deployment": "deployment",
    "deploy": "deployment",
    "deployments": "deployment",
    "deploymentconfig": "deploymentconfig",
    "deploymentconfigs": "deploymentconfig",
    "dc": "deploymentconfig",
}


def validate_namespace(namespace: str) -> str:
    value = (namespace or "").strip()
    if not _DNS_LABEL.fullmatch(value):
        raise ValueError("namespace must be a DNS-1123 label (lowercase letters, digits, and hyphens).")
    return value


def validate_resource_name(name: str) -> str:
    value = (name or "").strip()
    if not value or len(value) > 253 or not _DNS_SUBDOMAIN.fullmatch(value):
        raise ValueError("resource name must be a DNS-1123 subdomain.")
    return value


def validate_container_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        return ""
    if not _DNS_LABEL.fullmatch(value):
        raise ValueError("container_name must be a DNS-1123 label.")
    return value


def validate_label_selector(selector: str) -> str:
    value = selector or ""
    if len(value) > 512 or not _SELECTOR.fullmatch(value):
        raise ValueError("label_selector contains unsupported characters.")
    return value


def normalize_resource_type(resource_type: str) -> str:
    key = (resource_type or "").strip().lower()
    if key not in _RESOURCE_TYPES:
        raise ValueError("resource_type must be deployment or deploymentconfig.")
    return _RESOURCE_TYPES[key]


def validate_image_reference(image_tag: str) -> str:
    """Accept only an immutable sha256 digest. Reject the floating latest tag."""
    tag = (image_tag or "").strip()
    if not tag or len(tag) > 512:
        raise ValueError("image_tag must be a non-empty immutable digest reference up to 512 characters.")
    if _UNSAFE_IMAGE.search(tag):
        raise ValueError("image_tag contains unsupported characters.")
    if _LATEST.search(tag):
        raise ValueError(
            "Refusing mutable tag latest. Pass sha256:<64 hex> or registry/name@sha256:<64 hex>."
        )
    if tag.startswith("sha256:"):
        if not _DIGEST.fullmatch(tag):
            raise ValueError("Digest must be sha256: followed by 64 lowercase hex characters.")
        return tag
    if "@" in tag:
        base, digest = tag.rsplit("@", 1)
        if not base or "/" not in base or not _DIGEST.fullmatch(digest):
            raise ValueError("Full image digest must look like registry/name@sha256:<64 hex>.")
        if base.endswith(":"):
            raise ValueError("Invalid image reference.")
        return tag
    raise ValueError(
        "Immutable digest required. Pass sha256:<64 hex> to pin the current image, "
        "or registry/name@sha256:<64 hex>."
    )


def strip_tag(image: str) -> str:
    """Drop a tag or digest and keep a registry port, such as registry:5000/app."""
    without_digest = (image or "").split("@", 1)[0]
    slash = without_digest.rfind("/")
    tail = without_digest[slash + 1 :]
    colon = tail.rfind(":")
    if colon == -1:
        return without_digest
    return without_digest[: slash + 1 + colon]


def apply_digest(current: str, image_tag: str) -> str:
    verified = validate_image_reference(image_tag)
    if verified.startswith("sha256:"):
        base = strip_tag((current or "").strip())
        if not base or "://" in base:
            raise ValueError("Current image is empty or unusable. Pass registry/name@sha256:<64 hex>.")
        return f"{base}@{verified}"
    return verified


def clamp_tail(tail_lines: int) -> int:
    try:
        value = int(tail_lines)
    except (TypeError, ValueError) as exc:
        raise ValueError("tail_lines must be an integer.") from exc
    if value < 1 or value > 2000:
        raise ValueError("tail_lines must be between 1 and 2000.")
    return value


def clamp_timeout(timeout_seconds: int) -> int:
    try:
        value = int(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be an integer.") from exc
    if value < 0 or value > 180:
        raise ValueError("timeout_seconds must be between 0 and 180.")
    return value


def validate_revision(revision: int) -> int:
    try:
        value = int(revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("revision must be an integer.") from exc
    if value < 0:
        raise ValueError("revision must be 0 for the previous revision, or a positive revision number.")
    return value


def select_revision(revisions: list[int], requested: int) -> int:
    requested = validate_revision(requested)
    known = sorted({int(item) for item in revisions if int(item) > 0})
    if not known:
        raise ValueError("No rollout history was found.")
    if requested == 0:
        if len(known) < 2:
            raise ValueError(f"Only revision {known[0]} exists. There is no previous revision to restore.")
        return known[-2]
    if requested not in known:
        raise ValueError(f"Revision {requested} was not found. Known revisions: {known}.")
    if requested == known[-1]:
        raise ValueError(
            f"Revision {requested} is already the latest. "
            f"Pass revision=0 or an older revision. Known revisions: {known}."
        )
    return requested


def confirmation_preview(message_plan: dict) -> dict:
    return {
        "ok": False,
        "access": "mutating",
        "applied": False,
        "confirmation_required": True,
        "message": (
            "This change was not applied. Show the plan to the user and call again with "
            "confirm=true only after they explicitly approve it."
        ),
        "plan": message_plan,
    }
