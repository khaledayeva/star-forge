"""Cohesive Star Forge runtime extracted from the CLI facade."""

from __future__ import annotations
import json
import re
import stat
from pathlib import Path
from typing import Any
from live_collectors import common as live_common
from starforge import lifecycle as project_lifecycle
from starforge import review_policy as adaptive_review_policy
from .policy_data import mapping as _policy_mapping, record as _policy_record
from .policy_data import value as _policy_value
from .runtime_support import *

_POLICY = _policy_value("runtime_project.POLICY")

def has_star_forge_project_markers(project: Path) -> bool:
    return (project / PROJECT_MANIFEST).exists()

def _optional_load(path: Path, loader: Any, errors: tuple[type[BaseException], ...], default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return loader(path)
    except errors:
        return default

def _read_json_if_possible(path: Path) -> dict[str, Any]:
    return _optional_load(path, read_json, (Exception,), {})

def find_star_forge_project_root(start: Path) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if has_star_forge_project_markers(candidate):
            return follow_project_redirect(candidate)
    return None

def follow_project_redirect(candidate: Path) -> Path:
    manifest = _read_json_if_possible(candidate / PROJECT_MANIFEST)
    raw_root = (str(manifest.get("project_root") or "")
                if manifest.get("schema") == _POLICY["redirect_schema"] else "")
    target = Path(raw_root) if raw_root else candidate
    valid = (target.resolve() != candidate.resolve() and target.exists()
             and has_star_forge_project_markers(target))
    return target.resolve() if valid else candidate

def resolve_project(raw: str) -> Path:
    start = Path(raw).resolve()
    return find_star_forge_project_root(start) or repo_root(start)

def root_needs_product_isolation(project: Path) -> bool:
    if has_star_forge_project_markers(project):
        return False
    try:
        entries = {path.name for path in project.iterdir()}
    except OSError:
        return False
    return bool(entries & set(_POLICY["isolation_markers"])
                and entries - set(_POLICY["isolation_ignored"]))

def product_slug_from_objective(project: Path, objective: str = "", explicit: str = "") -> str:
    if explicit.strip():
        return slugify(explicit).lower()
    text = objective.strip()
    if not text:
        blueprint = _optional_load(project / BLUEPRINT_FILE, read_text, (OSError,), "")
        match = re.search(r"^#\s+(.+)$", blueprint, re.MULTILINE)
        text = match.group(1) if match else project.name
    terms = [term for term in re.findall(r"[A-Za-z0-9]+", text.lower()) if term not in STOPWORDS]
    source = ("-".join(terms[:_POLICY["slug_term_limit"]]) if terms
              else project.name or _POLICY["slug_fallback"])
    return slugify(source).lower()

def normalize_project_profile(profile: str) -> str:
    candidate = str(profile or "").strip()
    return candidate if candidate in REVIEW_PROFILE_ROLES else _POLICY["default_profile"]

def source_profile_path(project: Path) -> Path:
    return project / SOURCE_PROFILE_FILE

def _profile_failure(status: dict[str, Any], field: str, kind: str) -> dict[str, Any]:
    status[field] = _POLICY["source_profile_errors"][kind].format(file=SOURCE_PROFILE_FILE)
    return status

def _attempt(operation: Any, errors: tuple[tuple[type[BaseException], str], ...]) -> tuple[Any, str]:
    try:
        return operation(), ""
    except tuple(error[0] for error in errors) as exc:
        return None, next(kind for exception, kind in errors if isinstance(exc, exception))

def _source_profile_status(project: Path) -> dict[str, Any]:
    status = dict(_POLICY["source_profile_status_defaults"])
    path = source_profile_path(project)
    mode, error = _attempt(lambda: path.lstat().st_mode, (
        (FileNotFoundError, "missing"), (OSError, "inspect")))
    status["exists"] = error != "missing"
    if error == "inspect":
        return _profile_failure(status, "path_problem", "inspect")
    parent, error = _attempt(lambda: path.parent.resolve(strict=True), ((OSError, "parent"),))
    if error:
        return _profile_failure(status, "path_problem", "parent")
    if not (parent / path.name).is_relative_to(project.resolve()):
        return _profile_failure(status, "path_problem", "outside")
    if mode is None:
        return status
    path_error = ("symlink" if stat.S_ISLNK(mode) else "regular"
                  if not stat.S_ISREG(mode) else "readable" if mode & 0o444 == 0 else "")
    if path_error:
        field = "read_problem" if path_error == "readable" else "path_problem"
        return _profile_failure(status, field, path_error)
    payload, error = _attempt(lambda: read_json(path), (
        (PermissionError, "readable"), (UnicodeDecodeError, "utf8"),
        (json.JSONDecodeError, "json"), (ForgeError, "object"), (OSError, "read")))
    if error:
        field = ("invalid_problem" if error in {"utf8", "json", "object"}
                 else "read_problem")
        return _profile_failure(status, field, error)
    invalid = next((kind for failed, kind in (
        (not isinstance(payload, dict), "object"),
        (isinstance(payload, dict) and payload.get("schema") != SOURCE_PROFILE_SCHEMA, "schema"),
        (isinstance(payload, dict) and str(payload.get("profile") or "").strip()
         not in REVIEW_PROFILE_ROLES, "profile")) if failed), "")
    if invalid:
        return _profile_failure(status, "invalid_problem", invalid)
    status["payload"] = payload
    return status

def source_profile_exists(project: Path) -> bool:
    return bool(_source_profile_status(project)["exists"])

def source_profile_path_problem(project: Path) -> str:
    return str(_source_profile_status(project)["path_problem"])

def _profile_problem(status: dict[str, Any], *fields: str) -> str:
    return str(next((status[field] for field in fields if status[field]), ""))

def source_profile_read_problem(project: Path) -> str:
    return _profile_problem(_source_profile_status(project), "path_problem", "read_problem")

def source_profile_invalid_problem(project: Path) -> str:
    return _profile_problem(
        _source_profile_status(project), "path_problem", "read_problem", "invalid_problem")

def source_profile_hash_blocker(project: Path) -> str:
    status = _source_profile_status(project)
    return (_profile_problem(status, "path_problem", "read_problem", "invalid_problem")
            if status["exists"] else "")

def source_hash_unavailable_problem(project: Path) -> dict[str, Any] | None:
    problem = source_profile_hash_blocker(project)
    if not problem:
        return None
    return _policy_mapping(
        "source_hash_problem", file=SOURCE_PROFILE_FILE,
        message=_POLICY["source_hash_messages"]["blocked"].format(problem=problem))

def source_hash_exception_problem(exc: BaseException) -> dict[str, Any]:
    return _policy_mapping("source_hash_problem", file="source tree",
        message=_POLICY["source_hash_messages"]["exception"].format(error=exc))

def try_source_hash(project: Path) -> tuple[str | None, dict[str, Any] | None]:
    problem = source_hash_unavailable_problem(project)
    if problem:
        return None, problem
    try:
        return source_hash(project), None
    except (PermissionError, OSError) as exc:
        return None, source_hash_exception_problem(exc)

def read_source_profile_payload(project: Path) -> dict[str, Any]:
    return dict(_source_profile_status(project)["payload"])

def read_source_profile(project: Path) -> str:
    profile = str(read_source_profile_payload(project).get("profile") or "").strip()
    return profile if profile in REVIEW_PROFILE_ROLES else ""

def ensure_source_profile(project: Path, profile: str) -> None:
    normalized = normalize_project_profile(profile)
    write_problem = source_profile_path_problem(project)
    if write_problem:
        raise ForgeError(f"Refusing to write {SOURCE_PROFILE_FILE}: {write_problem}")
    existing = read_source_profile_payload(project)
    selected_before_gates = (bool(existing.get("selected_before_gates")) if existing
                             else not profile_downgrade_lock_reasons(project))
    payload = _policy_record(
        "source_profile", profile=normalized, initial_profile=str(existing.get("initial_profile") or normalized),
        selected_before_gates=selected_before_gates, review_roles=review_roles_for_profile(normalized))
    write_json_if_changed(source_profile_path(project), payload)

def review_records_exist(project: Path) -> bool:
    root = project / REVIEWS_DIR
    return root.exists() and any(path.is_file() for path in root.rglob("*"))

def _profile_gate_reasons(project: Path, *, extended: bool) -> list[str]:
    reasons: list[str] = []
    if blueprint_is_approved(project):
        reasons.append(_POLICY["gate_reasons"]["blueprint"])
    plan_text = _optional_load(project / PLAN_FILE, read_text, (OSError,), "")
    if _text_has_real_tasks(plan_text, parse_tasks_from_text):
        if extended:
            reasons.append(_POLICY["gate_reasons"]["plan_exists"])
        reasons.append(_POLICY["gate_reasons"]["plan_tasks"])
    if extended and load_proof(project) is not None:
        reasons.append(_POLICY["gate_reasons"]["proof"])
    if extended and review_records_exist(project):
        reasons.append(_POLICY["gate_reasons"]["reviews"])
    return reasons

def profile_downgrade_lock_reasons(project: Path) -> list[str]:
    return _profile_gate_reasons(project, extended=True)

def source_profile_payload_from_text(text: str) -> dict[str, Any]:
    payload, error = _attempt(lambda: json.loads(text), ((Exception, "invalid"),))
    valid = (not error and isinstance(payload, dict)
             and payload.get("schema") == SOURCE_PROFILE_SCHEMA
             and str(payload.get("profile") or "").strip() in REVIEW_PROFILE_ROLES)
    return payload if valid else {}

def _text_has_real_tasks(text: str, parser: Any) -> bool:
    try:
        tasks = parser(text)
    except Exception:
        return False
    return bool(tasks) and not plan_is_placeholder(tasks)

def plan_text_has_real_tasks(text: str) -> bool:
    return _text_has_real_tasks(text, parse_tasks_from_text)

def git_show_text(project: Path, revision: str, relpath: str) -> str | None:
    code, out, _ = run_git(["show", f"{revision}:{relpath}"], project)
    return out if code == 0 else None

def git_revision_has_review_gates(project: Path, revision: str) -> bool:
    blueprint, plan = (git_show_text(project, revision, path)
                       for path in (BLUEPRINT_FILE, PLAN_FILE))
    return bool(blueprint is not None and blueprint_text_is_approved(blueprint)
                or plan is not None and plan_text_has_real_tasks(plan))

def git_revision_or_ancestors_have_review_gates(project: Path, revision: str) -> bool:
    code, out, _ = run_git(["rev-list", "--reverse", revision], project)
    if code != 0:
        return True
    return any(git_revision_has_review_gates(project, line.strip())
               for line in out.splitlines() if line.strip())

def git_history_has_fast_mvp_before_gates(project: Path) -> bool:
    if not is_git_repo(project) or git_head(project) is None:
        return False
    code, out, _ = run_git(["rev-list", "--reverse", "HEAD", "--", SOURCE_PROFILE_FILE], project)
    if code != 0:
        return False
    selected = next((revision for revision in (line.strip() for line in out.splitlines())
                     if revision
                     if source_profile_payload_from_text(
                         git_show_text(project, revision, SOURCE_PROFILE_FILE) or ""
                     ).get("profile") == _POLICY["fast_profile"]), "")
    return bool(selected) and not git_revision_or_ancestors_have_review_gates(project, selected)

def source_profile_lock_is_durable(project: Path) -> bool:
    path = source_profile_path(project)
    if (not source_profile_exists(project) or not is_git_repo(project)
            or git_head(project) is None or source_profile_read_problem(project)):
        return False
    code, _, _ = run_git(["ls-files", "--error-unmatch", "--", SOURCE_PROFILE_FILE], project)
    dirty = any(git_status_path(entry) == SOURCE_PROFILE_FILE for entry in git_status(project))
    snapshotted = any(candidate.resolve() == path.resolve()
                      for candidate in live_common.snapshot_file_candidates(project))
    return code == 0 and not dirty and snapshotted

def fast_mvp_profile_predates_gates(project: Path) -> bool:
    return (read_source_profile(project) == _POLICY["fast_profile"]
            and source_profile_lock_is_durable(project)
            and git_history_has_fast_mvp_before_gates(project))

def fast_mvp_profile_selected_before_gates(project: Path) -> bool:
    payload = read_source_profile_payload(project)
    return (str(payload.get("profile") or "") == _POLICY["fast_profile"]
            and bool(payload.get("selected_before_gates")))

def setup_ledger_records_fast_mvp_before_gates(project: Path) -> bool:
    expected = _POLICY["ledger_setup"]
    return any(payload.get("schema") == expected["schema"]
               and payload.get("event") == expected["event"]
               and str(payload.get("profile") or "") == expected["profile"]
               and bool(payload.get(expected["selected_field"]))
               for payload in jsonl_payloads(project / LEDGER_FILE))

def review_profile(project: Path) -> str:
    manifest_profile = project_profile(project)
    return (_POLICY["fast_profile"] if manifest_profile == _POLICY["fast_profile"]
            and fast_mvp_profile_predates_gates(project) else _POLICY["default_profile"])

def profile_lock_gate_reasons(project: Path) -> list[str]:
    return _profile_gate_reasons(project, extended=False)

def source_profile_lock_problems(project: Path) -> list[str]:
    path = source_profile_path(project)
    errors = _POLICY["source_profile_lock_errors"]
    message = lambda kind: errors[kind].format(file=SOURCE_PROFILE_FILE)
    status = _source_profile_status(project)
    if not status["exists"]:
        return [message("missing")]
    profile_problem = _profile_problem(
        status, "path_problem", "read_problem", "invalid_problem")
    if profile_problem:
        return [str(profile_problem)]
    problems = ([] if status["payload"].get("profile") == _POLICY["fast_profile"]
                else [message("wrong_profile")])
    if not is_git_repo(project):
        problems.append(message("not_repo"))
        return problems
    head = git_head(project)
    code, _, _ = run_git(["ls-files", "--error-unmatch", "--", SOURCE_PROFILE_FILE], project)
    checks = ((head is None, "no_commits"), (code != 0, "untracked"),
        (any(git_status_path(entry) == SOURCE_PROFILE_FILE for entry in git_status(project)), "dirty"),
        (not any(candidate.resolve() == path.resolve()
                 for candidate in live_common.snapshot_file_candidates(project)), "snapshot"),
        (head is not None and not git_history_has_fast_mvp_before_gates(project), "history"))
    problems.extend(message(kind) for failed, kind in checks if failed)
    return problems

def fast_mvp_profile_lock_state(project: Path) -> dict[str, Any]:
    manifest_profile = project_profile(project)
    profile_status = _source_profile_status(project)
    profile_problem = _profile_problem(
        profile_status, "path_problem", "read_problem", "invalid_problem")
    source_payload = profile_status["payload"]
    recorded_profile = str(source_payload.get("profile") or "")
    effective_profile = review_profile(project)
    selected_before_gates = bool(source_payload.get("selected_before_gates"))
    fast_profile = _POLICY["fast_profile"]
    requested_fast_mvp = manifest_profile == fast_profile or recorded_profile == fast_profile
    gate_reasons = profile_lock_gate_reasons(project)
    states = ((effective_profile == fast_profile, "active"), (requested_fast_mvp and bool(profile_problem), "invalid"),
              (requested_fast_mvp and selected_before_gates and bool(gate_reasons), "selected_blocked"),
              (requested_fast_mvp and selected_before_gates, "selected_pending"),
              (requested_fast_mvp, "standard_required"), (True, "inactive"))
    state = next(name for selected, name in states if selected)
    descriptor = _POLICY["lock_states"][state]
    values = {key: value.format(file=SOURCE_PROFILE_FILE, problem=profile_problem)
              if isinstance(value, str) else value for key, value in descriptor.items()}
    return _policy_mapping(
        "profile_lock_state", **values, manifest_profile=manifest_profile,
        source_profile=recorded_profile or None, effective_review_profile=effective_profile,
        selected_before_gates=selected_before_gates, gate_reasons=gate_reasons,
        problems=source_profile_lock_problems(project) if requested_fast_mvp else [])

def project_manifest_payload(project: Path, *, objective: str = "", product_slug: str = "", profile: str = "standard", root_mode: str = "dedicated") -> dict[str, Any]:
    slug = product_slug_from_objective(project, objective, product_slug)
    project_root = str(project.resolve())
    return _policy_record(
        "project_manifest", created_at=now_utc(), updated_at=now_utc(),
        project_root=project_root, product_slug=slug, project_id=stable_json_hash({"root": project_root, "slug": slug})[:16],
        profile=normalize_project_profile(profile), source_profile_path=SOURCE_PROFILE_FILE,
        root_mode=root_mode or _POLICY["default_root_mode"], state_machine_version=STAR_FORGE_STATE_VERSION,
        blueprint_path=BLUEPRINT_FILE, plan_path=PLAN_FILE,
        blueprint_hash=file_sha256(project / BLUEPRINT_FILE) if (project / BLUEPRINT_FILE).exists() else None,
        plan_hash=file_sha256(project / PLAN_FILE) if (project / PLAN_FILE).exists() else None)

def ensure_project_manifest(project: Path, *, objective: str = "", product_slug: str = "", profile: str = "", root_mode: str = "") -> dict[str, Any]:
    path = project / PROJECT_MANIFEST
    existing = _read_json_if_possible(path)
    if existing.get("schema") == _POLICY["redirect_schema"]:
        raise ForgeError(_POLICY["redirect_error"].format(
            path=path, project_root=existing.get("project_root")))
    explicit_profile = bool(str(profile or "").strip())
    selected_profile = profile if explicit_profile else existing.get("profile")
    requested_profile = normalize_project_profile(str(selected_profile or _POLICY["default_profile"]))
    if explicit_profile and requested_profile == _POLICY["fast_profile"]:
        lock_reasons = profile_downgrade_lock_reasons(project)
        if lock_reasons and not fast_mvp_profile_predates_gates(project):
            raise ForgeError(_POLICY["downgrade_error"].format(
                reasons=", ".join(lock_reasons)))
    if explicit_profile:
        ensure_source_profile(project, requested_profile)
    payload = project_manifest_payload(
        project,
        objective=objective,
        product_slug=product_slug or str(existing.get("product_slug") or ""),
        profile=requested_profile,
        root_mode=root_mode or str(existing.get("root_mode") or _POLICY["default_root_mode"]),
    )
    if existing:
        fields = _POLICY["manifest_preserved_fields"]
        payload.update({field: existing[field] or payload.get(field)
                        for field in fields["nonempty"] if field in existing})
        payload.update({field: existing[field] for field in fields["exact"]
                        if field in existing})
        if strip_volatile(existing) == strip_volatile(payload):
            return existing
    write_json(path, payload)
    return payload

def project_profile(project: Path) -> str:
    return normalize_project_profile(str(_read_json_if_possible(project / PROJECT_MANIFEST).get("profile") or _POLICY["default_profile"]))

def review_roles_for_profile(profile: str) -> list[str]:
    return adaptive_review_policy.legacy_roles_for_profile(profile)

def required_review_policy(
    project: Path,
    *,
    source_hash_value: str | None = None,
    bind_source_hash: bool = True,
) -> adaptive_review_policy.ReviewPolicySelection:
    blueprint_text = _optional_load(project / BLUEPRINT_FILE, read_text, (OSError,), "")
    tasks = _optional_load(project / PLAN_FILE, parse_tasks, (ForgeError,), [])
    contract_path = project / project_lifecycle.DELIVERY_CONTRACT_PATH
    delivery_contract = _optional_load(
        contract_path, read_json, (ForgeError, OSError, UnicodeError, json.JSONDecodeError), {})
    if source_hash_value is None and bind_source_hash:
        source_hash_value, _problem = try_source_hash(project)
    return adaptive_review_policy.select_review_policy(
        blueprint_text, tasks, profile=review_profile(project),
        source_hash=source_hash_value, delivery_contract=delivery_contract)

def required_review_roles(project: Path) -> list[str]:
    return list(required_review_policy(project).roles)

__all__ = tuple(name for name in globals() if not name.startswith("__"))
