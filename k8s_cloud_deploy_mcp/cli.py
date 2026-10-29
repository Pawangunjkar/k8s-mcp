"""Fixed argv wrappers for kubectl and oc. The shell is never invoked."""

from __future__ import annotations

import os
import re
import subprocess

from k8s_cloud_deploy_mcp.guards import normalize_resource_type, validate_namespace, validate_resource_name

_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./:=@+-]{1,220}$")


def binary_name(path: str) -> str:
    name = os.path.basename(path).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def validate_cli_args(args: list[str]) -> None:
    for arg in args:
        if not _SAFE_ARG.fullmatch(arg):
            raise ValueError("A CLI argument failed validation and was not executed.")


def rollout_status_argv(
    binary_path: str,
    resource_type: str,
    namespace: str,
    name: str,
    timeout_seconds: int,
) -> list[str]:
    kind = normalize_resource_type(resource_type)
    ns = validate_namespace(namespace)
    resource = validate_resource_name(name)
    base = binary_name(binary_path)
    if base not in {"kubectl", "oc"}:
        raise ValueError("Only kubectl or oc can be executed.")
    if kind == "deploymentconfig" and base != "oc":
        raise ValueError("DeploymentConfig rollout status requires oc.")
    target = f"dc/{resource}" if kind == "deploymentconfig" else f"deployment/{resource}"
    args = ["rollout", "status", target, "-n", ns, f"--timeout={int(timeout_seconds)}s"]
    validate_cli_args(args)
    return [binary_path, *args]


def client_version_argv(binary_path: str) -> list[str]:
    base = binary_name(binary_path)
    if base == "kubectl":
        args = ["version", "--client=true", "-o", "json"]
    elif base == "oc":
        args = ["version", "--client"]
    else:
        raise ValueError("Only kubectl or oc can be executed.")
    validate_cli_args(args)
    return [binary_path, *args]


def run_cli(argv: list[str], timeout: int) -> dict:
    """Run a prebuilt kubectl or oc argv. shell is hardcoded off."""
    if not argv:
        raise ValueError("Refusing an empty command.")
    if timeout < 1 or timeout > 30:
        raise ValueError("CLI timeout must be between 1 and 30 seconds.")
    base = binary_name(argv[0])
    if base not in {"kubectl", "oc"}:
        raise ValueError("Only kubectl or oc can be executed.")
    validate_cli_args(argv[1:])
    command = [base, *argv[1:]]
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": True,
            "timed_out": True,
            "exit_code": None,
            "command": command,
            "stdout": "",
            "stderr": "The CLI command timed out.",
        }
    except OSError as exc:
        return {
            "available": True,
            "timed_out": False,
            "exit_code": None,
            "command": command,
            "stdout": "",
            "stderr": exc.strerror or "The CLI command could not start.",
        }
    return {
        "available": True,
        "timed_out": False,
        "exit_code": completed.returncode,
        "command": command,
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-2000:],
    }
