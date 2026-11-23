"""
Regression tests for k8s-mcp fixes: image guard on k8s_apply, event ordering,
and metadata-only Secret listing.

Author: Pawan Gunjkar (pawangunjkar@gmail.com)
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ["K8S_DISABLE_AUTOCONNECT"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kubernetes.client import ApiException  # noqa: E402

from k8s_cloud_deploy_mcp.guards import validate_manifest_images  # noqa: E402
from k8s_cloud_deploy_mcp.kube import K8sHub, event_time  # noqa: E402


def deployment(*images: str, init: tuple[str, ...] = ()) -> dict:
    return {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "prod"},
        "spec": {"template": {"spec": {
            "containers": [{"name": f"c{i}", "image": image} for i, image in enumerate(images)],
            "initContainers": [{"name": f"i{i}", "image": image} for i, image in enumerate(init)],
        }}},
    }


def fake_hub(core=None, apps=None, api_client=None) -> K8sHub:
    hub = K8sHub()
    hub.sessions["default"] = SimpleNamespace(
        name="default",
        namespace="prod",
        core=core or mock.MagicMock(),
        apps=apps or mock.MagicMock(),
        api_client=api_client or mock.MagicMock(),
    )
    return hub


class ManifestImageGuardTests(unittest.TestCase):
    def test_pinned_images_pass(self):
        validate_manifest_images(deployment(
            "registry:5000/team/api:1.4.2",
            "ghcr.io/org/sidecar@sha256:" + "a" * 64,
            init=("busybox:1.36",),
        ))

    def test_latest_and_untagged_are_rejected(self):
        for image in ("nginx:latest", "nginx", "registry:5000/team/api", "ghcr.io/org/app:LATEST"):
            with self.subTest(image=image), self.assertRaises(ValueError):
                validate_manifest_images(deployment(image))
        with self.assertRaises(ValueError):
            validate_manifest_images(deployment("app:1.0", init=("migrate",)))

    def test_apply_refuses_latest_before_calling_the_api(self):
        apps = mock.MagicMock()
        hub = fake_hub(apps=apps)
        manifest = json.dumps(deployment("nginx:latest"))
        with self.assertRaises(ValueError):
            hub.apply_manifest("default", manifest)
        apps.patch_namespaced_deployment.assert_not_called()
        apps.create_namespaced_deployment.assert_not_called()

    def test_apply_pinned_deployment_patches(self):
        apps = mock.MagicMock()
        hub = fake_hub(apps=apps)
        out = hub.apply_manifest("default", json.dumps(deployment("nginx:1.27.0")))
        self.assertIn("Patched Deployment prod/api", out)


class EventOrderingTests(unittest.TestCase):
    def test_newest_events_are_returned_in_time_order(self):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        items = []
        for minute in (40, 5, 30, 10):
            items.append(SimpleNamespace(
                last_timestamp=base + timedelta(minutes=minute),
                event_time=None,
                first_timestamp=None,
                metadata=SimpleNamespace(creation_timestamp=None),
                type="Warning",
                reason=f"r{minute}",
                involved_object=SimpleNamespace(name="pod-a"),
                count=1,
                message="m",
            ))
        # An event that only carries eventTime (events.k8s.io style).
        items.append(SimpleNamespace(
            last_timestamp=None,
            event_time=base + timedelta(minutes=50),
            first_timestamp=None,
            metadata=SimpleNamespace(creation_timestamp=None),
            type="Normal",
            reason="r50",
            involved_object=SimpleNamespace(name="pod-b"),
            count=None,
            message="m",
        ))
        core = mock.MagicMock()
        core.list_namespaced_event.return_value = SimpleNamespace(items=items)
        rows = json.loads(fake_hub(core=core).events("default", "prod"))
        self.assertEqual([r["reason"] for r in rows], ["r5", "r10", "r30", "r40", "r50"])
        self.assertTrue(rows[-1]["time"].startswith("2024-01-01T00:50"))

    def test_event_time_fallback(self):
        stamp = datetime(2024, 5, 1)
        item = SimpleNamespace(last_timestamp=None, event_time=None, first_timestamp=None,
                               metadata=SimpleNamespace(creation_timestamp=stamp))
        self.assertEqual(event_time(item), stamp.replace(tzinfo=timezone.utc))


class SecretNameTests(unittest.TestCase):
    def test_metadata_only_request_is_used(self):
        api_client = mock.MagicMock()
        api_client.call_api.return_value = {
            "kind": "PartialObjectMetadataList",
            "items": [{"metadata": {"name": "tls"}}, {"metadata": {"name": "db"}}],
        }
        core = mock.MagicMock()
        names = fake_hub(core=core, api_client=api_client).secret_names("default", "prod")
        self.assertEqual(names, ["db", "tls"])
        headers = api_client.call_api.call_args.kwargs["header_params"]
        self.assertIn("PartialObjectMetadataList", headers["Accept"])
        core.list_namespaced_secret.assert_not_called()

    def test_falls_back_on_old_api_servers(self):
        api_client = mock.MagicMock()
        api_client.call_api.side_effect = ApiException(status=406)
        core = mock.MagicMock()
        core.list_namespaced_secret.return_value = SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name="b")), SimpleNamespace(metadata=SimpleNamespace(name="a"))]
        )
        names = fake_hub(core=core, api_client=api_client).secret_names("default", "prod")
        self.assertEqual(names, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
