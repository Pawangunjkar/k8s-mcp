"""Unit tests for provider detection, digest guards, and the Agensi package scan."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

os.environ["K8S_DISABLE_AUTOCONNECT"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from k8s_cloud_deploy_mcp.cli import rollout_status_argv
from k8s_cloud_deploy_mcp.cloud import (
    api_is_openshift,
    auth_gaps,
    flatten_api_groups,
    infrastructure_of,
    resolve_provider,
    safe_version_url,
)
from k8s_cloud_deploy_mcp.diagnostics import (
    deployment_progress,
    describe_container,
    poll_until,
    stamp_template,
)
from k8s_cloud_deploy_mcp.guards import (
    apply_digest,
    confirmation_preview,
    select_revision,
    validate_image_reference,
)
from k8s_cloud_deploy_mcp.output import dump, fail
from k8s_cloud_deploy_mcp.rollout import trigger_rollout

DIGEST = "sha256:" + ("a" * 64)
SPEC = importlib.util.spec_from_file_location("build_agensi_zip", ROOT / "scripts" / "build_agensi_zip.py")
BUILD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BUILD)
VERIFY_SPEC = importlib.util.spec_from_file_location(
    "verify_env",
    ROOT / "agensi_build" / "k8s-cloud-deploy-mcp-pro" / "scripts" / "verify_env.py",
)
VERIFY = importlib.util.module_from_spec(VERIFY_SPEC)
assert VERIFY_SPEC.loader is not None
VERIFY_SPEC.loader.exec_module(VERIFY)


class CloudTests(unittest.TestCase):
    def test_openshift_groups(self) -> None:
        groups = flatten_api_groups([("apps.openshift.io", ["v1"]), ("route.openshift.io", ["v1"])])
        self.assertTrue(api_is_openshift(groups))
        self.assertIn("apps.openshift.io/v1", groups)
        self.assertFalse(api_is_openshift(["apps/v1", "route.example.com/v1"]))

    def test_environment_provider_wins_on_rosa(self) -> None:
        provider, source = resolve_provider("aws_eks", True, "aws")
        self.assertEqual((provider, source), ("aws_eks", "environment"))

    def test_openshift_detected_without_env(self) -> None:
        provider, source = resolve_provider("", True, "aws")
        self.assertEqual((provider, source), ("openshift", "api-groups"))

    def test_infrastructure_tokens(self) -> None:
        self.assertEqual(infrastructure_of("tasks", "https://10.0.0.1", []), "unknown")
        self.assertEqual(infrastructure_of("dev-aks-1", "", []), "azure")
        self.assertEqual(infrastructure_of("", "https://x.eks.amazonaws.com", []), "aws")
        self.assertEqual(infrastructure_of("", "", ["gce://proj/zone/node"]), "gcp")

    def test_auth_gaps(self) -> None:
        self.assertEqual(auth_gaps("aws_eks", "", "", ""), ["AWS_REGION is unset"])
        self.assertEqual(auth_gaps("gcp_gke", "", "", "demo"), [])

    def test_version_url_drops_userinfo(self) -> None:
        url = safe_version_url("https://user:secret-pass@10.1.2.3:6443/api?q=1")
        self.assertEqual(url, "https://10.1.2.3:6443/version")
        self.assertNotIn("secret-pass", url)
        self.assertEqual(safe_version_url("https://[::1]:6443"), "https://[::1]:6443/version")


class GuardTests(unittest.TestCase):
    def test_rejects_latest_and_plain_tags(self) -> None:
        for value in ("latest", ":latest", "registry.example.com/app:latest", "app:1.2.3"):
            with self.assertRaises(ValueError):
                validate_image_reference(value)

    def test_apply_digest_keeps_registry_port(self) -> None:
        updated = apply_digest("registry.example.com:5000/team/app:1.2.3", DIGEST)
        self.assertEqual(updated, f"registry.example.com:5000/team/app@{DIGEST}")
        full = f"registry.example.com/app@{DIGEST}"
        self.assertEqual(apply_digest("ignored:1", full), full)

    def test_latest_with_digest_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_image_reference(f"registry.example.com/app:latest@{DIGEST}")

    def test_revision_selection(self) -> None:
        self.assertEqual(select_revision([1, 2, 4], 0), 2)
        with self.assertRaises(ValueError):
            select_revision([4], 0)
        with self.assertRaises(ValueError):
            select_revision([2, 4], 4)

    def test_rollout_argv_rejects_metacharacters(self) -> None:
        with self.assertRaises(ValueError):
            rollout_status_argv("kubectl", "deployment", "default", "app;rm", 5)
        argv = rollout_status_argv(r"C:\Tools\kubectl.exe", "deployment", "default", "api", 5)
        self.assertEqual(argv[1:], ["rollout", "status", "deployment/api", "-n", "default", "--timeout=5s"])

    def test_preview_is_not_an_apply(self) -> None:
        payload = confirmation_preview({"namespace": "default", "image": f"app@{DIGEST}"})
        payload["tool"] = "k8s_trigger_deployment_rollout"
        report = json.loads(dump(payload))
        self.assertFalse(report["applied"])
        self.assertTrue(report["confirmation_required"])

    def test_bad_digest_never_touches_a_client(self) -> None:
        with self.assertRaises(ValueError):
            trigger_rollout(None, "default", "deployment", "api", "latest", False)

    def test_api_error_hides_body(self) -> None:
        class ApiException(Exception):
            def __init__(self) -> None:
                super().__init__("body must-not-leak")
                self.status = 403
                self.reason = "Forbidden"

        text = fail("k8s_get_cluster_overview", ApiException())
        self.assertIn("403", text)
        self.assertNotIn("must-not-leak", text)


class DiagnosticTests(unittest.TestCase):
    def test_container_states(self) -> None:
        waiting = SimpleNamespace(
            name="api",
            ready=False,
            restart_count=4,
            state=SimpleNamespace(
                running=None,
                waiting=SimpleNamespace(reason="CrashLoopBackOff", message="back-off"),
                terminated=None,
            ),
            last_state=SimpleNamespace(terminated=SimpleNamespace(reason="OOMKilled")),
        )
        report = describe_container(waiting)
        self.assertEqual(report["state"], "CrashLoopBackOff")
        self.assertEqual(report["last_terminated_reason"], "OOMKilled")
        self.assertTrue(report["problem"])
        running = SimpleNamespace(
            name="api",
            ready=True,
            restart_count=0,
            state=SimpleNamespace(running=SimpleNamespace(), waiting=None, terminated=None),
            last_state=None,
        )
        self.assertEqual(describe_container(running)["state"], "Running")

    def test_rollout_progress_and_timeout(self) -> None:
        done = deployment_progress(
            3,
            3,
            2,
            2,
            2,
            2,
            0,
            [SimpleNamespace(type="Available", status="True", reason="MinimumReplicasAvailable", message="")],
        )
        self.assertTrue(done["complete"])
        failed = deployment_progress(
            2,
            2,
            2,
            1,
            1,
            1,
            1,
            [SimpleNamespace(type="Progressing", status="False", reason="ProgressDeadlineExceeded", message="")],
        )
        self.assertTrue(failed["failed"])
        clocks = iter([0, 0, 5])

        def clock() -> int:
            return next(clocks)

        result = poll_until(lambda: {"complete": False, "failed": False}, 5, 5, lambda _seconds: None, clock)
        self.assertTrue(result["timed_out"])
        immediate = poll_until(lambda: {"complete": True}, 30, 2, lambda _seconds: (_ for _ in ()).throw(AssertionError("slept")), lambda: 0)
        self.assertTrue(immediate["complete"])
        self.assertFalse(immediate["timed_out"])

    def test_stamp_drops_controller_metadata(self) -> None:
        stamped = stamp_template(
            {"metadata": {"uid": "abc", "resourceVersion": "9", "annotations": {"keep": "yes"}}, "spec": {}},
            "2026-10-29T00:00:00+00:00",
        )
        self.assertNotIn("uid", stamped["metadata"])
        self.assertEqual(stamped["metadata"]["annotations"]["keep"], "yes")
        self.assertIn("k8s-cloud-deploy-mcp/rollback-at", stamped["metadata"]["annotations"])


class PackageTests(unittest.TestCase):
    def test_server_registers_enterprise_tools(self) -> None:
        from k8s_cloud_deploy_mcp.server import mcp

        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        expected = {
            "k8s_get_cluster_overview",
            "k8s_list_deployments_and_configs",
            "k8s_get_pod_health_and_diagnostics",
            "k8s_get_container_logs",
            "k8s_manage_openshift_routes_and_services",
            "k8s_trigger_deployment_rollout",
            "k8s_check_rollout_status",
            "k8s_rollback_deployment",
        }
        self.assertTrue(expected <= names)

    def test_scanner_rejects_shell_and_credentials(self) -> None:
        self.assertTrue(BUILD.scan_python("import subprocess\nsubprocess.run(['kubectl'], shell=True)\n", allow_subprocess=True))
        self.assertTrue(BUILD.scan_python("eval('1')\n", allow_subprocess=False))
        self.assertTrue(BUILD.scan_credentials('token = "supersecretvalue"\n'))
        self.assertFalse(BUILD.scan_python((ROOT / "k8s_cloud_deploy_mcp" / "cli.py").read_text(encoding="utf-8"), allow_subprocess=True))

    def test_verify_env_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kubeconfig = Path(tmp) / "config"
            kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
            missing = str(Path(tmp) / "missing")
            base = {
                "KUBECONFIG": str(kubeconfig),
                "KUBERNETES_SERVICE_HOST": "",
                "CLOUD_PROVIDER": "",
                "AWS_REGION": "",
                "AZURE_SUBSCRIPTION_ID": "",
                "GCP_PROJECT": "",
                "K8S_CONTEXT": "dev",
            }
            with mock.patch.dict(os.environ, base, clear=False):
                self.assertEqual(VERIFY.main(), 0)
            with mock.patch.dict(os.environ, {**base, "KUBECONFIG": missing}, clear=False):
                self.assertEqual(VERIFY.main(), 1)
            with mock.patch.dict(
                os.environ,
                {**base, "CLOUD_PROVIDER": "aws_eks", "AWS_REGION": ""},
                clear=False,
            ):
                self.assertEqual(VERIFY.main(), 1)
            with mock.patch.dict(os.environ, {**base, "CLOUD_PROVIDER": "not-a-cloud"}, clear=False):
                self.assertEqual(VERIFY.main(), 1)
        self.assertEqual(BUILD.main(), 0)
        archive = ROOT / "dist" / "k8s-cloud-deploy-mcp-pro.zip"
        skill_tar = ROOT / "dist" / "k8s-cloud-deploy-mcp-pro.tar.gz"
        self.assertTrue(archive.is_file())
        self.assertTrue(skill_tar.is_file())
        self.assertTrue(any((ROOT / "dist").glob("*.whl")))
        self.assertTrue(any(path.name.startswith("k8s_cloud_deploy_mcp-") for path in (ROOT / "dist").glob("*.tar.gz")))
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
        self.assertIn("SKILL.md", names)
        self.assertIn("scripts/verify_env.py", names)
        self.assertIn("reference/tools-reference.md", names)
        self.assertIn("reference/mcp-setup.md", names)


if __name__ == "__main__":
    unittest.main()
