"""Backward-compatible import path. The client lives in k8s_cloud_deploy_mcp.kube."""

from k8s_cloud_deploy_mcp.kube import K8sHub, K8sSession

__all__ = ["K8sHub", "K8sSession"]
