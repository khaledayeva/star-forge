"""Collection orchestration and CLI for the GitHub collector."""

from __future__ import annotations

from live_collectors.github_adapter import *  # noqa: F401,F403


def _load_input(args: argparse.Namespace, problems: list[dict[str, Any]]) -> RawEvidence:
    modes = (
        (args.connector_fixture, load_connector_fixture),
        (args.gh_fixture_dir, load_gh_fixture_dir),
        (args.connector_input, load_connector_input),
        (args.gh_readonly_dir, load_gh_readonly_dir),
    )
    selected = [(path, loader) for path, loader in modes if path]
    if len(selected) == 1:
        path, loader = selected[0]
        return loader(Path(path))
    problems.append(command_problem(
        "provide exactly one of --connector-fixture, --gh-fixture-dir, "
        "--connector-input, or --gh-readonly-dir"
    ))
    return RawEvidence("missing-fixture", {}, {}, "", None, None, None, None,
                       None, None, [], [], {})


def _write_artifacts(
    root: Path, specs: Mapping[str, tuple[str, Any, bool]], report: Mapping[str, int],
) -> tuple[dict[str, Path], dict[str, int]]:
    artifacts: dict[str, Path] = {}
    merged = dict(report)
    for key, (filename, payload, text) in specs.items():
        path, local = (artifact_write_text(root / filename, str(payload))
                       if text else artifact_write_json(root / filename, payload))
        artifacts[key] = path
        merged = merge_reports(merged, local)
    return artifacts, merged


def _proof_commands(project: Path, task: str, manifest: Path) -> list[list[str]]:
    return descriptor(
        PROOF_COMMANDS, project=live_common.project_cli_arg(project), task=str(task),
        manifest=live_common.project_relative(project, manifest),
    )


