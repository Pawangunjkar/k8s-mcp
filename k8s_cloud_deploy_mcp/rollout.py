"""Confirmation-gated image rollout, status polling, and rollback."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone

from kubernetes.client import ApiException

from k8s_cloud_deploy_mcp.cli import binary_name, rollout_status_argv, run_cli
from k8s_cloud_deploy_mcp.diagnostics import (
    deployment_progress,
    deploymentconfig_progress,
    owned_by,
    poll_until,
    revision_annotation,
    stamp_template,
    template_images,
)
from k8s_cloud_deploy_mcp.guards import (
    apply_digest,
    clamp_timeout,
    confirmation_preview,
    normalize_resource_type,
    select_revision,
    validate_container_name,
    validate_image_reference,
    validate_namespace,
    validate_resource_name,
    validate_revision,
)
from k8s_cloud_deploy_mcp.kube import FIELD_MANAGER, REQUEST_TIMEOUT, STRATEGIC_MERGE

ROLLOUT_TOOL = "k8s_trigger_deployment_rollout"
STATUS_TOOL = "k8s_check_rollout_status"
ROLLBACK_TOOL = "k8s_rollback_deployment"
DC_GROUP = "apps.openshift.io"
DC_VERSION = "v1"
DC_PLURAL = "deploymentconfigs"


def _supports_deploymentconfig(session) -> bool:
    return any(item == DC_GROUP or item.startswith(DC_GROUP + "/") for item in session.api_groups)


def _require_deploymentconfig(session) -> None:
    if not _supports_deploymentconfig(session):
        raise ValueError("DeploymentConfig requires the apps.openshift.io API group.")


def _container_name(container) -> str:
    if isinstance(container, dict):
        return str(container.get("name") or "")
    return str(container.name or "")


def _container_image(container) -> str:
    if isinstance(container, dict):
        return str(container.get("image") or "")
    return str(container.image or "")


def _choose_container(containers, container_name: str):
    names = [_container_name(container) for container in containers]
    if not containers:
        raise ValueError("The workload has no containers.")
    if container_name:
        for container, name in zip(containers, names):
            if name == container_name:
                return container, name
        raise ValueError(f"Container {container_name!r} was not found. Available: {names}.")
    if len(containers) != 1:
        raise ValueError(f"Multiple containers are present ({names}). Pass container_name.")
    return containers[0], names[0]


def _image_change_warnings(triggers) -> list[str]:
    names = []
    for trigger in triggers or []:
        if not isinstance(trigger, dict) or trigger.get("type") != "ImageChange" or trigger.get("paused"):
            continue
        source = (trigger.get("imageChangeParams") or {}).get("from") or {}
        names.append(str(source.get("name") or "ImageChange"))
    if not names:
        return []
    return ["Active ImageChange triggers will be paused so the digest is not overwritten: " + ", ".join(names)]


def _pause_image_change(triggers) -> list[dict]:
    paused = []
    for trigger in triggers or []:
        item = dict(trigger)
        if item.get("type") == "ImageChange":
            item["paused"] = True
        paused.append(item)
    return paused


def _mutation(tool: str, applied: bool, message: str, details: dict) -> dict:
    payload = {
        "ok": True,
        "access": "mutating",
        "applied": applied,
        "confirmation_required": False,
        "tool": tool,
        "message": message,
    }
    payload.update(details)
    return payload


def _preview(tool: str, plan: dict) -> dict:
    payload = confirmation_preview(plan)
    payload["tool"] = tool
    return payload


def _drop_none(value):
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value


def trigger_rollout(
    session,
    namespace: str,
    resource_type: str,
    resource_name: str,
    image_tag: str,
    confirm: bool = False,
    container_name: str = "",
) -> dict:
    ns = validate_namespace(namespace)
    name = validate_resource_name(resource_name)
    kind = normalize_resource_type(resource_type)
    container = validate_container_name(container_name)
    validate_image_reference(image_tag)
    if kind == "deploymentconfig":
        _require_deploymentconfig(session)
        return _trigger_deploymentconfig(session, ns, name, image_tag, bool(confirm), container)
    return _trigger_deployment(session, ns, name, image_tag, bool(confirm), container)


def _trigger_deployment(session, namespace: str, name: str, image_tag: str, confirm: bool, container_name: str) -> dict:
    deployment = session.apps.read_namespaced_deployment(name, namespace, _request_timeout=REQUEST_TIMEOUT)
    containers = deployment.spec.template.spec.containers or []
    _target, chosen = _choose_container(containers, container_name)
    previous = next(_container_image(item) for item in containers if _container_name(item) == chosen)
    updated = apply_digest(previous, image_tag)
    details = {
        "namespace": namespace,
        "resource_type": "deployment",
        "resource_name": name,
        "container": chosen,
        "previous_image": previous,
        "image": updated,
        "digest_verified": True,
        "warnings": [],
    }
    if not confirm:
        return _preview(ROLLOUT_TOOL, details)
    if previous == updated:
        return _mutation(ROLLOUT_TOOL, False, "The container already uses this digest. No rollout was started.", details)
    body = {"spec": {"template": {"spec": {"containers": [{"name": chosen, "image": updated}]}}}}
    session.apps.patch_namespaced_deployment(
        name,
        namespace,
        body,
        field_manager=FIELD_MANAGER,
        _content_type=STRATEGIC_MERGE,
        _request_timeout=REQUEST_TIMEOUT,
    )
    return _mutation(ROLLOUT_TOOL, True, "Deployment image updated and rollout triggered.", details)


def _trigger_deploymentconfig(session, namespace: str, name: str, image_tag: str, confirm: bool, container_name: str) -> dict:
    current = session.custom.get_namespaced_custom_object(DC_GROUP, DC_VERSION, namespace, DC_PLURAL, name)
    spec = current.get("spec") or {}
    containers = [dict(item) for item in ((spec.get("template") or {}).get("spec") or {}).get("containers") or []]
    _target, chosen = _choose_container(containers, container_name)
    previous = ""
    for container in containers:
        if container.get("name") == chosen:
            previous = str(container.get("image") or "")
            container["image"] = apply_digest(previous, image_tag)
            break
    updated = next(container["image"] for container in containers if container.get("name") == chosen)
    triggers = list(spec.get("triggers") or [])
    warnings = _image_change_warnings(triggers)
    details = {
        "namespace": namespace,
        "resource_type": "deploymentconfig",
        "resource_name": name,
        "container": chosen,
        "previous_image": previous,
        "image": updated,
        "digest_verified": True,
        "warnings": warnings,
    }
    if not confirm:
        return _preview(ROLLOUT_TOOL, details)
    image_changed = previous != updated
    if not image_changed and not warnings:
        return _mutation(
            ROLLOUT_TOOL,
            False,
            "The container already uses this digest. No rollout was started.",
            details,
        )
    patch: dict = {"spec": {"template": {"spec": {"containers": containers}}}}
    if warnings:
        patch["spec"]["triggers"] = _pause_image_change(triggers)
    session.custom.patch_namespaced_custom_object(DC_GROUP, DC_VERSION, namespace, DC_PLURAL, name, patch)
    message = (
        "DeploymentConfig image updated and rollout triggered."
        if image_changed
        else "ImageChange triggers were paused so the existing digest is kept."
    )
    return _mutation(ROLLOUT_TOOL, True, message, details)


def _deployment_history(session, namespace: str, name: str) -> list[dict]:
    deployment = session.apps.read_namespaced_deployment(name, namespace, _request_timeout=REQUEST_TIMEOUT)
    listed = session.apps.list_namespaced_replica_set(namespace, limit=200, _request_timeout=REQUEST_TIMEOUT)
    history = []
    uid = deployment.metadata.uid or ""
    for replica_set in listed.items:
        if not owned_by(replica_set, "Deployment", name, uid):
            continue
        revision = revision_annotation(replica_set, "deployment.kubernetes.io/revision")
        if revision <= 0 or replica_set.spec is None or replica_set.spec.template is None:
            continue
        history.append(
            {
                "revision": revision,
                "images": template_images(replica_set.spec.template),
                "template": replica_set.spec.template,
            }
        )
    return history


def _dc_belongs(controller, name: str, uid: str) -> bool:
    if owned_by(controller, "DeploymentConfig", name, uid):
        return True
    annotations = controller.metadata.annotations or {}
    labels = controller.metadata.labels or {}
    return (
        annotations.get("openshift.io/deployment-config.name") == name
        or labels.get("deploymentconfig") == name
        or labels.get("openshift.io/deployment-config.name") == name
    )


def _dc_revision(controller, name: str) -> int:
    for key in ("openshift.io/deployment-config.latest-version", "openshift.io/deployment.revision"):
        revision = revision_annotation(controller, key)
        if revision:
            return revision
    controller_name = controller.metadata.name or ""
    prefix = f"{name}-"
    suffix = controller_name[len(prefix) :] if controller_name.startswith(prefix) else ""
    return int(suffix) if suffix.isdigit() else 0


def _deploymentconfig_history(session, namespace: str, name: str) -> tuple[list[dict], list[str]]:
    current = session.custom.get_namespaced_custom_object(DC_GROUP, DC_VERSION, namespace, DC_PLURAL, name)
    uid = str((current.get("metadata") or {}).get("uid") or "")
    warnings = _image_change_warnings((current.get("spec") or {}).get("triggers") or [])
    listed = session.core.list_namespaced_replication_controller(namespace, limit=200, _request_timeout=REQUEST_TIMEOUT)
    history = []
    for controller in listed.items:
        if not _dc_belongs(controller, name, uid):
            continue
        revision = _dc_revision(controller, name)
        if revision <= 0 or controller.spec is None or controller.spec.template is None:
            continue
        history.append(
            {
                "revision": revision,
                "images": template_images(controller.spec.template),
                "template": controller.spec.template,
            }
        )
    return history, warnings


def _public_history(history: list[dict]) -> list[dict]:
    return [{"revision": item["revision"], "images": item["images"]} for item in history]


def rollback(
    session,
    namespace: str,
    resource_type: str,
    resource_name: str,
    revision: int = 0,
    confirm: bool = False,
) -> dict:
    ns = validate_namespace(namespace)
    name = validate_resource_name(resource_name)
    kind = normalize_resource_type(resource_type)
    requested = validate_revision(revision)
    warnings: list[str] = []
    if kind == "deploymentconfig":
        _require_deploymentconfig(session)
        history, warnings = _deploymentconfig_history(session, ns, name)
    else:
        history = _deployment_history(session, ns, name)
    target = select_revision([item["revision"] for item in history], requested)
    chosen = next(item for item in history if item["revision"] == target)
    details = {
        "namespace": ns,
        "resource_type": kind,
        "resource_name": name,
        "requested_revision": requested,
        "to_revision": target,
        "known_revisions": _public_history(history),
        "images": chosen["images"],
        "warnings": warnings,
    }
    if not confirm:
        return _preview(ROLLBACK_TOOL, details)
    stamped = stamp_template(
        _drop_none(session.api_client.sanitize_for_serialization(chosen["template"])),
        datetime.now(timezone.utc).isoformat(),
    )
    if kind == "deployment":
        session.apps.patch_namespaced_deployment(
            name,
            ns,
            {"spec": {"template": stamped}},
            field_manager=FIELD_MANAGER,
            _content_type=STRATEGIC_MERGE,
            _request_timeout=REQUEST_TIMEOUT,
        )
    else:
        current = session.custom.get_namespaced_custom_object(DC_GROUP, DC_VERSION, ns, DC_PLURAL, name)
        patch: dict = {"spec": {"template": stamped}}
        triggers = (current.get("spec") or {}).get("triggers") or []
        if _image_change_warnings(triggers):
            patch["spec"]["triggers"] = _pause_image_change(triggers)
        session.custom.patch_namespaced_custom_object(DC_GROUP, DC_VERSION, ns, DC_PLURAL, name, patch)
    return _mutation(ROLLBACK_TOOL, True, f"Rollback to revision {target} was submitted.", details)


def _sample_deployment(session, namespace: str, name: str) -> dict:
    deployment = session.apps.read_namespaced_deployment(name, namespace, _request_timeout=REQUEST_TIMEOUT)
    status = deployment.status
    spec = deployment.spec
    listed = session.apps.list_namespaced_replica_set(namespace, limit=200, _request_timeout=REQUEST_TIMEOUT)
    history = []
    uid = deployment.metadata.uid or ""
    for replica_set in listed.items:
        if not owned_by(replica_set, "Deployment", name, uid):
            continue
        replica_status = replica_set.status
        history.append(
            {
                "name": replica_set.metadata.name,
                "revision": revision_annotation(replica_set, "deployment.kubernetes.io/revision"),
                "desired": int((replica_status.replicas or 0) if replica_status else 0),
                "ready": int((replica_status.ready_replicas or 0) if replica_status else 0),
                "available": int((replica_status.available_replicas or 0) if replica_status else 0),
            }
        )
    history.sort(key=lambda row: row["revision"])
    progress = deployment_progress(
        getattr(status, "observed_generation", None) if status else None,
        deployment.metadata.generation,
        spec.replicas if spec and spec.replicas is not None else 0,
        getattr(status, "updated_replicas", 0) if status else 0,
        getattr(status, "ready_replicas", 0) if status else 0,
        getattr(status, "available_replicas", 0) if status else 0,
        getattr(status, "unavailable_replicas", 0) if status else 0,
        getattr(status, "conditions", None) if status else None,
    )
    progress["replica_sets"] = history
    return progress


def _sample_deploymentconfig(session, namespace: str, name: str) -> dict:
    current = session.custom.get_namespaced_custom_object(DC_GROUP, DC_VERSION, namespace, DC_PLURAL, name)
    spec = current.get("spec") or {}
    status = current.get("status") or {}
    progress = deploymentconfig_progress(
        spec.get("replicas") or 0,
        status.get("updatedReplicas") or 0,
        status.get("availableReplicas") or 0,
        status.get("observedGeneration"),
        (current.get("metadata") or {}).get("generation"),
        status.get("conditions") or [],
    )
    uid = str((current.get("metadata") or {}).get("uid") or "")
    listed = session.core.list_namespaced_replication_controller(namespace, limit=200, _request_timeout=REQUEST_TIMEOUT)
    history = []
    for controller in listed.items:
        if not _dc_belongs(controller, name, uid):
            continue
        controller_status = controller.status
        history.append(
            {
                "name": controller.metadata.name,
                "revision": _dc_revision(controller, name),
                "desired": int((controller_status.replicas or 0) if controller_status else 0),
                "ready": int((controller_status.ready_replicas or 0) if controller_status else 0),
                "available": int((controller_status.available_replicas or 0) if controller_status else 0),
            }
        )
    history.sort(key=lambda row: row["revision"])
    progress["replication_controllers"] = history
    return progress


def check_rollout(
    session,
    namespace: str,
    resource_type: str,
    resource_name: str,
    timeout_seconds: int = 60,
    which=None,
    run_command=None,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict:
    ns = validate_namespace(namespace)
    name = validate_resource_name(resource_name)
    kind = normalize_resource_type(resource_type)
    timeout = clamp_timeout(timeout_seconds)
    if kind == "deploymentconfig":
        _require_deploymentconfig(session)
        sample = lambda: _sample_deploymentconfig(session, ns, name)
        binary_preference = ("oc",)
    else:
        sample = lambda: _sample_deployment(session, ns, name)
        binary_preference = ("kubectl", "oc")
    progress = poll_until(sample, timeout=timeout, interval=2, sleep=sleep, clock=clock)
    locator = which or shutil.which
    runner = run_command or run_cli
    cli: dict = {"available": False}
    if timeout > 0:
        path = next((locator(item) for item in binary_preference if locator(item)), None)
        if path and binary_name(path) in {"kubectl", "oc"}:
            try:
                spot = min(10, max(1, timeout))
                argv = rollout_status_argv(path, kind, ns, name, spot)
                cli = runner(argv, timeout=min(12, spot + 2))
            except (ValueError, ApiException) as exc:
                cli = {"available": True, "error": str(getattr(exc, "reason", None) or exc)}
        else:
            cli = {"available": False}
    if progress.get("failed"):
        message = "The rollout reported ProgressDeadlineExceeded."
        ok = False
    elif progress.get("complete"):
        message = "Rollout complete."
        ok = True
    elif progress.get("timed_out"):
        message = "Rollout did not finish before the timeout."
        ok = False
    else:
        message = "Rollout is still progressing."
        ok = False
    progress.update(
        {
            "ok": ok,
            "access": "read-only",
            "tool": STATUS_TOOL,
            "namespace": ns,
            "resource_type": kind,
            "resource_name": name,
            "message": message,
            "cli": cli,
        }
    )
    return progress
