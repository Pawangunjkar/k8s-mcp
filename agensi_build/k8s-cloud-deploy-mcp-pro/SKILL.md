---
name: k8s-cloud-deploy-mcp-pro
version: 1.0.0
author: Pawan Gunjkar
category: devops-cloud
description: "Multi-cloud Kubernetes and OpenShift deployment playbook for discovery, route inspection, crash triage, immutable rollouts, and emergency rollback."
---

# Multi-Cloud and OpenShift Deployment Playbook

Use the `k8s-cloud-deploy-mcp` server for standard Kubernetes, Red Hat OpenShift, and cloud-managed clusters (AWS EKS, Azure AKS, GCP GKE). Load credentials from the operator's kubeconfig or from an in-cluster service account. Set `CLOUD_PROVIDER` to `aws_eks`, `azure_aks`, `gcp_gke`, `openshift`, or `generic_k8s`. `AWS_REGION`, `AZURE_SUBSCRIPTION_ID`, `GCP_PROJECT`, and `K8S_CONTEXT` select the target. OpenShift is also detected from the `apps.openshift.io/v1` and `route.openshift.io` API groups, including ROSA or ARO clusters where `CLOUD_PROVIDER` stays `aws_eks` or `azure_aks`.

Run `scripts/verify_env.py` before the first cluster call when a shell preflight is available. Secret values are never requested and never printed.

## Strict security guardrails

Read-only tools are autonomous. Use them for discovery and triage without asking for a second confirmation.

| Autonomous read-only tool | Use |
| --- | --- |
| `k8s_get_cluster_overview` | Provider, version, nodes, projects, quotas |
| `k8s_list_deployments_and_configs` | Workload inventory |
| `k8s_get_pod_health_and_diagnostics` | Phase, restarts, crash reasons, warning events |
| `k8s_get_container_logs` | Current or previous container logs |
| `k8s_manage_openshift_routes_and_services` | Routes, TLS termination, Services |
| `k8s_check_rollout_status` | Replica progress until success or timeout |
| `k8s_connect`, `k8s_status`, `k8s_list_namespaces`, `k8s_list_pods`, `k8s_pod_logs`, `k8s_events`, `k8s_list_deployments`, `k8s_list_services`, `k8s_list_nodes`, `k8s_list_secret_names` | Legacy session and inventory reads |

Mutating tools change running workloads. They are confirmation-gated.

| Mutating tool | Gate |
| --- | --- |
| `k8s_trigger_deployment_rollout` | Call with `confirm=false` first. Show the plan. Call with `confirm=true` only after the user explicitly approves that plan. |
| `k8s_rollback_deployment` | Same preview-then-approval gate. `revision=0` means the previous revision. |
| `k8s_delete_pod` | Requires `confirm=true`. |
| `k8s_scale`, `k8s_restart`, `k8s_apply` | Legacy writes. Call them only when the user explicitly asks for that change. |

A response with `confirmation_required: true` is a preview. It applied nothing. Do not treat it as a failure to route around, and do not set `confirm=true` on your own authority.

`k8s_trigger_deployment_rollout` accepts only an immutable digest: `sha256:<64 lowercase hex>` or `registry/name@sha256:<64 lowercase hex>`. A reference that uses the tag `latest` is rejected.

## Workflow 1: Multi-cluster discovery and health audit

1. Connect with `k8s_connect` when `k8s_status` shows no session. Pass the kubeconfig path and context the user named.
2. Call `k8s_get_cluster_overview`.
3. Record `cloud.provider`, `cloud.is_openshift`, `cloud.infrastructure`, `cloud.auth_gaps`, `version.kubernetes`, and `version.openshift`.
4. Report node `ready` versus `not_ready`, the active namespace or project list, and any ResourceQuota `hard` versus `used` that is exhausted.
5. If `auth_gaps` is non-empty, stop and ask the user to set the missing `AWS_REGION`, `AZURE_SUBSCRIPTION_ID`, or `GCP_PROJECT` before a rollout.

## Workflow 2: OpenShift Route and DeploymentConfig inspection

1. Confirm `cloud.is_openshift` is true from Workflow 1. On a generic cluster, say that Routes are unavailable and still list Services.
2. Call `k8s_list_deployments_and_configs` for the project. Include rows whose `kind` is `DeploymentConfig`.
3. Call `k8s_manage_openshift_routes_and_services` for the same namespace.
4. For each Route, report `host`, `tls_termination` (`edge`, `passthrough`, or `reencrypt`), `insecure_edge_termination_policy`, `target`, and `admitted`.
5. This tool is read-only. Do not create or edit Routes.

## Workflow 3: CrashLoopBackOff and OOMKilled pod debugging

1. Call `k8s_get_pod_health_and_diagnostics` with the namespace and a label selector when the user named an app.
2. Prioritize containers whose `state` or `last_terminated_reason` is `CrashLoopBackOff`, `OOMKilled`, or `ImagePullBackOff`, and any warning events for those pods.
3. Call `k8s_get_container_logs` for the failing container. Use `previous=true` when the current logs are empty or the previous instance was `OOMKilled`.
4. Summarize the reason, the restart count, the last warning event, and the relevant log lines. Do not propose a rollout from this workflow unless the user asks for one.

## Workflow 4: Immutable image rollout and status verification

1. From Workflow 2, identify `namespace`, `resource_type` (`deployment` or `deploymentconfig`), `resource_name`, and the container name when more than one container exists.
2. Require the user to supply an immutable digest. Do not invent one and do not pass `latest`.
3. Call `k8s_trigger_deployment_rollout` with `confirm=false`.
4. Show `previous_image`, `image`, `container`, and any warning that ImageChange triggers will be paused.
5. Wait for an explicit user approval of that plan.
6. Call the same tool with `confirm=true` and the same arguments.
7. Call `k8s_check_rollout_status` with `timeout_seconds` of 60 to 180. Read `replica_sets` or `replication_controllers`, `ready`, `updated`, and `desired`.
8. If `timed_out` or `failed` is true, go to Workflow 5 only after the user asks to roll back. Do not roll back automatically.

## Workflow 5: Emergency rollback procedure

1. Call `k8s_rollback_deployment` with `confirm=false`. Use `revision=0` for the previous revision, or the revision the user named.
2. Show `to_revision`, `known_revisions`, and the images that would be restored.
3. Wait for an explicit user approval.
4. Call the same tool with `confirm=true`.
5. Call `k8s_check_rollout_status` until `complete` is true or the timeout reports that the rollback did not finish.
6. Tell the user the restored revision and whether the new pods became ready.

## Environment

| Variable | Required when |
| --- | --- |
| `KUBECONFIG` | Outside a cluster, unless `~/.kube/config` exists |
| `K8S_CONTEXT` | More than one context is installed |
| `K8S_NAMESPACE` | The session default namespace |
| `CLOUD_PROVIDER` | Optional. One of `aws_eks`, `azure_aks`, `gcp_gke`, `openshift`, `generic_k8s` |
| `AWS_REGION` | `CLOUD_PROVIDER=aws_eks` |
| `AZURE_SUBSCRIPTION_ID` | `CLOUD_PROVIDER=azure_aks` |
| `GCP_PROJECT` | `CLOUD_PROVIDER=gcp_gke` |
| `KUBERNETES_SERVICE_HOST` | Set by Kubernetes for in-cluster configuration |

Cloud CLIs (`aws eks update-kubeconfig`, `az aks get-credentials`, `gcloud container clusters get-credentials`, `oc login`) are expected to have prepared the kubeconfig before this server starts. The server does not collect cloud passwords.
