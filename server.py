"""Backward-compatible launcher. The implementation is k8s_cloud_deploy_mcp.server."""

from k8s_cloud_deploy_mcp.server import main, mcp

__all__ = ["main", "mcp"]

if __name__ == "__main__":
    main()
