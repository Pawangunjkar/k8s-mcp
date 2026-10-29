# MCP setup

Install [uv](https://docs.astral.sh/uv/) and point `KUBECONFIG` at the kubeconfig produced by `aws eks update-kubeconfig`, `az aks get-credentials`, `gcloud container clusters get-credentials`, or `oc login`. Do not put cloud passwords or registry pull credentials in these files.

Package name: `k8s-cloud-deploy-mcp`. Command: `k8s-cloud-deploy-mcp`.

Local checkout example:

```text
uvx --from C:/AI_Workspaces/Anti_Workspace/k8s-mcp k8s-cloud-deploy-mcp
```

## Cursor

`.cursor/mcp.json`

```json
{
  "mcpServers": {
    "k8s-cloud-deploy-mcp": {
      "command": "uvx",
      "args": ["--from", "k8s-cloud-deploy-mcp", "k8s-cloud-deploy-mcp"],
      "env": {
        "KUBECONFIG": "C:/Users/you/.kube/config",
        "K8S_CONTEXT": "dev",
        "K8S_NAMESPACE": "default",
        "CLOUD_PROVIDER": "generic_k8s"
      }
    }
  }
}
```

For EKS set `CLOUD_PROVIDER` to `aws_eks` and set `AWS_REGION`. For AKS set `azure_aks` and `AZURE_SUBSCRIPTION_ID`. For GKE set `gcp_gke` and `GCP_PROJECT`. For a self-managed OpenShift cluster set `openshift`.

Use a local checkout by replacing the args with `["--from", "C:/AI_Workspaces/Anti_Workspace/k8s-mcp", "k8s-cloud-deploy-mcp"]`.

## Claude Code

Project `.mcp.json` or the user MCP config:

```json
{
  "mcpServers": {
    "k8s-cloud-deploy-mcp": {
      "command": "uvx",
      "args": ["--from", "k8s-cloud-deploy-mcp", "k8s-cloud-deploy-mcp"],
      "env": {
        "KUBECONFIG": "/home/you/.kube/config",
        "K8S_CONTEXT": "dev",
        "CLOUD_PROVIDER": "openshift"
      }
    }
  }
}
```

## VS Code

`.vscode/mcp.json`

```json
{
  "servers": {
    "k8s-cloud-deploy-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "k8s-cloud-deploy-mcp", "k8s-cloud-deploy-mcp"],
      "env": {
        "KUBECONFIG": "C:/Users/you/.kube/config",
        "K8S_CONTEXT": "dev",
        "CLOUD_PROVIDER": "gcp_gke",
        "GCP_PROJECT": "example-project"
      }
    }
  }
}
```

The server speaks MCP over stdio. Read-only tools can be used as soon as the session connects. Rollout and rollback still require `confirm=true` after you approve the preview.
