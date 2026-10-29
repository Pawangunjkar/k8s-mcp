"""
Kubernetes, OpenShift, and cloud-managed cluster MCP server.

Author: Pawan Gunjkar (pawangunjkar@gmail.com)

Read-only diagnostics can run without a second confirmation.
k8s_trigger_deployment_rollout and k8s_rollback_deployment change workloads and
require confirm=true after the user explicitly approves the plan.
Secret values are never returned.
"""

from __future__ import annotations

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

from k8s_cloud_deploy_mcp.kube import K8sHub
from k8s_cloud_deploy_mcp.output import dump, fail
from k8s_cloud_deploy_mcp.rollout import check_rollout, rollback, trigger_rollout

mcp = FastMCP(
    "k8s-cloud-deploy",
    instructions=(
        "Multi-cloud Kubernetes and OpenShift MCP for EKS, AKS, GKE, OpenShift, and generic clusters. "
        "Call k8s_connect first, or set KUBECONFIG, K8S_CONTEXT, and CLOUD_PROVIDER. "
        "Read-only tools: k8s_get_cluster_overview, k8s_list_deployments_and_configs, "
        "k8s_get_pod_health_and_diagnostics, k8s_get_container_logs, "
        "k8s_manage_openshift_routes_and_services, k8s_check_rollout_status, plus the legacy list and log tools. "
        "Mutating tools k8s_trigger_deployment_rollout and k8s_rollback_deployment require confirm=true "
        "only after the user explicitly approves the preview. Image updates must be sha256 digests. "
        "The tag latest is rejected. Do not request Secret data; this server lists secret names only."
    ),
)

hub = K8sHub()


