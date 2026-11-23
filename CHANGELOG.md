# Changelog

## [Unreleased]

### Fixed
- **`k8s_apply` bypassed the image guard.** Deployment manifests whose containers or init containers use `latest`
  or no tag are rejected, matching `k8s_trigger_deployment_rollout`.
- **`k8s_events` returned an arbitrary 30 events.** Events are sorted by time (lastTimestamp / eventTime), so the
  newest 30 are returned, now with timestamps and counts.
- **`k8s_list_secret_names` downloaded secret values.** It uses a metadata-only (`PartialObjectMetadataList`)
  request, so secret data never leaves the API server; very old clusters fall back to a normal list.

### Added
- `tests/test_k8s_bugfixes.py`: 8 regression tests.
