# Tool reference

Access `read-only` means the tool does not change the cluster. Access `mutating` means it does. Mutating rollout and rollback tools return `confirmation_required: true` until `confirm=true`.

| Tool | Access | Parameters | Behavior |
| --- | --- | --- | --- |
| `k8s_connect` | read-only | `kubeconfig`, `context`, `namespace`, `session_name` | Load kubeconfig or in-cluster config. Records cloud env and OpenShift API groups. |
| `k8s_disconnect` | read-only | `session_name` | Drop a session. |
| `k8s_status` | read-only | none | List sessions, context, namespace, provider, and OpenShift flag. |
| `k8s_get_cluster_overview` | read-only | `session_name` | Provider, detection source, Kubernetes and OpenShift versions, node Ready and pressure, namespaces, OpenShift projects, ResourceQuotas, API probe. |
| `k8s_list_deployments_and_configs` | read-only | `namespace=""`, `session_name` | Deployments, StatefulSets, DaemonSets, and DeploymentConfigs. Empty namespace lists all visible namespaces. |
| `k8s_get_pod_health_and_diagnostics` | read-only | `namespace`, `label_selector=""`, `session_name` | Phase, restart count, container state (`Running`, `CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`), warning events. |
| `k8s_get_container_logs` | read-only | `namespace`, `pod_name`, `container_name=""`, `tail_lines=200`, `previous=false`, `session_name` | Current or previous logs. `tail_lines` is 1 through 2000. |
| `k8s_manage_openshift_routes_and_services` | read-only | `namespace`, `session_name` | Routes (host, TLS termination, admitted status) and Services. Does not create Routes. |
| `k8s_check_rollout_status` | read-only | `namespace`, `resource_type`, `resource_name`, `timeout_seconds=60`, `session_name` | Poll replica sets or replication controllers until complete, failed, or timeout. Cap is 180 seconds. |
| `k8s_trigger_deployment_rollout` | mutating | `namespace`, `resource_type`, `resource_name`, `image_tag`, `confirm=false`, `container_name=""`, `session_name` | Preview, then on `confirm=true` set an immutable digest and roll. Rejects `latest`. Pauses active OpenShift ImageChange triggers. |
| `k8s_rollback_deployment` | mutating | `namespace`, `resource_type`, `resource_name`, `revision=0`, `confirm=false`, `session_name` | Preview history, then on `confirm=true` restore a prior revision. `revision=0` is the previous revision. |
| `k8s_list_namespaces` | read-only | `session_name` | Namespace names. |
| `k8s_list_pods` | read-only | `namespace=""`, `label=""`, `session_name` | Pod phase, ready count, node. |
| `k8s_pod_logs` | read-only | `pod`, `namespace=""`, `container=""`, `tail=100`, `session_name` | Legacy log tail. |
| `k8s_events` | read-only | `namespace=""`, `session_name` | Latest 30 events. |
| `k8s_list_deployments` | read-only | `namespace=""`, `session_name` | Deployment ready and desired counts. |
| `k8s_list_services` | read-only | `namespace=""`, `session_name` | Service type and cluster IP. |
| `k8s_list_nodes` | read-only | `session_name` | Node Ready condition. |
| `k8s_list_secret_names` | read-only | `namespace=""`, `session_name` | Secret names only. Values are not returned. |
| `k8s_scale` | mutating | `name`, `replicas`, `namespace=""`, `session_name` | Scale a Deployment. Call only when the user asked to scale. |
| `k8s_restart` | mutating | `name`, `namespace=""`, `session_name` | Restart a Deployment via `restartedAt`. |
| `k8s_delete_pod` | mutating | `pod`, `namespace=""`, `confirm=false`, `session_name` | Delete one pod. Requires `confirm=true`. |
| `k8s_apply` | mutating | `manifest_yaml`, `session_name` | Create or patch one Deployment or ConfigMap document. Secret manifests are refused. |

`resource_type` accepts `deployment`, `deploy`, `deploymentconfig`, `dc`.

`image_tag` accepts `sha256:` plus 64 lowercase hex characters, or `registry/name@sha256:` plus 64 lowercase hex characters. Tags without a digest, including `latest`, are rejected.
