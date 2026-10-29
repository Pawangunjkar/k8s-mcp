#!/usr/bin/env python3
"""Scan the skill and package, then write the Agensi zip, skill tar, sdist, and wheel."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "agensi_build" / "k8s-cloud-deploy-mcp-pro"
PACKAGE = ROOT / "k8s_cloud_deploy_mcp"
DIST_DIR = ROOT / "dist"
DIST = DIST_DIR / "k8s-cloud-deploy-mcp-pro.zip"
SKILL_TAR = DIST_DIR / "k8s-cloud-deploy-mcp-pro.tar.gz"

REQUIRED = (
    "SKILL.md",
    "scripts/verify_env.py",
    "reference/tools-reference.md",
    "reference/mcp-setup.md",
)
FORBIDDEN_NAMES = {
    ".env",
    ".env.local",
    "credentials.json",
    "kubeconfig",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
SUBPROCESS_CALLS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
}
CREDENTIAL_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(aws_secret_access_key|client_secret|private_key)\b\s*[:=]\s*['\"][^'\"]+['\"]"),
    re.compile(r"(?i)\b(api[_-]?key|password|secret|token)\b\s*=\s*['\"][^'\"]{6,}['\"]"),
)
CALL_PATTERNS = (
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"shell\s*=\s*True"),
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bos\.popen\s*\("),
)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def scan_credentials(text: str) -> list[str]:
    return [f"credential pattern {pattern.pattern}" for pattern in CREDENTIAL_PATTERNS if pattern.search(text)]


def scan_call_text(text: str) -> list[str]:
    return [f"forbidden text {pattern.pattern}" for pattern in CALL_PATTERNS if pattern.search(text)]


def scan_python(text: str, *, allow_subprocess: bool) -> list[str]:
    errors = scan_call_text(text)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return errors + [f"syntax error: {exc}"]
    saw_subprocess_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" and not allow_subprocess:
                    errors.append("subprocess import is not allowed in this file")
                if alias.name in {"pickle"}:
                    errors.append("pickle import is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess" and not allow_subprocess:
                errors.append("subprocess import is not allowed in this file")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in {"eval", "exec", "os.system", "os.popen"}:
                errors.append(f"forbidden call {name}")
            if name in SUBPROCESS_CALLS:
                saw_subprocess_run = saw_subprocess_run or name == "subprocess.run"
                if not allow_subprocess:
                    errors.append(f"forbidden call {name}")
                    continue
                shell = next((kw for kw in node.keywords if kw.arg == "shell"), None)
                if (
                    shell is None
                    or not isinstance(shell.value, ast.Constant)
                    or shell.value.value is not False
                ):
                    errors.append(f"{name} must pass shell=False")
    if allow_subprocess and not saw_subprocess_run:
        errors.append("expected a subprocess.run call with shell disabled")
    return errors


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        yield path


def scan_tree(root: Path, *, package: bool) -> list[str]:
    errors: list[str] = []
    for path in _iter_files(root):
        rel = path.relative_to(root).as_posix()
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"{rel}: forbidden file name")
            continue
        text = path.read_text(encoding="utf-8")
        label = f"{root.name}/{rel}"
        for finding in scan_credentials(text):
            errors.append(f"{label}: {finding}")
        if path.suffix == ".py":
            allow = package and path.name == "cli.py"
            for finding in scan_python(text, allow_subprocess=allow):
                errors.append(f"{label}: {finding}")
        else:
            for finding in scan_call_text(text):
                errors.append(f"{label}: {finding}")
    return errors


def _archive_members(skill: Path) -> list[tuple[Path, str]]:
    members: list[tuple[Path, str]] = []
    for path in _iter_files(skill):
        rel = path.relative_to(skill).as_posix()
        if ".." in rel.split("/"):
            raise ValueError(f"Refusing archive path {rel}")
        members.append((path, rel))
    return members


def build_zip(skill: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    members = _archive_members(skill)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, rel in members:
            archive.write(path, rel)
    return len(members)


def build_tar(skill: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    members = _archive_members(skill)
    with tarfile.open(dest, "w:gz") as archive:
        for path, rel in members:
            archive.add(path, arcname=rel)
    return len(members)


def build_python_distributions(dist_dir: Path) -> list[Path]:
    """Build the sdist tarball and wheel next to the skill archives."""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is not on PATH, so the sdist and wheel were not built.")
    completed = subprocess.run(
        [uv, "build", "--sdist", "--wheel", "--out-dir", str(dist_dir)],
        cwd=ROOT,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "uv build failed").strip()
        raise RuntimeError(detail)
    artifacts = sorted(dist_dir.glob("*.whl")) + sorted(
        path for path in dist_dir.glob("*.tar.gz") if path.name != SKILL_TAR.name
    )
    if not any(path.suffix == ".whl" for path in artifacts):
        raise RuntimeError("uv build finished without writing a wheel.")
    if not any(path.name.endswith(".tar.gz") and path.name != SKILL_TAR.name for path in artifacts):
        raise RuntimeError("uv build finished without writing a source tarball.")
    return artifacts


def main() -> int:
    missing = [item for item in REQUIRED if not (SKILL / item).is_file()]
    errors = [f"missing {item}" for item in missing]
    if SKILL.is_dir():
        errors.extend(scan_tree(SKILL, package=False))
    else:
        errors.append(f"missing skill directory {SKILL}")
    if PACKAGE.is_dir():
        errors.extend(scan_tree(PACKAGE, package=True))
    else:
        errors.append(f"missing package directory {PACKAGE}")
    if errors:
        for item in errors:
            print(f"SCAN FAIL {item}", file=sys.stderr)
        return 1
    count = build_zip(SKILL, DIST)
    tar_count = build_tar(SKILL, SKILL_TAR)
    try:
        distributions = build_python_distributions(DIST_DIR)
    except RuntimeError as exc:
        print(f"BUILD FAIL {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {DIST} ({count} files)")
    print(f"Wrote {SKILL_TAR} ({tar_count} files)")
    for artifact in distributions:
        print(f"Wrote {artifact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
