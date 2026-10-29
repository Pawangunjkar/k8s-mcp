"""Kubernetes and OpenShift client session."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import yaml
from kubernetes import client, config
from kubernetes.client import ApiException

from k8s_cloud_deploy_mcp.cli import client_version_argv, run_cli
from k8s_cloud_deploy_mcp.cloud import (
    CloudContext,
    api_is_openshift,
    auth_gaps,
    flatten_api_groups,
    infrastructure_of,
    load_cloud_context,
    resolve_provider,
    safe_version_url,
)
from k8s_cloud_deploy_mcp.diagnostics import describe_container
from k8s_cloud_deploy_mcp.guards import (
    clamp_tail,
    validate_container_name,
    validate_label_selector,
    validate_namespace,
    validate_resource_name,
)

LIST_LIMIT = 500
REQUEST_TIMEOUT = 20
STRATEGIC_MERGE = "application/strategic-merge-patch+json"
FIELD_MANAGER = "k8s-cloud-deploy-mcp"
LOG_CHAR_LIMIT = 200_000


@dataclass
class K8sSession:
    name: str
    namespace: str
    context: str
    core: client.CoreV1Api
    apps: client.AppsV1Api
    custom: client.CustomObjectsApi
    api_client: client.ApiClient
    cloud: CloudContext
    api_groups: set[str]
    is_openshift: bool
    provider: str
    provider_source: str
    infrastructure: str
    config_source: str
    server_host: str


def _api_message(exc: Exception) -> str:
    if isinstance(exc, ApiException):
        return f"Kubernetes API {exc.status}: {exc.reason}"
    return str(exc)


def _capture(warnings: list[str], label: str, fn, default):
    try:
        return fn()
    except Exception as exc:
        warnings.append(f"{label}: {_api_message(exc)}")
        return default


def _quantity_map(value) -> dict:
    if not value:
        return {}
    return {str(key): str(item) for key, item in dict(value).items()}


def _truncated(payload) -> bool:
    if isinstance(payload, dict):
        return bool((payload.get("metadata") or {}).get("continue"))
    meta = getattr(payload, "metadata", None)
    return bool(getattr(meta, "_continue", None))


def _load_cluster_config(kubeconfig: str, context: str) -> str:
    if kubeconfig:
        config.load_kube_config(config_file=kubeconfig, context=context or None)
        return "kubeconfig"
    try:
        config.load_kube_config(context=context or None)
        return "kubeconfig"
    except Exception:
        config.load_incluster_config()
        return "in-cluster"


def _active_context(requested: str, source: str) -> str:
    if source == "in-cluster":
        return requested or "in-cluster"
    try:
        _contexts, active = config.list_kube_config_contexts()
        if requested:
            return requested
        if active and active.get("name"):
            return str(active["name"])
    except Exception:
        return requested or "in-cluster"
    return requested or "in-cluster"


def _discover_groups(api_client: client.ApiClient) -> set[str]:
    listing = client.ApisApi(api_client).get_api_versions(_request_timeout=REQUEST_TIMEOUT)
    pairs = []
    for group in listing.groups or []:
        versions = [item.version for item in (group.versions or []) if item.version]
        if group.preferred_version and group.preferred_version.version:
            versions.append(group.preferred_version.version)
        pairs.append((group.name, versions))
    return flatten_api_groups(pairs)


def _scrub(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    cleaned = text.replace(secret, "[redacted]")
    bearer = secret[7:].strip() if secret.lower().startswith("bearer ") else secret
    if bearer and bearer != secret:
        cleaned = cleaned.replace(bearer, "[redacted]")
    return cleaned


def _bearer_token(cfg: client.Configuration) -> str:
    api_key = cfg.api_key or {}
    prefix = (cfg.api_key_prefix or {}).get("authorization") or ""
    raw = api_key.get("authorization") or ""
    if not raw:
        return ""
    if prefix and not raw.lower().startswith("bearer "):
        return f"{prefix} {raw}".strip()
    return raw


def probe_apiserver(host: str, token: str, verify: bool | str) -> dict:
    """GET /version with httpx. The token is sent as a header and never returned."""
    try:
        url = safe_version_url(host)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    headers = {"Authorization": token} if token else {}
    try:
        with httpx.Client(timeout=8.0, verify=verify, follow_redirects=False) as http:
            response = http.get(url, headers=headers)
        payload = {}
        if "json" in response.headers.get("content-type", ""):
            payload = response.json()
        result = {
            "ok": response.status_code == 200,
            "status_code": response.status_code,
            "git_version": str(payload.get("gitVersion") or ""),
            "platform": str(payload.get("platform") or ""),
        }
    except Exception as exc:
        result = {"ok": False, "error": _scrub(exc.__class__.__name__, token)}
    encoded = json.dumps(result)
    if token and token in encoded:
        result = json.loads(_scrub(encoded, token))
    return result


def _verify_setting(cfg: client.Configuration) -> bool | str:
    if cfg.verify_ssl is False:
        return False
    if cfg.ssl_ca_cert:
        return cfg.ssl_ca_cert
    return True


def _client_versions(is_openshift: bool, which) -> list[dict]:
    names = ["kubectl"]
    if is_openshift:
        names.append("oc")
    elif which("oc"):
        names.append("oc")
    rows = []
    seen = set()
    for name in names:
        path = which(name)
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            argv = client_version_argv(path)
        except ValueError as exc:
            rows.append({"binary": name, "available": True, "error": str(exc)})
            continue
        result = run_cli(argv, timeout=5)
        git_version = ""
        if name == "kubectl" and result.get("stdout"):
            try:
                parsed = json.loads(result["stdout"])
                git_version = str((parsed.get("clientVersion") or {}).get("gitVersion") or "")
            except json.JSONDecodeError:
                git_version = ""
        rows.append(
            {
                "binary": name,
                "exit_code": result.get("exit_code"),
                "git_version": git_version,
                "stdout": "" if git_version else (result.get("stdout") or "")[:300],
                "stderr": (result.get("stderr") or "")[:300],
            }
        )
    return rows


class K8sHub:
    def __init__(self) -> None:
        self.sessions: dict[str, K8sSession] = {}

    def connect(self, session_name: str, kubeconfig: str, context: str, namespace: str) -> str:
        cloud = load_cloud_context(kubeconfig, context, namespace)
        source = _load_cluster_config(cloud.kubeconfig, cloud.k8s_context)
        current = _active_context(cloud.k8s_context, source)
        api_client = client.ApiClient()
        groups = _discover_groups(api_client)
        core = client.CoreV1Api(api_client)
        apps = client.AppsV1Api(api_client)
        custom = client.CustomObjectsApi(api_client)
        cfg = client.Configuration.get_default_copy()
        server_host = cfg.host or ""
        provider_ids: list[str] = []
        try:
            nodes = core.list_node(limit=50, _request_timeout=REQUEST_TIMEOUT)
            provider_ids = [item.spec.provider_id for item in nodes.items if item.spec and item.spec.provider_id]
        except Exception:
            provider_ids = []
        is_openshift = api_is_openshift(groups)
        infrastructure = infrastructure_of(current, server_host, provider_ids)
        provider, provider_source = resolve_provider(cloud.configured_provider, is_openshift, infrastructure)
        self.sessions[session_name] = K8sSession(
            name=session_name,
            namespace=cloud.namespace,
            context=current,
            core=core,
            apps=apps,
            custom=custom,
            api_client=api_client,
            cloud=cloud,
            api_groups=groups,
            is_openshift=is_openshift,
            provider=provider,
            provider_source=provider_source,
            infrastructure=infrastructure,
            config_source=source,
            server_host=server_host,
        )
        gaps = auth_gaps(provider, cloud.aws_region, cloud.azure_subscription_id, cloud.gcp_project)
        gap_text = f" auth_gaps={'; '.join(gaps)}" if gaps else ""
        return (
            f"Connected '{session_name}' context={current or 'in-cluster'} namespace={cloud.namespace} "
            f"provider={provider} source={provider_source} openshift={str(is_openshift).lower()} "
            f"infrastructure={infrastructure}.{gap_text}"
        )

    def session(self, name: str) -> K8sSession:
        if name not in self.sessions:
            raise KeyError(f"No Kubernetes session '{name}'. Call k8s_connect first.")
        return self.sessions[name]

    def cluster_overview(self, session_name: str, which=None) -> dict:
        import shutil

        session = self.session(session_name)
        warnings: list[str] = []
        which = which or shutil.which
        version = _capture(
            warnings,
            "version",
            lambda: client.VersionApi(session.api_client).get_code(_request_timeout=REQUEST_TIMEOUT),
            None,
        )
        nodes = _capture(
            warnings,
            "nodes",
            lambda: session.core.list_node(limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT),
            None,
        )
        namespaces = _capture(
            warnings,
            "namespaces",
            lambda: session.core.list_namespace(limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT),
            None,
        )
        quotas = _capture(
            warnings,
            "resourcequotas",
            lambda: session.core.list_resource_quota_for_all_namespaces(limit=200, _request_timeout=REQUEST_TIMEOUT),
            None,
        )
        openshift_version = ""
        projects: list[str] = []
        projects_source = ""
        if session.is_openshift:
            cluster_version = _capture(
                warnings,
                "clusterversion",
                lambda: session.custom.get_cluster_custom_object(
                    "config.openshift.io", "v1", "clusterversions", "version"
                ),
                None,
            )
            if isinstance(cluster_version, dict):
                openshift_version = str(((cluster_version.get("status") or {}).get("desired") or {}).get("version") or "")
            project_list = _capture(
                warnings,
                "projects",
                lambda: session.custom.list_cluster_custom_object("project.openshift.io", "v1", "projects", limit=LIST_LIMIT),
                None,
            )
            if isinstance(project_list, dict):
                projects = [item.get("metadata", {}).get("name", "") for item in project_list.get("items") or []]
                projects_source = "project.openshift.io/v1"
        node_rows = []
        provider_ids = []
        if nodes is not None:
            for item in nodes.items:
                ready = "Unknown"
                conditions = []
                status = item.status
                for condition in (status.conditions or []) if status else []:
                    if condition.type in {"Ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}:
                        conditions.append({"type": condition.type, "status": condition.status})
                    if condition.type == "Ready":
                        ready = condition.status
                provider_id = item.spec.provider_id if item.spec and item.spec.provider_id else ""
                if provider_id:
                    provider_ids.append(provider_id)
                info = status.node_info if status else None
                node_rows.append(
                    {
                        "name": item.metadata.name,
                        "ready": ready,
                        "unschedulable": bool(item.spec.unschedulable) if item.spec else False,
                        "kubelet_version": info.kubelet_version if info else "",
                        "provider_id": provider_id,
                        "conditions": conditions,
                    }
                )
        namespace_names = [item.metadata.name for item in namespaces.items] if namespaces is not None else []
        if session.is_openshift and not projects:
            projects = list(namespace_names)
            projects_source = projects_source or "namespaces"
        quota_rows = []
        if quotas is not None:
            for item in quotas.items:
                status = item.status
                quota_rows.append(
                    {
                        "namespace": item.metadata.namespace,
                        "name": item.metadata.name,
                        "hard": _quantity_map(status.hard if status else None),
                        "used": _quantity_map(status.used if status else None),
                    }
                )
        cfg = client.Configuration.get_default_copy()
        probe = probe_apiserver(session.server_host, _bearer_token(cfg), _verify_setting(cfg))
        cloud = session.cloud
        gaps = auth_gaps(session.provider, cloud.aws_region, cloud.azure_subscription_id, cloud.gcp_project)
        return {
            "ok": True,
            "access": "read-only",
            "tool": "k8s_get_cluster_overview",
            "context": session.context,
            "namespace": session.namespace,
            "config_source": session.config_source,
            "cloud": {
                "provider": session.provider,
                "configured_provider": cloud.configured_provider,
                "detection_source": session.provider_source,
                "is_openshift": session.is_openshift,
                "infrastructure": session.infrastructure,
                "aws_region": cloud.aws_region,
                "azure_subscription_id": cloud.azure_subscription_id,
                "gcp_project": cloud.gcp_project,
                "auth_gaps": gaps,
                "openshift_api_groups": sorted(
                    group for group in session.api_groups if "openshift.io" in group and "/" not in group
                ),
            },
            "version": {
                "kubernetes": getattr(version, "git_version", "") if version else "",
                "platform": getattr(version, "platform", "") if version else "",
                "openshift": openshift_version,
            },
            "api_probe": probe,
            "nodes": {
                "total": len(node_rows),
                "ready": sum(1 for row in node_rows if row["ready"] == "True"),
                "not_ready": sum(1 for row in node_rows if row["ready"] != "True"),
                "truncated": _truncated(nodes) if nodes is not None else False,
                "items": node_rows,
            },
            "namespaces": {
                "count": len(namespace_names),
                "truncated": _truncated(namespaces) if namespaces is not None else False,
                "items": namespace_names,
            },
            "projects": {
                "source": projects_source,
                "count": len(projects),
                "items": projects,
            },
            "resource_quotas": quota_rows,
            "client_tools": _capture(warnings, "client-tools", lambda: _client_versions(session.is_openshift, which), []),
            "warnings": warnings,
        }

    def deployments_and_configs(self, session_name: str, namespace: str) -> dict:
        session = self.session(session_name)
        ns = validate_namespace(namespace) if namespace else ""
        warnings: list[str] = []
        truncated = False
        rows: list[dict] = []

        def add_apps(kind: str, listed, desired_fn, ready_fn, updated_fn) -> None:
            nonlocal truncated
            if listed is None:
                return
            truncated = truncated or _truncated(listed)
            for item in listed.items:
                if len(rows) >= LIST_LIMIT:
                    truncated = True
                    return
                status = item.status
                spec = item.spec
                rows.append(
                    {
                        "kind": kind,
                        "namespace": item.metadata.namespace,
                        "name": item.metadata.name,
                        "desired": desired_fn(spec, status),
                        "ready": ready_fn(status),
                        "updated": updated_fn(status),
                    }
                )

        if ns:
            deployments = _capture(
                warnings,
                "deployments",
                lambda: session.apps.list_namespaced_deployment(ns, limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT),
                None,
            )
            stateful_sets = _capture(
                warnings,
                "statefulsets",
                lambda: session.apps.list_namespaced_stateful_set(ns, limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT),
                None,
            )
            daemon_sets = _capture(
                warnings,
                "daemonsets",
                lambda: session.apps.list_namespaced_daemon_set(ns, limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT),
                None,
            )
        else:
            deployments = _capture(
                warnings,
                "deployments",
                lambda: session.apps.list_deployment_for_all_namespaces(limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT),
                None,
            )
            stateful_sets = _capture(
                warnings,
                "statefulsets",
                lambda: session.apps.list_stateful_set_for_all_namespaces(limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT),
                None,
            )
            daemon_sets = _capture(
                warnings,
                "daemonsets",
                lambda: session.apps.list_daemon_set_for_all_namespaces(limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT),
                None,
            )
        add_apps(
            "Deployment",
            deployments,
            lambda spec, status: int(spec.replicas if spec and spec.replicas is not None else (status.replicas or 0) if status else 0),
            lambda status: int((status.ready_replicas or 0) if status else 0),
            lambda status: int((status.updated_replicas or 0) if status else 0),
        )
        add_apps(
            "StatefulSet",
            stateful_sets,
            lambda spec, status: int(spec.replicas if spec and spec.replicas is not None else 0),
            lambda status: int((status.ready_replicas or 0) if status else 0),
            lambda status: int((status.updated_replicas or 0) if status else 0),
        )
        add_apps(
            "DaemonSet",
            daemon_sets,
            lambda spec, status: int((status.desired_number_scheduled or 0) if status else 0),
            lambda status: int((status.number_ready or 0) if status else 0),
            lambda status: int((status.updated_number_scheduled or 0) if status else 0),
        )
        if session.is_openshift:
            def load_dc():
                if ns:
                    return session.custom.list_namespaced_custom_object(
                        "apps.openshift.io", "v1", ns, "deploymentconfigs", limit=LIST_LIMIT
                    )
                return session.custom.list_cluster_custom_object(
                    "apps.openshift.io", "v1", "deploymentconfigs", limit=LIST_LIMIT
                )

            deploymentconfigs = _capture(warnings, "deploymentconfigs", load_dc, None)
            if isinstance(deploymentconfigs, dict):
                truncated = truncated or _truncated(deploymentconfigs)
                for item in deploymentconfigs.get("items") or []:
                    if len(rows) >= LIST_LIMIT:
                        truncated = True
                        break
                    meta = item.get("metadata") or {}
                    spec = item.get("spec") or {}
                    status = item.get("status") or {}
                    rows.append(
                        {
                            "kind": "DeploymentConfig",
                            "namespace": meta.get("namespace") or ns,
                            "name": meta.get("name"),
                            "desired": int(spec.get("replicas") or 0),
                            "ready": int(status.get("availableReplicas") or 0),
                            "updated": int(status.get("updatedReplicas") or 0),
                        }
                    )
        else:
            warnings.append("DeploymentConfigs are unavailable because this API does not advertise apps.openshift.io.")
        rows.sort(key=lambda row: (row.get("namespace") or "", row.get("kind") or "", row.get("name") or ""))
        return {
            "ok": True,
            "access": "read-only",
            "tool": "k8s_list_deployments_and_configs",
            "namespace": ns or "*",
            "is_openshift": session.is_openshift,
            "truncated": truncated,
            "workloads": rows,
            "warnings": warnings,
        }

    def pod_health(self, session_name: str, namespace: str, label_selector: str) -> dict:
        session = self.session(session_name)
        ns = validate_namespace(namespace)
        selector = validate_label_selector(label_selector)
        pods = session.core.list_namespaced_pod(
            ns,
            label_selector=selector or None,
            limit=LIST_LIMIT,
            _request_timeout=REQUEST_TIMEOUT,
        )
        pod_rows = []
        pod_names = set()
        for item in pods.items:
            status = item.status
            statuses = []
            if status is not None:
                statuses = list(status.container_statuses or []) + list(status.init_container_statuses or [])
            containers = [describe_container(container_status) for container_status in statuses]
            restarts = sum(container["restart_count"] for container in containers)
            phase = status.phase if status else "Unknown"
            pod_names.add(item.metadata.name)
            pod_rows.append(
                {
                    "name": item.metadata.name,
                    "phase": phase,
                    "node": item.spec.node_name if item.spec else "",
                    "qos_class": status.qos_class if status else "",
                    "restarts": restarts,
                    "problem": any(container["problem"] for container in containers) or phase in {"Failed", "Unknown"},
                    "containers": containers,
                }
            )
        events = session.core.list_namespaced_event(
            ns,
            field_selector="type=Warning",
            limit=100,
            _request_timeout=REQUEST_TIMEOUT,
        )
        warning_rows = []
        for event in events.items:
            involved = event.involved_object.name if event.involved_object else ""
            if pod_names and involved not in pod_names:
                continue
            last_seen = event.last_timestamp or event.event_time or event.first_timestamp
            warning_rows.append(
                {
                    "reason": event.reason,
                    "message": event.message,
                    "object": involved,
                    "type": event.type,
                    "count": event.count,
                    "last_seen": str(last_seen or ""),
                }
            )
        warning_rows.sort(key=lambda row: row["last_seen"])
        return {
            "ok": True,
            "access": "read-only",
            "tool": "k8s_get_pod_health_and_diagnostics",
            "namespace": ns,
            "label_selector": selector,
            "truncated": _truncated(pods),
            "pods": pod_rows,
            "warning_events": warning_rows[-25:],
        }

    def container_logs(
        self,
        session_name: str,
        namespace: str,
        pod_name: str,
        container_name: str,
        tail_lines: int,
        previous: bool,
    ) -> dict:
        session = self.session(session_name)
        ns = validate_namespace(namespace)
        pod = validate_resource_name(pod_name)
        container = validate_container_name(container_name)
        tail = clamp_tail(tail_lines)
        live = session.core.read_namespaced_pod(pod, ns, _request_timeout=REQUEST_TIMEOUT)
        if live.spec is None:
            raise ValueError("Pod spec is empty.")
        available = [item.name for item in (live.spec.containers or [])]
        available.extend(item.name for item in (live.spec.init_containers or []))
        chosen = container
        if not chosen:
            if len(live.spec.containers or []) == 1:
                chosen = live.spec.containers[0].name
            else:
                raise ValueError(f"Pod has multiple containers. Pass container_name. Available: {available}")
        kwargs = {
            "name": pod,
            "namespace": ns,
            "container": chosen,
            "tail_lines": tail,
            "previous": bool(previous),
            "timestamps": True,
            "_request_timeout": REQUEST_TIMEOUT,
        }
        try:
            text = session.core.read_namespaced_pod_log(**kwargs)
        except ApiException as exc:
            if previous and exc.status == 400:
                raise ValueError(
                    "Previous container log is unavailable. The container may not have a prior terminated instance."
                ) from exc
            raise
        truncated = len(text) > LOG_CHAR_LIMIT
        if truncated:
            text = text[-LOG_CHAR_LIMIT:]
        return {
            "ok": True,
            "access": "read-only",
            "tool": "k8s_get_container_logs",
            "namespace": ns,
            "pod": pod,
            "container": chosen,
            "previous": bool(previous),
            "tail_lines": tail,
            "truncated": truncated,
            "logs": text,
        }

    def routes_and_services(self, session_name: str, namespace: str) -> dict:
        session = self.session(session_name)
        ns = validate_namespace(namespace)
        warnings: list[str] = []
        routes = []
        routes_available = False
        if session.is_openshift or "route.openshift.io" in session.api_groups:
            routes_available = True
            listed = _capture(
                warnings,
                "routes",
                lambda: session.custom.list_namespaced_custom_object("route.openshift.io", "v1", ns, "routes", limit=LIST_LIMIT),
                None,
            )
            route_items = (listed or {}).get("items", []) if isinstance(listed, dict) else []
            for item in route_items:
                spec = item.get("spec") or {}
                tls = spec.get("tls") or {}
                target = spec.get("to") or {}
                admitted = ""
                admitted_reason = ""
                for ingress in (item.get("status") or {}).get("ingress") or []:
                    for condition in ingress.get("conditions") or []:
                        if condition.get("type") == "Admitted":
                            admitted = condition.get("status") or ""
                            admitted_reason = condition.get("reason") or ""
                routes.append(
                    {
                        "name": (item.get("metadata") or {}).get("name"),
                        "host": spec.get("host") or "",
                        "path": spec.get("path") or "",
                        "wildcard_policy": spec.get("wildcardPolicy") or "",
                        "tls_termination": tls.get("termination"),
                        "insecure_edge_termination_policy": tls.get("insecureEdgeTerminationPolicy") or "",
                        "target": f"{target.get('kind') or 'Service'}/{target.get('name') or ''}",
                        "admitted": admitted,
                        "admitted_reason": admitted_reason,
                    }
                )
        else:
            warnings.append(
                "This context does not advertise route.openshift.io. OpenShift Routes are unavailable. Services are listed."
            )
        services = session.core.list_namespaced_service(ns, limit=LIST_LIMIT, _request_timeout=REQUEST_TIMEOUT)
        service_rows = []
        for item in services.items:
            ports = []
            for port in item.spec.ports or []:
                ports.append(
                    {
                        "name": port.name,
                        "port": port.port,
                        "target_port": str(port.target_port),
                        "protocol": port.protocol,
                    }
                )
            service_rows.append(
                {
                    "name": item.metadata.name,
                    "type": item.spec.type,
                    "cluster_ip": item.spec.cluster_ip,
                    "external_ips": list(item.spec.external_ips or []),
                    "ports": ports,
                }
            )
        return {
            "ok": True,
            "access": "read-only",
            "tool": "k8s_manage_openshift_routes_and_services",
            "namespace": ns,
            "is_openshift": session.is_openshift,
            "routes_available": routes_available,
            "routes": routes,
            "services": service_rows,
            "warnings": warnings,
        }

    def list_pods(self, session_name: str, namespace: str, label: str) -> str:
        session = self.session(session_name)
        ns = validate_namespace(namespace) if namespace else session.namespace
        selector = validate_label_selector(label)
        pods = session.core.list_namespaced_pod(ns, label_selector=selector or None, _request_timeout=REQUEST_TIMEOUT)
        rows = [
            {
                "name": item.metadata.name,
                "phase": item.status.phase,
                "ready": sum(1 for container in (item.status.container_statuses or []) if container.ready),
                "node": item.spec.node_name,
            }
            for item in pods.items
        ]
        return json.dumps({"namespace": ns, "pods": rows}, indent=2)

    def pod_logs(self, session_name: str, namespace: str, pod: str, container: str, tail: int) -> str:
        report = self.container_logs(session_name, namespace or self.session(session_name).namespace, pod, container, tail, False)
        return report["logs"]

    def events(self, session_name: str, namespace: str) -> str:
        session = self.session(session_name)
        ns = validate_namespace(namespace) if namespace else session.namespace
        events = session.core.list_namespaced_event(ns, _request_timeout=REQUEST_TIMEOUT)
        rows = [
            {"reason": item.reason, "message": item.message, "object": item.involved_object.name, "type": item.type}
            for item in events.items[-30:]
        ]
        return json.dumps(rows, indent=2)

    def list_deployments(self, session_name: str, namespace: str) -> str:
        session = self.session(session_name)
        ns = validate_namespace(namespace) if namespace else session.namespace
        items = session.apps.list_namespaced_deployment(ns, _request_timeout=REQUEST_TIMEOUT)
        rows = [
            {
                "name": item.metadata.name,
                "ready": item.status.ready_replicas or 0,
                "replicas": item.status.replicas or 0,
            }
            for item in items.items
        ]
        return json.dumps({"namespace": ns, "deployments": rows}, indent=2)

    def scale(self, session_name: str, namespace: str, name: str, replicas: int) -> str:
        session = self.session(session_name)
        ns = validate_namespace(namespace) if namespace else session.namespace
        resource = validate_resource_name(name)
        if int(replicas) < 0:
            raise ValueError("replicas must be zero or greater.")
        body = {"spec": {"replicas": int(replicas)}}
        session.apps.patch_namespaced_deployment_scale(
            resource,
            ns,
            body,
            field_manager=FIELD_MANAGER,
            _content_type=STRATEGIC_MERGE,
            _request_timeout=REQUEST_TIMEOUT,
        )
        return f"Scaled {ns}/{resource} to {int(replicas)}."

    def restart(self, session_name: str, namespace: str, name: str) -> str:
        from datetime import datetime, timezone

        session = self.session(session_name)
        ns = validate_namespace(namespace) if namespace else session.namespace
        resource = validate_resource_name(name)
        now = datetime.now(timezone.utc).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        session.apps.patch_namespaced_deployment(
            resource,
            ns,
            body,
            field_manager=FIELD_MANAGER,
            _content_type=STRATEGIC_MERGE,
            _request_timeout=REQUEST_TIMEOUT,
        )
        return f"Restart requested for {ns}/{resource}."

    def delete_pod(self, session_name: str, namespace: str, pod: str) -> str:
        session = self.session(session_name)
        ns = validate_namespace(namespace) if namespace else session.namespace
        resource = validate_resource_name(pod)
        session.core.delete_namespaced_pod(resource, ns, _request_timeout=REQUEST_TIMEOUT)
        return f"Deleted pod {ns}/{resource}."

    def apply_manifest(self, session_name: str, manifest: str) -> str:
        session = self.session(session_name)
        docs = [doc for doc in yaml.safe_load_all(manifest) if doc is not None]
        if len(docs) != 1 or not isinstance(docs[0], dict):
            raise ValueError("Apply accepts exactly one YAML mapping.")
        doc = docs[0]
        kind = doc.get("kind")
        if kind == "Secret":
            raise ValueError("Refusing to apply Secret manifests. This server does not handle Secret values.")
        meta = doc.get("metadata") or {}
        name = validate_resource_name(str(meta.get("name") or ""))
        ns = validate_namespace(str(meta.get("namespace") or session.namespace))
        if kind == "ConfigMap":
            try:
                session.core.patch_namespaced_config_map(
                    name,
                    ns,
                    doc,
                    field_manager=FIELD_MANAGER,
                    _content_type=STRATEGIC_MERGE,
                    _request_timeout=REQUEST_TIMEOUT,
                )
                return f"Patched ConfigMap {ns}/{name}."
            except ApiException as exc:
                if exc.status != 404:
                    raise
                session.core.create_namespaced_config_map(ns, doc, _request_timeout=REQUEST_TIMEOUT)
                return f"Created ConfigMap {ns}/{name}."
        if kind == "Deployment":
            try:
                session.apps.patch_namespaced_deployment(
                    name,
                    ns,
                    doc,
                    field_manager=FIELD_MANAGER,
                    _content_type=STRATEGIC_MERGE,
                    _request_timeout=REQUEST_TIMEOUT,
                )
                return f"Patched Deployment {ns}/{name}."
            except ApiException as exc:
                if exc.status != 404:
                    raise
                session.apps.create_namespaced_deployment(ns, doc, _request_timeout=REQUEST_TIMEOUT)
                return f"Created Deployment {ns}/{name}."
        return "Apply supports Deployment and ConfigMap."
