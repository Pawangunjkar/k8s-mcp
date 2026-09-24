"""Kubernetes client wrapper for the k8s MCP server."""

from __future__ import annotations

import json
from dataclasses import dataclass

import yaml
from kubernetes import client, config
from kubernetes.client import ApiException


@dataclass
class K8sSession:
    name: str
    namespace: str
    context: str
    core: client.CoreV1Api
    apps: client.AppsV1Api


class K8sHub:
    def __init__(self) -> None:
        self.sessions: dict[str, K8sSession] = {}

    def connect(self, session_name: str, kubeconfig: str, context: str, namespace: str) -> str:
        if kubeconfig:
            config.load_kube_config(config_file=kubeconfig, context=context or None)
        else:
            try:
                config.load_kube_config(context=context or None)
            except Exception:
                config.load_incluster_config()
        current = context
        try:
            _contexts, active = config.list_kube_config_contexts()
            current = context or (active or {}).get("name", "")
        except Exception:
            current = context or "in-cluster"
        self.sessions[session_name] = K8sSession(
            name=session_name,
            namespace=namespace or "default",
            context=current,
            core=client.CoreV1Api(),
            apps=client.AppsV1Api(),
        )
        return f"Connected '{session_name}' context={current or 'in-cluster'} namespace={namespace or 'default'}."

    def session(self, name: str) -> K8sSession:
        if name not in self.sessions:
            raise KeyError(f"No Kubernetes session '{name}'. Call k8s_connect first.")
        return self.sessions[name]

    @staticmethod
    def _err(exc: Exception) -> str:
        if isinstance(exc, ApiException):
            return f"Kubernetes API {exc.status}: {exc.reason}"
        return str(exc)

    def list_pods(self, session_name: str, namespace: str, label: str) -> str:
        session = self.session(session_name)
        ns = namespace or session.namespace
        pods = session.core.list_namespaced_pod(ns, label_selector=label or None)
        rows = [
            {
                "name": item.metadata.name,
                "phase": item.status.phase,
                "ready": sum(1 for c in (item.status.container_statuses or []) if c.ready),
                "node": item.spec.node_name,
            }
            for item in pods.items
        ]
        return json.dumps({"namespace": ns, "pods": rows}, indent=2)

    def pod_logs(self, session_name: str, namespace: str, pod: str, container: str, tail: int) -> str:
        session = self.session(session_name)
        ns = namespace or session.namespace
        kwargs = {"name": pod, "namespace": ns, "tail_lines": max(1, min(tail, 1000))}
        if container:
            kwargs["container"] = container
        return session.core.read_namespaced_pod_log(**kwargs)

    def events(self, session_name: str, namespace: str) -> str:
        session = self.session(session_name)
        ns = namespace or session.namespace
        events = session.core.list_namespaced_event(ns)
        rows = [
            {"reason": item.reason, "message": item.message, "object": item.involved_object.name}
            for item in events.items[-30:]
        ]
        return json.dumps(rows, indent=2)

    def list_deployments(self, session_name: str, namespace: str) -> str:
        session = self.session(session_name)
        ns = namespace or session.namespace
        items = session.apps.list_namespaced_deployment(ns)
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
        ns = namespace or session.namespace
        body = {"spec": {"replicas": replicas}}
        session.apps.patch_namespaced_deployment_scale(name, ns, body)
        return f"Scaled {ns}/{name} to {replicas}."

    def restart(self, session_name: str, namespace: str, name: str) -> str:
        from datetime import datetime, timezone

        session = self.session(session_name)
        ns = namespace or session.namespace
        now = datetime.now(timezone.utc).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        session.apps.patch_namespaced_deployment(name, ns, body)
        return f"Restart requested for {ns}/{name}."

    def delete_pod(self, session_name: str, namespace: str, pod: str) -> str:
        session = self.session(session_name)
        ns = namespace or session.namespace
        session.core.delete_namespaced_pod(pod, ns)
        return f"Deleted pod {ns}/{pod}."

    def apply_manifest(self, session_name: str, manifest: str) -> str:
        session = self.session(session_name)
        doc = yaml.safe_load(manifest)
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        name = meta.get("name")
        ns = meta.get("namespace") or session.namespace
        if kind == "ConfigMap":
            try:
                session.core.patch_namespaced_config_map(name, ns, doc)
                return f"Patched ConfigMap {ns}/{name}."
            except ApiException as exc:
                if exc.status != 404:
                    raise
                session.core.create_namespaced_config_map(ns, doc)
                return f"Created ConfigMap {ns}/{name}."
        if kind == "Deployment":
            try:
                session.apps.patch_namespaced_deployment(name, ns, doc)
                return f"Patched Deployment {ns}/{name}."
            except ApiException as exc:
                if exc.status != 404:
                    raise
                session.apps.create_namespaced_deployment(ns, doc)
                return f"Created Deployment {ns}/{name}."
        return f"Apply supports Deployment and ConfigMap. Got kind={kind}."