def collect(args: argparse.Namespace) -> CollectionResult:
    project = Path(args.project).resolve()
    source_hash_before = live_common.compute_source_hash(project)
    problems: list[dict[str, Any]] = []
    raw = _load_input(args, problems)
    if args.foundation_evidence:
        try:
            path = live_common.safe_project_path(
                project, args.foundation_evidence, must_exist=True
            )
            payload = read_json(path, {})
            if not isinstance(payload, Mapping):
                raise ValueError("Foundation evidence must be a JSON object")
            raw.foundation_provenance = dict(payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            problems.append(blocking_problem(
                f"Foundation evidence could not be imported safely: {exc}",
                rule="github-foundation-provenance",
            ))

    initial_base, initial_head = extract_base_sha(raw.pr), extract_head_sha(raw.pr)
    captured_base, captured_head = first_text(args.base, initial_base), first_text(args.head, initial_head)
    github_host = github_host_for_raw(raw)
    problems += validate_read_only(
        raw, repo=args.repo, pr_number=str(args.pr),
        captured_head=captured_head, github_host=github_host,
    )
    if is_live_source(raw.source):
        current_base = extract_current_base_sha(raw.final_pr)
        current_head = extract_current_head_sha(raw.final_pr)
    else:
        current_base = extract_current_base_sha(raw.final_pr) or initial_base
        current_head = extract_current_head_sha(raw.final_pr) or initial_head
    merge_base = extract_merge_base(raw, raw.pr)
    for argument, initial, side in (
        (args.base, initial_base, "base"), (args.head, initial_head, "head")
    ):
        if argument and initial and argument != initial:
            problems.append(blocking_problem(
                f"captured {side} SHA does not match PR metadata", rule="github-refs"
            ))
    missing_refs = not all((captured_base, captured_head, current_base, current_head, merge_base))
    if missing_refs:
        problems.append(blocking_problem(
            "GitHub PR evidence is missing base, head, current, or merge-base refs",
            rule="github-refs",
        ))
    for captured, current, side in (
        (captured_base, current_base, "base"), (captured_head, current_head, "head")
    ):
        if captured and current and captured != current:
            problems.append(blocking_problem(
                f"GitHub PR {side} SHA changed after capture", rule="github-freshness"
            ))

    files = normalize_files(raw, raw.pr)
    collections = [
        normalize_simple_list(payload, keys)
        for payload, keys in (
            (raw.reviews, ("reviews", "nodes")),
            (raw.comments, ("comments", "review_comments", "nodes")),
            (raw.annotations, ("annotations", "nodes")),
        )
    ]
    reviews, comments, annotations = (item[0] for item in collections)
    list_partial = any(item[1] for item in collections)
    list_incomplete = any(item[2] for item in collections)
    checks, checks_partial, checks_incomplete = normalize_check_runs(
        raw.check_runs, captured_head, problems
    )
    partial_permissions = checks_partial or list_partial
    pagination_incomplete = checks_incomplete or list_incomplete
    if list_partial:
        problems.append(blocking_problem(
            "GitHub PR evidence reports partial permissions", rule="github-permissions"
        ))
    if list_incomplete:
        problems.append(blocking_problem(
            "GitHub PR evidence reports incomplete pagination", rule="github-pagination"
        ))
    problems += validate_live_import(
        raw, repo=args.repo, pr_number=str(args.pr),
        captured_base=captured_base, captured_head=captured_head,
        current_base=current_base, current_head=current_head,
    )
    logs, log_report = normalize_logs(
        raw.logs, include=bool(args.include_ci_logs),
        max_log_bytes=int(args.max_log_bytes), problems=problems,
    )
    if logs is not None:
        log_problems = validate_ci_log_excerpt_payload(
            logs, repo=args.repo, pr_number=str(args.pr), captured_head=captured_head,
            check_runs=checks, path="ci-log-excerpts.json",
        )
        problems += log_problems
        if log_problems:
            logs = None

    root = live_common.live_collector_dir(project, args.task, COLLECTOR)
    safe_commands, command_report = redact_gh_api_command_query_values(raw.commands)
    pr_payload = normalize_pr_payload(
        raw=raw, repo=args.repo, pr_number=str(args.pr),
        captured_base=captured_base, captured_head=captured_head,
        current_base=current_base, current_head=current_head,
        merge_base=merge_base, files=files,
    )
    transcript = operation_transcript_payload(
        raw=raw, repo=args.repo, pr_number=str(args.pr), github_host=github_host,
        captured_base=captured_base, captured_head=captured_head,
        current_base=current_base, current_head=current_head, merge_base=merge_base,
        partial_permissions=partial_permissions,
        pagination_incomplete=pagination_incomplete,
    )
    specs: dict[str, tuple[str, Any, bool]] = {
        "pr": ("pr.json", pr_payload, False),
        "diff": ("diff.patch", raw.diff or "", True),
        "reviews": ("reviews.json", {"reviews": reviews}, False),
        "comments": ("comments.json", {"comments": comments}, False),
        "check-runs": ("check-runs.json", checks, False),
        "annotations": ("annotations.json", {"annotations": annotations}, False),
        "operation-transcript": ("operation-transcript.json", transcript, False),
    }
    if logs is not None:
        specs["ci-log-excerpts"] = ("ci-log-excerpts.json", logs, False)
    artifacts, redaction_report = _write_artifacts(
        root, specs, merge_reports(log_report, command_report)
    )
    transcript_path = artifacts["operation-transcript"]
    transcript_sha256 = live_common.file_sha256(transcript_path)
    source_hash_after = live_common.compute_source_hash(project)
    foundation = normalize_foundation_provenance(
        raw, repo=args.repo, current_source_hash=source_hash_after, problems=problems
    )
    live_provenance = dict(raw.live_provenance)
    if github_host and github_host_provenance_evidence_for_raw(raw):
        live_provenance.setdefault("github_host", github_host)
    live_provenance["operation_transcript_sha256"] = transcript_sha256
    live_provenance, provenance_report = redact_artifact_payload(live_provenance)
    foundation, foundation_report = redact_artifact_payload(foundation)
    redaction_report = merge_reports(
        redaction_report, provenance_report, foundation_report
    )
    summary = descriptor(
        SUMMARY_TEMPLATE, source=raw.source, repo=args.repo, pr=str(args.pr),
        github_host=github_host, captured_base_sha=captured_base,
        current_base_sha=current_base, captured_head_sha=captured_head,
        current_head_sha=current_head, merge_base_sha=merge_base,
        changed_files_count=len(files), review_count=len(reviews),
        comment_count=len(comments), check_run_count=len(checks.get("check_runs", [])),
        annotation_count=len(annotations),
        ci_log_excerpt_count=len(logs.get("logs", [])) if isinstance(logs, dict) else 0,
        logs_included=bool(logs), missing_refs=missing_refs,
        partial_permissions=partial_permissions, pagination_incomplete=pagination_incomplete,
        checks_bound_to_head=not any(
            item.get("rule") == "github-checks"
            and "head SHA" in str(item.get("message")) for item in problems
        ),
        read_only_operations=raw.operations, read_only_commands=safe_commands,
        read_only_transcript_sha256=transcript_sha256,
        captured_at=first_text(
            raw.live_provenance.get("collected_at"), raw.live_provenance.get("captured_at")
        ),
        live_provenance=live_provenance, foundation=foundation,
    )
    tool_versions = {"adapter": "github-pr.v1", "source": raw.source, **raw.tool_versions}
    safe_argv, argv_report = redact_artifact_payload(args.command_argv)
    redaction_report = merge_reports(redaction_report, argv_report)
    manifest = live_common.write_live_manifest(
        project, task=args.task, collector=COLLECTOR, command_argv=safe_argv,
        tool_versions=tool_versions, artifacts=artifacts, summary=summary,
        degraded=False, unavailable_capabilities=[], problems=problems,
        source_hash_before=source_hash_before, source_hash_after=source_hash_after,
        runtime_asset_hash=live_common.compute_runtime_asset_hash(project),
    )
    update_manifest_redaction_report(manifest, redaction_report)
    pr_url = first_text(
        raw.final_pr.get("url"), raw.final_pr.get("html_url"),
        raw.pr.get("url"), raw.pr.get("html_url"),
    )
    envelope, _ = write_evidence_envelope(
        project, manifest, raw=raw, repo=args.repo, pr_number=str(args.pr),
        github_host=github_host, pr_url=pr_url, captured_base=captured_base,
        captured_head=captured_head, current_base=current_base,
        current_head=current_head, foundation=foundation,
    )
    commands = [] if raw.source in {
        "connector-fixture", "gh-fixture", "missing-fixture"
    } else _proof_commands(project, str(args.task), manifest)
    return CollectionResult(manifest, envelope, commands, problems)


def record_proof_commands(result: CollectionResult, project: Path) -> int:
    for command in result.commands:
        record = live_common.run_trusted_command(
            command, cwd=project, script_path=STAR_FORGE_SCRIPT
        )
        if record["stdout"]:
            print(record["stdout"], end="")
        if record["stderr"]:
            print(record["stderr"], end="", file=sys.stderr)
        if record["returncode"] != 0:
            return int(record["returncode"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect read-only GitHub PR evidence for a Star Forge source packet"
    )
    for name in ("project", "base", "head", "connector-fixture", "gh-fixture-dir",
                 "connector-input", "gh-readonly-dir", "foundation-evidence"):
        parser.add_argument(f"--{name}", default="." if name == "project" else "")
    for name in ("task", "repo", "pr"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--include-ci-logs", action="store_true")
    parser.add_argument("--max-log-bytes", type=int, default=DEFAULT_MAX_LOG_BYTES)
    parser.add_argument("--record", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(raw_argv)
    args.command_argv = ["github_pr.py", *raw_argv]
    result = collect(args)
    project = Path(args.project).resolve()
    print("Wrote GitHub PR source packet manifest:")
    print(live_common.project_relative(project, result.manifest_path))
    print("Wrote GitHub evidence envelope:")
    print(live_common.project_relative(project, result.evidence_path))
    if result.commands:
        print("Source packet proof commands:")
        for command in result.commands:
            print(display_command(command))
    else:
        print("Fixture-only evidence was written; production proof commands were not emitted.")
    if args.record:
        if not result.commands:
            print(
                "Record skipped because fixture-only evidence cannot satisfy production proof.",
                file=sys.stderr,
            )
            return 1
        return record_proof_commands(result, project)
    return 1 if result.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
