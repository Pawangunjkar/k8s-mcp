"""Cloud provider context and OpenShift API-group detection."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, field_validator

ALLOWED_PROVIDERS = frozenset({"aws_eks", "azure_aks", "gcp_gke", "openshift", "generic_k8s"})


class CloudContext(BaseModel):
    """Authentication and targeting inputs. Tokens and kubeconfig contents are not stored."""

    model_config = ConfigDict(frozen=True)

    configured_provider: str = ""
    aws_region: str = ""
    azure_subscription_id: str = ""
    gcp_project: str = ""
    k8s_context: str = ""
    kubeconfig: str = ""
    namespace: str = "default"

    @field_validator("configured_provider")
    @classmethod
    def provider_must_be_known(cls, value: str) -> str:
        if value and value not in ALLOWED_PROVIDERS:
            allowed = ", ".join(sorted(ALLOWED_PROVIDERS))
            raise ValueError(f"CLOUD_PROVIDER must be one of: {allowed}.")
        return value


def load_cloud_context(kubeconfig: str = "", context: str = "", namespace: str = "") -> CloudContext:
    """Load CLOUD_PROVIDER, AWS_REGION, AZURE_SUBSCRIPTION_ID, GCP_PROJECT, and K8S_CONTEXT.

    Explicit non-empty arguments override the matching environment variables.
    """
    configured = os.environ.get("CLOUD_PROVIDER", "").strip().lower()
    resolved_namespace = (namespace or os.environ.get("K8S_NAMESPACE") or "default").strip() or "default"
    return CloudContext(
        configured_provider=configured,
        aws_region=os.environ.get("AWS_REGION", "").strip(),
        azure_subscription_id=os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip(),
        gcp_project=os.environ.get("GCP_PROJECT", "").strip(),
        k8s_context=(context or os.environ.get("K8S_CONTEXT", "")).strip(),
        kubeconfig=(kubeconfig or os.environ.get("KUBECONFIG", "")).strip(),
        namespace=resolved_namespace,
    )


def flatten_api_groups(groups: Iterable[tuple[str, Iterable[str]]]) -> set[str]:
    """Store both the API group and group/version pairs such as apps.openshift.io/v1."""
    found: set[str] = set()
    for name, versions in groups:
        if not name:
            continue
        found.add(name)
        for version in versions:
            if version:
                found.add(f"{name}/{version}")
    return found


def api_is_openshift(api_groups: Iterable[str]) -> bool:
    """True when apps.openshift.io/v1 or route.openshift.io is advertised."""
    for item in api_groups:
        if item in {"apps.openshift.io", "route.openshift.io"}:
            return True
        if item.startswith("apps.openshift.io/") or item.startswith("route.openshift.io/"):
            return True
    return False


def _has_token(blob: str, token: str) -> bool:
    return re.search(rf"(^|[^a-z0-9]){re.escape(token)}([^a-z0-9]|$)", blob) is not None


def infrastructure_of(context: str, server: str, provider_ids: Iterable[str]) -> str:
    """Classify node provider IDs and API hostnames as aws, azure, gcp, or unknown."""
    ids = [item.lower() for item in provider_ids if item]
    blob = " ".join([context or "", server or "", *ids]).lower()
    if any(item.startswith("aws://") for item in ids) or "amazonaws.com" in blob or _has_token(blob, "eks"):
        return "aws"
    if any(item.startswith("azure://") for item in ids) or "azmk8s.io" in blob or _has_token(blob, "aks"):
        return "azure"
    if any(item.startswith("gce://") for item in ids) or "googleapis.com" in blob or _has_token(blob, "gke"):
        return "gcp"
    return "unknown"


def resolve_provider(configured: str, is_openshift: bool, infrastructure: str) -> tuple[str, str]:
    """Environment selection wins. Otherwise OpenShift API groups win, then cloud metadata."""
    if configured:
        if configured not in ALLOWED_PROVIDERS:
            allowed = ", ".join(sorted(ALLOWED_PROVIDERS))
            raise ValueError(f"CLOUD_PROVIDER must be one of: {allowed}.")
        return configured, "environment"
    if is_openshift:
        return "openshift", "api-groups"
    if infrastructure == "aws":
        return "aws_eks", "cluster-metadata"
    if infrastructure == "azure":
        return "azure_aks", "cluster-metadata"
    if infrastructure == "gcp":
        return "gcp_gke", "cluster-metadata"
    return "generic_k8s", "default"


def auth_gaps(provider: str, aws_region: str, azure_subscription_id: str, gcp_project: str) -> list[str]:
    gaps: list[str] = []
    if provider == "aws_eks" and not aws_region:
        gaps.append("AWS_REGION is unset")
    if provider == "azure_aks" and not azure_subscription_id:
        gaps.append("AZURE_SUBSCRIPTION_ID is unset")
    if provider == "gcp_gke" and not gcp_project:
        gaps.append("GCP_PROJECT is unset")
    return gaps


def safe_version_url(host: str) -> str:
    """Build an API /version URL and drop userinfo, path, and query from the kube host."""
    parts = urlsplit((host or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("API server host must be an http or https URL.")
    hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{hostname}:{parts.port}" if parts.port else hostname
    return urlunsplit((parts.scheme, netloc, "/version", "", ""))
