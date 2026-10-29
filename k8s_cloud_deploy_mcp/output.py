"""JSON rendering for tool results. API error bodies are not returned."""

from __future__ import annotations

import json

from k8s_cloud_deploy_mcp.models import MutationReport, ReadOnlyReport


def dump(payload: dict) -> str:
    access = payload.get("access")
    if access == "read-only":
        normalized = ReadOnlyReport.model_validate(payload).model_dump(mode="json")
    elif access == "mutating":
        normalized = MutationReport.model_validate(payload).model_dump(mode="json")
    else:
        raise ValueError("Tool report is missing an access classification.")
    return json.dumps(normalized, indent=2, default=str)


def fail(tool: str, exc: Exception, access: str = "read-only") -> str:
    status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None)
    if exc.__class__.__name__ == "ApiException" and status and reason:
        message = f"Kubernetes API {status}: {reason}"
    else:
        message = str(exc)
    payload = {"ok": False, "access": access, "tool": tool, "error": message}
    if access == "mutating":
        payload["applied"] = False
        payload["confirmation_required"] = False
    return dump(payload)
