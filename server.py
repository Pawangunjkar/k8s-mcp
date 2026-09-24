"""
Kubernetes MCP Server.

Author: Pawan Gunjkar (pawangunjkar@gmail.com)

Read pods, logs, events, deployments, and services.
Write scale, restart, delete pod, and apply Deployment or ConfigMap manifests.
Secret values are not returned.
"""

from __future__ import annotations

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

from k8s_client import K8sHub

mcp = FastMCP(
    "k8s",
    instructions=(
        "Kubernetes MCP. Call k8s_connect first, or set KUBECONFIG and K8S_NAMESPACE. "
        "Read tools: namespaces, pods, logs, events, deployments, services, nodes. "
        "Write tools: k8s_scale, k8s_restart, k8s_delete_pod (confirm=true), k8s_apply. "
        "Do not request Secret data; this server lists secret names only."
    ),
)

hub = K8sHub()

if os.environ.get("KUBECONFIG") or os.environ.get("K8S_NAMESPACE"):
    print("Auto-connecting Kubernetes session...", file=sys.stderr)
    try:
        hub.connect(
            session_name=os.environ.get("K8S_SESSION", "default"),
            kubeconfig=os.environ.get("KUBECONFIG", ""),
            context=os.environ.get("K8S_CONTEXT", ""),
            namespace=os.environ.get("K8S_NAMESPACE", "default"),
        )
    except Exception as exc:
        print(f"Auto-connect failed: {exc}", file=sys.stderr)


@mcp.tool()
def k8s_connect(
    kubeconfig: str = "",
    context: str = "",
    namespace: str = "default",
    session_name: str = "default",
) -> str:
    """Load a kubeconfig context, or in-cluster config when no kubeconfig is available."""
    try:
        return hub.connect(session_name, kubeconfig, context, namespace)
    except Exception as exc:
        return f"Connect failed: {exc}"


@mcp.tool()
def k8s_disconnect(session_name: str = "default") -> str:
    """Drop a Kubernetes session."""
    hub.sessions.pop(session_name, None)
    return f"Disconnected '{session_name}'."


@mcp.tool()
def k8s_status() -> str:
    """List Kubernetes sessions."""
    if not hub.sessions:
        return "No sessions. Call k8s_connect."
    return "\n".join(f"{name}: context={s.context} ns={s.namespace}" for name, s in hub.sessions.items())


@mcp.tool()
def k8s_list_namespaces(session_name: str = "default") -> str:
    """List namespace names."""
    try:
        items = hub.session(session_name).core.list_namespace()
        return json.dumps([item.metadata.name for item in items.items], indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_list_pods(namespace: str = "", label: str = "", session_name: str = "default") -> str:
    """List pods with phase, ready count, and node."""
    try:
        return hub.list_pods(session_name, namespace, label)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_pod_logs(
    pod: str,
    namespace: str = "",
    container: str = "",
    tail: int = 100,
    session_name: str = "default",
) -> str:
    """Read the tail of a pod log."""
    try:
        return hub.pod_logs(session_name, namespace, pod, container, tail)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_events(namespace: str = "", session_name: str = "default") -> str:
    """Show the latest 30 namespaced events."""
    try:
        return hub.events(session_name, namespace)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_list_deployments(namespace: str = "", session_name: str = "default") -> str:
    """List deployments and ready replica counts."""
    try:
        return hub.list_deployments(session_name, namespace)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_list_services(namespace: str = "", session_name: str = "default") -> str:
    """List services, type, and cluster IP."""
    try:
        session = hub.session(session_name)
        ns = namespace or session.namespace
        items = session.core.list_namespaced_service(ns)
        rows = [
            {"name": item.metadata.name, "type": item.spec.type, "clusterIP": item.spec.cluster_ip}
            for item in items.items
        ]
        return json.dumps({"namespace": ns, "services": rows}, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_list_nodes(session_name: str = "default") -> str:
    """List nodes and Ready condition."""
    try:
        items = hub.session(session_name).core.list_node()
        rows = []
        for item in items.items:
            ready = "Unknown"
            for condition in item.status.conditions or []:
                if condition.type == "Ready":
                    ready = condition.status
            rows.append({"name": item.metadata.name, "ready": ready})
        return json.dumps(rows, indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_list_secret_names(namespace: str = "", session_name: str = "default") -> str:
    """List Secret names only. Values are not returned."""
    try:
        session = hub.session(session_name)
        ns = namespace or session.namespace
        items = session.core.list_namespaced_secret(ns)
        return json.dumps([item.metadata.name for item in items.items], indent=2)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_scale(name: str, replicas: int, namespace: str = "", session_name: str = "default") -> str:
    """Scale a Deployment."""
    try:
        return hub.scale(session_name, namespace, name, replicas)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_restart(name: str, namespace: str = "", session_name: str = "default") -> str:
    """Roll a Deployment by setting restartedAt."""
    try:
        return hub.restart(session_name, namespace, name)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_delete_pod(pod: str, namespace: str = "", confirm: bool = False, session_name: str = "default") -> str:
    """Delete one pod so the controller recreates it. confirm=true is required."""
    if not confirm:
        return "Pod delete needs confirm=true."
    try:
        return hub.delete_pod(session_name, namespace, pod)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_apply(manifest_yaml: str, session_name: str = "default") -> str:
    """Create or patch a Deployment or ConfigMap from YAML."""
    try:
        return hub.apply_manifest(session_name, manifest_yaml)
    except Exception as exc:
        return f"Error: {exc}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
