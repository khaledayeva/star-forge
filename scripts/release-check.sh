#!/bin/sh
# Star Forge public release gate.
set -e
cd "$(dirname "$0")/.."

mode=${1:-full}
case "$mode" in
    full|--metadata-only|--version-only|--agents-only) ;;
    *)
        echo "usage: scripts/release-check.sh [--metadata-only|--version-only|--agents-only]" >&2
        exit 2
        ;;
esac

if [ "$mode" != "--version-only" ]; then
    PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
from __future__ import annotations

import runpy
from pathlib import Path

root = Path.cwd()
runtime = runpy.run_path(str(root / "scripts" / "star_forge.py"))
role_names = runtime["agent_role_names"]
render_agent_toml = runtime["render_agent_toml"]

expected_paths = {
    root / ".codex" / "agents" / f"starforge-{role}.toml": role
    for role in role_names()
}
actual_paths = set((root / ".codex" / "agents").glob("starforge-*.toml"))
problems: list[str] = []

for path, role in expected_paths.items():
    if not path.is_file():
        problems.append(f"missing generated agent definition: {path.relative_to(root)}")
        continue
    expected = render_agent_toml(role).encode("utf-8")
    if path.read_bytes() != expected:
        problems.append(
            "generated agent definition does not match its canonical prompt: "
            f"{path.relative_to(root)} != agents/{role}/agent.md"
        )

for path in sorted(actual_paths - set(expected_paths)):
    problems.append(
        f"generated agent definition has no canonical prompt: {path.relative_to(root)}"
    )

if problems:
    raise SystemExit("release check failed: " + "; ".join(problems))
PY
fi

if [ "$mode" = "--agents-only" ]; then
    exit 0
fi

python3 -m json.tool .codex-plugin/plugin.json >/dev/null

python3 - "$mode" <<'PY'
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
mode = sys.argv[1]
manifest_path = root / ".codex-plugin" / "plugin.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
version = manifest.get("version", "")
semver = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+codex\.[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
if not isinstance(version, str) or not semver.fullmatch(version):
    raise SystemExit(
        "release check failed: manifest version must be semantic versioning with "
        "an optional single +codex.<cachebuster> suffix"
    )


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def commit_exists(candidate: str) -> bool:
    if not candidate:
        return False
    return git("cat-file", "-e", f"{candidate}^{{commit}}", check=False).returncode == 0


def event_base() -> str:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path:
        return ""
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return ""
    base = pull_request.get("base")
    if not isinstance(base, dict):
        return ""
    sha = base.get("sha")
    return sha if isinstance(sha, str) else ""


if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
    raise SystemExit("release check failed: release validation must run inside Git")

base_candidates = [
    os.environ.get("STAR_FORGE_RELEASE_BASE", ""),
    os.environ.get("GITHUB_BASE_SHA", ""),
    event_base(),
    "refs/remotes/origin/main",
    "refs/remotes/origin/master",
]
base = next((candidate for candidate in base_candidates if commit_exists(candidate)), "")
if base:
    changed = [
        line
        for line in git("diff", "--name-only", base, "--").stdout.splitlines()
        if line and not line.startswith(".starforge/")
    ]
    changed.extend(
        line
        for line in git(
            "ls-files", "--others", "--exclude-standard"
        ).stdout.splitlines()
        if line and not line.startswith(".starforge/")
    )
    changed = list(dict.fromkeys(changed))
    if changed:
        prior_manifest = git(
            "show", f"{base}:.codex-plugin/plugin.json", check=False
        )
        if prior_manifest.returncode != 0:
            raise SystemExit(
                "release check failed: the comparison revision has no plugin manifest"
            )
        try:
            prior_version = json.loads(prior_manifest.stdout).get("version", "")
        except json.JSONDecodeError as exc:
            raise SystemExit(
                "release check failed: the comparison manifest is invalid JSON"
            ) from exc
        if prior_version == version:
            sample = ", ".join(changed[:5])
            raise SystemExit(
                "release check failed: publishable files changed without a new "
                f"plugin version or cachebuster ({sample})"
            )
elif mode != "--version-only":
    print(
        "release check note: no comparison revision was available; "
        "validated current manifest version only",
        file=sys.stderr,
    )
PY

if [ "$mode" = "--version-only" ]; then
    exit 0
fi

python3 -m json.tool .agents/plugins/marketplace.json >/dev/null
python3 tests/test_v04_release.py

if [ "$mode" = "--metadata-only" ]; then
    exit 0
fi

scripts/check.sh
python3 scripts/star_forge.py self-test --strict
