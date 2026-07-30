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

def git_paths(*args: str) -> list[str]:
    process = subprocess.run(
        ["git", *args],
        cwd=root,
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        raise SystemExit(
            "release check failed: cannot enumerate Git paths: "
            + os.fsdecode(process.stderr).strip()
        )
    output = process.stdout
    if output and not output.endswith(b"\0"):
        raise SystemExit(
            "release check failed: Git path output is not NUL terminated"
        )
    records = [] if not output else output[:-1].split(b"\0")
    if any(not record for record in records):
        raise SystemExit(
            "release check failed: Git path output contains an empty record"
        )
    return [os.fsdecode(record) for record in records]


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
    if isinstance(pull_request, dict):
        base = pull_request.get("base")
        if isinstance(base, dict) and isinstance(base.get("sha"), str):
            return base["sha"]
    before = payload.get("before")
    return (
        before
        if isinstance(before, str) and before.strip("0")
        else ""
    )


if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
    raise SystemExit("release check failed: release validation must run inside Git")

explicit_candidates = [
    ("STAR_FORGE_RELEASE_BASE", os.environ.get("STAR_FORGE_RELEASE_BASE", "")),
    ("GITHUB_BASE_SHA", os.environ.get("GITHUB_BASE_SHA", "")),
    ("GitHub event base", event_base()),
]
for label, candidate in explicit_candidates:
    if candidate and not commit_exists(candidate):
        raise SystemExit(
            f"release check failed: {label} is not a readable commit"
        )
base_candidates = [
    *(candidate for _label, candidate in explicit_candidates),
    "refs/remotes/origin/main",
    "refs/remotes/origin/master",
]
base = next((candidate for candidate in base_candidates if commit_exists(candidate)), "")
head = git("rev-parse", "HEAD").stdout.strip()
initial_release = os.environ.get("STAR_FORGE_INITIAL_RELEASE", "") == "1"
if not base:
    if not initial_release:
        raise SystemExit(
            "release check failed: no trustworthy comparison revision is "
            "available; set STAR_FORGE_RELEASE_BASE to the predecessor commit"
        )
    if commit_exists("HEAD^"):
        raise SystemExit(
            "release check failed: STAR_FORGE_INITIAL_RELEASE=1 is valid only "
            "for a repository's first commit"
        )
if base:
    base_is_head = git("rev-parse", base).stdout.strip() == head
    changed = [
        path
        for path in git_paths("diff", "--name-only", "-z", base, "--")
        if not path.startswith(".starforge/")
    ]
    changed.extend(
        path
        for path in git_paths(
            "ls-files", "-z", "--others", "--exclude-standard"
        )
        if not path.startswith(".starforge/")
    )
    changed = list(dict.fromkeys(changed))
    if base_is_head and not changed:
        raise SystemExit(
            "release check failed: the comparison revision resolves to current HEAD"
        )
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
        "release check note: explicit initial release validated current "
        "manifest version only",
        file=sys.stderr,
    )
PY

if [ "$mode" = "--version-only" ]; then
    exit 0
fi

python3 -m json.tool .agents/plugins/marketplace.json >/dev/null
release_candidate_tmp=$(mktemp -d "${TMPDIR:-/tmp}/star-forge-release-candidate.XXXXXX")
trap 'rmdir "$release_candidate_tmp" 2>/dev/null || true' EXIT HUP INT TERM
STAR_FORGE_RC_TMPDIR="$release_candidate_tmp" \
    PYTHONDONTWRITEBYTECODE=1 \
    python3 tests/test_v04_release.py
rmdir "$release_candidate_tmp"
trap - EXIT HUP INT TERM

if [ "$mode" = "--metadata-only" ]; then
    exit 0
fi

scripts/check.sh
python3 scripts/star_forge.py self-test --strict