def bootstrap() -> None:
    if os.environ.get("K8S_DISABLE_AUTOCONNECT") == "1":
        return
    if not any(
        os.environ.get(name)
        for name in ("KUBECONFIG", "K8S_NAMESPACE", "K8S_CONTEXT", "KUBERNETES_SERVICE_HOST", "CLOUD_PROVIDER")
    ):
        return
    print("Auto-connecting Kubernetes session...", file=sys.stderr)
    try:
        print(
            hub.connect(
                session_name=os.environ.get("K8S_SESSION", "default"),
                kubeconfig=os.environ.get("KUBECONFIG", ""),
                context=os.environ.get("K8S_CONTEXT", ""),
                namespace=os.environ.get("K8S_NAMESPACE", "default"),
            ),
            file=sys.stderr,
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
    """Load a kubeconfig context, or in-cluster config when no kubeconfig is available.

    CLOUD_PROVIDER, AWS_REGION, AZURE_SUBSCRIPTION_ID, GCP_PROJECT, and K8S_CONTEXT
    are read from the environment. OpenShift is detected from the API groups.
    """
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
    return "\n".join(
        f"{name}: context={item.context} ns={item.namespace} provider={item.provider} openshift={item.is_openshift}"
        for name, item in hub.sessions.items()
    )


@mcp.tool()
def k8s_get_cluster_overview(session_name: str = "default") -> str:
    """Read-only audit of provider, version, node health, namespaces or projects, and quotas.

    Detects OpenShift from apps.openshift.io/v1 or route.openshift.io.
    Uses CLOUD_PROVIDER (aws_eks, azure_aks, gcp_gke, openshift, generic_k8s),
    AWS_REGION, AZURE_SUBSCRIPTION_ID, GCP_PROJECT, and K8S_CONTEXT.
    """
    try:
        return dump(hub.cluster_overview(session_name))
    except Exception as exc:
        return fail("k8s_get_cluster_overview", exc)


@mcp.tool()
def k8s_list_deployments_and_configs(namespace: str = "", session_name: str = "default") -> str:
    """Read-only list of Deployments, StatefulSets, DaemonSets, and OpenShift DeploymentConfigs.

    An empty namespace lists every namespace the credentials can read.
    """
    try:
        return dump(hub.deployments_and_configs(session_name, namespace))
    except Exception as exc:
        return fail("k8s_list_deployments_and_configs", exc)


@mcp.tool()
def k8s_get_pod_health_and_diagnostics(
    namespace: str,
    label_selector: str = "",
    session_name: str = "default",
) -> str:
    """Read-only pod phases, restart counts, container reasons, and recent warning events.

    Container reasons include Running, CrashLoopBackOff, OOMKilled, and ImagePullBackOff.
    """
    try:
        return dump(hub.pod_health(session_name, namespace, label_selector))
    except Exception as exc:
        return fail("k8s_get_pod_health_and_diagnostics", exc)


@mcp.tool()
def k8s_get_container_logs(
    namespace: str,
    pod_name: str,
    container_name: str = "",
    tail_lines: int = 200,
    previous: bool = False,
    session_name: str = "default",
) -> str:
    """Read-only current or previous container logs for crash triage."""
    try:
        return dump(hub.container_logs(session_name, namespace, pod_name, container_name, tail_lines, previous))
    except Exception as exc:
        return fail("k8s_get_container_logs", exc)


@mcp.tool()
def k8s_manage_openshift_routes_and_services(namespace: str, session_name: str = "default") -> str:
    """Read-only OpenShift Routes, Services, TLS termination, and external hosts.

    This tool does not create or modify Routes. On a non-OpenShift cluster it still lists Services.
    """
    try:
        return dump(hub.routes_and_services(session_name, namespace))
    except Exception as exc:
        return fail("k8s_manage_openshift_routes_and_services", exc)


@mcp.tool()
def k8s_trigger_deployment_rollout(
    namespace: str,
    resource_type: str,
    resource_name: str,
    image_tag: str,
    confirm: bool = False,
    container_name: str = "",
    session_name: str = "default",
) -> str:
    """Update a Deployment or DeploymentConfig to an immutable digest and trigger a rollout.

    Confirmation-gated mutating tool. The default confirm=false returns a plan and changes nothing.
    Call again with confirm=true only after the user explicitly approves that plan.
    image_tag must be sha256:<64 hex> or registry/name@sha256:<64 hex>. The tag latest is rejected.
    resource_type is deployment or deploymentconfig.
    """
    try:
        return dump(
            trigger_rollout(
                hub.session(session_name),
                namespace,
                resource_type,
                resource_name,
                image_tag,
                confirm,
                container_name,
            )
        )
    except Exception as exc:
        return fail("k8s_trigger_deployment_rollout", exc, access="mutating")


@mcp.tool()
def k8s_check_rollout_status(
    namespace: str,
    resource_type: str,
    resource_name: str,
    timeout_seconds: int = 60,
    session_name: str = "default",
) -> str:
    """Read-only poll of replica-set or replication-controller progress until success or timeout.

    timeout_seconds is capped at 180. API status is authoritative. kubectl or oc is consulted
    only as a short secondary check when that binary is installed.
    """
    try:
        return dump(check_rollout(hub.session(session_name), namespace, resource_type, resource_name, timeout_seconds))
    except Exception as exc:
        return fail("k8s_check_rollout_status", exc)


@mcp.tool()
def k8s_rollback_deployment(
    namespace: str,
    resource_type: str,
    resource_name: str,
    revision: int = 0,
    confirm: bool = False,
    session_name: str = "default",
) -> str:
    """Roll a Deployment or DeploymentConfig back to the previous or requested revision.

    Confirmation-gated mutating tool. revision=0 selects the previous revision.
    The default confirm=false returns the history and changes nothing.
    Call again with confirm=true only after the user explicitly approves that plan.
    """
    try:
        return dump(rollback(hub.session(session_name), namespace, resource_type, resource_name, revision, confirm))
    except Exception as exc:
        return fail("k8s_rollback_deployment", exc, access="mutating")


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
    """Scale a Deployment. This mutates the cluster. Use it only when the user asked to scale."""
    try:
        return hub.scale(session_name, namespace, name, replicas)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def k8s_restart(name: str, namespace: str = "", session_name: str = "default") -> str:
    """Roll a Deployment by setting restartedAt. This mutates the cluster."""
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
    """Create or patch a Deployment or ConfigMap from a single YAML document. Secret manifests are refused."""
    try:
        return hub.apply_manifest(session_name, manifest_yaml)
    except Exception as exc:
        return f"Error: {exc}"


def main() -> None:
    mcp.run(transport="stdio")


bootstrap()


if __name__ == "__main__":
    main()
