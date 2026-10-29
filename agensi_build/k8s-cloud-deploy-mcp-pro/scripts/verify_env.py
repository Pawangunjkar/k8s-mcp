#!/usr/bin/env python3
"""Preflight check for kubeconfig and cloud auth environment variables.

Standard library only. It prints variable names and path checks. It does not
read kubeconfig contents, start a shell, or contact the cluster.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ALLOWED = frozenset({"aws_eks", "azure_aks", "gcp_gke", "openshift", "generic_k8s"})


def main() -> int:
    failures: list[str] = []
    notes: list[str] = []
    provider = os.environ.get("CLOUD_PROVIDER", "").strip().lower()
    if provider and provider not in ALLOWED:
        allowed = ", ".join(sorted(ALLOWED))
        failures.append(f"CLOUD_PROVIDER must be one of: {allowed}.")
    elif provider:
        notes.append(f"CLOUD_PROVIDER={provider}.")
    else:
        notes.append("CLOUD_PROVIDER is unset. The server detects the provider from the cluster API.")

    if provider == "aws_eks" and not os.environ.get("AWS_REGION", "").strip():
        failures.append("AWS_REGION is required when CLOUD_PROVIDER=aws_eks.")
    if provider == "azure_aks" and not os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip():
        failures.append("AZURE_SUBSCRIPTION_ID is required when CLOUD_PROVIDER=azure_aks.")
    if provider == "gcp_gke" and not os.environ.get("GCP_PROJECT", "").strip():
        failures.append("GCP_PROJECT is required when CLOUD_PROVIDER=gcp_gke.")

    context = os.environ.get("K8S_CONTEXT", "").strip()
    if context:
        notes.append(f"K8S_CONTEXT={context}.")
    else:
        notes.append("K8S_CONTEXT is unset. The active kubeconfig context will be used.")

    kubeconfig = os.environ.get("KUBECONFIG", "").strip()
    in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST", "").strip())
    if kubeconfig:
        missing = [str(Path(entry).expanduser()) for entry in kubeconfig.split(os.pathsep) if entry.strip() and not Path(entry).expanduser().is_file()]
        if missing:
            failures.append("KUBECONFIG entries are not files: " + ", ".join(missing))
        else:
            notes.append("KUBECONFIG points at an existing file.")
    elif in_cluster:
        notes.append("KUBERNETES_SERVICE_HOST is set. In-cluster configuration can be used.")
    else:
        default = Path.home() / ".kube" / "config"
        if default.is_file():
            notes.append("Default kubeconfig exists at ~/.kube/config.")
        else:
            failures.append("No KUBECONFIG file, no default ~/.kube/config, and no in-cluster service host.")

    for binary in ("kubectl", "oc"):
        if shutil.which(binary):
            notes.append(f"{binary} is on PATH.")
        else:
            notes.append(f"{binary} is not on PATH. The Kubernetes Python client is still used.")

    for line in notes:
        print(f"INFO {line}")
    for line in failures:
        print(f"FAIL {line}")
    if failures:
        print("FAIL preflight failed.")
        return 1
    print("PASS preflight completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
