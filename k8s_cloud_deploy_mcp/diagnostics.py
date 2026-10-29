"""Pure parsers for pod state, rollout progress, and workload history."""

from __future__ import annotations

PROBLEM_STATES = frozenset(
    {
        "CrashLoopBackOff",
        "OOMKilled",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "InvalidImageName",
        "RunContainerError",
        "Error",
        "StartError",
        "CreateContainerError",
    }
)


def _field(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def describe_container(status) -> dict:
    """Report Running, CrashLoopBackOff, OOMKilled, ImagePullBackOff, and restart counts."""
    state = _field(status, "state")
    waiting = _field(state, "waiting") if state is not None else None
    running = _field(state, "running") if state is not None else None
    terminated = _field(state, "terminated") if state is not None else None
    last_state = _field(status, "last_state")
    last_terminated = _field(last_state, "terminated") if last_state is not None else None
    last_reason = _field(last_terminated, "reason") or ""
    exit_code = None
    if running is not None:
        current = "Running"
        message = ""
    elif waiting is not None:
        current = _field(waiting, "reason") or "Waiting"
        message = _field(waiting, "message") or ""
    elif terminated is not None:
        current = _field(terminated, "reason") or "Terminated"
        message = _field(terminated, "message") or ""
        exit_code = _field(terminated, "exit_code")
    else:
        current = "Unknown"
        message = ""
    return {
        "name": _field(status, "name") or "",
        "ready": bool(_field(status, "ready", False)),
        "restart_count": int(_field(status, "restart_count", 0) or 0),
        "state": current,
        "message": message,
        "exit_code": exit_code,
        "last_terminated_reason": last_reason,
        "problem": current in PROBLEM_STATES or last_reason in PROBLEM_STATES,
    }


def _condition_row(condition) -> dict:
    return {
        "type": _field(condition, "type"),
        "status": _field(condition, "status"),
        "reason": _field(condition, "reason"),
        "message": _field(condition, "message") or "",
    }


def deployment_progress(
    observed_generation,
    generation,
    desired,
    updated,
    ready,
    available,
    unavailable,
    conditions,
) -> dict:
    parsed = [_condition_row(item) for item in conditions or []]
    failed = any(item["reason"] == "ProgressDeadlineExceeded" for item in parsed)
    available_status = next((item["status"] for item in parsed if item["type"] == "Available"), None)
    generation_ok = (
        observed_generation is not None
        and generation is not None
        and int(observed_generation) >= int(generation)
    )
    desired_count = int(desired or 0)
    counts_ok = int(updated or 0) == desired_count and int(ready or 0) == desired_count and int(unavailable or 0) == 0
    complete = generation_ok and counts_ok and available_status != "False" and not failed
    return {
        "complete": complete,
        "failed": failed,
        "desired": desired_count,
        "updated": int(updated or 0),
        "ready": int(ready or 0),
        "available": int(available or 0),
        "unavailable": int(unavailable or 0),
        "observed_generation": observed_generation,
        "generation": generation,
        "conditions": parsed,
    }


def deploymentconfig_progress(desired, updated, available, observed_generation, generation, conditions) -> dict:
    parsed = [_condition_row(item) for item in conditions or []]
    failed = any(item["reason"] == "ProgressDeadlineExceeded" and item["status"] == "True" for item in parsed)
    desired_count = int(desired or 0)
    complete = int(updated or 0) >= desired_count and int(available or 0) >= desired_count and not failed
    if observed_generation is not None and generation is not None and int(observed_generation) < int(generation):
        complete = False
    return {
        "complete": complete,
        "failed": failed,
        "desired": desired_count,
        "updated": int(updated or 0),
        "ready": int(available or 0),
        "available": int(available or 0),
        "observed_generation": observed_generation,
        "generation": generation,
        "conditions": parsed,
    }


def poll_until(check, timeout: float, interval: float, sleep, monotonic) -> dict:
    """Call check until it completes, fails, or the timeout elapses."""
    last = check()
    if last.get("complete") or last.get("failed"):
        last["timed_out"] = False
        return last
    if timeout <= 0:
        last["timed_out"] = True
        last["complete"] = False
        return last
    deadline = monotonic() + timeout
    while True:
        now = monotonic()
        if now >= deadline:
            last["timed_out"] = True
            last["complete"] = False
            return last
        sleep(min(interval, deadline - now))
        last = check()
        if last.get("complete") or last.get("failed"):
            last["timed_out"] = False
            return last


def owned_by(obj, kind: str, name: str, uid: str = "") -> bool:
    meta = _field(obj, "metadata")
    for owner in _field(meta, "owner_references") or []:
        if _field(owner, "kind") != kind or _field(owner, "name") != name:
            continue
        owner_uid = _field(owner, "uid") or ""
        if uid and owner_uid and owner_uid != uid:
            continue
        return True
    return False


def revision_annotation(obj, key: str) -> int:
    meta = _field(obj, "metadata")
    annotations = _field(meta, "annotations") or {}
    raw = annotations.get(key, "0")
    return int(raw) if str(raw).isdigit() else 0


def template_images(template) -> list[dict]:
    spec = _field(template, "spec") or {}
    containers = _field(spec, "containers") or []
    rows = []
    for container in containers:
        rows.append({"name": _field(container, "name") or "", "image": _field(container, "image") or ""})
    return rows


def stamp_template(raw: dict, rollback_at: str) -> dict:
    """Drop controller metadata and stamp a rollback annotation so the restore rolls pods."""
    meta = dict(raw.get("metadata") or {})
    for key in ("resourceVersion", "uid", "creationTimestamp", "managedFields", "generation", "selfLink"):
        meta.pop(key, None)
    annotations = dict(meta.get("annotations") or {})
    annotations["k8s-cloud-deploy-mcp/rollback-at"] = rollback_at
    meta["annotations"] = annotations
    stamped = dict(raw)
    stamped["metadata"] = meta
    return stamped
