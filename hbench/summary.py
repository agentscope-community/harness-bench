"""Offline Harbor result aggregation; missing/error trials never become passes."""
from collections import defaultdict
from datetime import datetime
import html
import json
import math
import os
from pathlib import Path
from urllib.parse import quote
from hbench.registry import harness_name


def _count(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


DIAGNOSTICS_VERSION = 1


def _contexts(row):
    # Multi-step jobs use each step's context; never add a root aggregate twice.
    if row.get("step_results"):
        return [s.get("agent_result") or {} for s in row["step_results"]]
    return [row.get("agent_result") or {}]


def _tokens(contexts, key):
    native_key = {"n_input_tokens": "inputTokens", "n_output_tokens": "outputTokens"}.get(key)
    for context in contexts:
        metadata = context.get("metadata") or {}
        if isinstance(metadata, dict):
            missing = metadata.get("native_usage_missing_fields") or []
            if metadata.get("native_parse_errors") or (isinstance(missing, list) and native_key in missing):
                return None
    values = [context.get(key) for context in contexts]
    if not values or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
        return None
    return sum(values)


def _safe_fields(value, keys):
    """Only expose scalar diagnostic fields, never arbitrary env/body/metadata."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if key in value
            and (value[key] is None or isinstance(value[key], (str, bool, int, float)))}


def _diagnostics(row, contexts):
    config = (row.get("config") or {}).get("agent") or {}
    kwargs = config.get("kwargs") or {}
    configured = _safe_fields(kwargs, (
        "version", "reasoning_effort", "max_tokens", "context_window_tokens", "temperature",
        "max_turns", "turn_timeout_seconds", "disable_memory_hooks", "journal_enabled",
        "repeated_failure_guard", "tool_result_eviction", "read_file_max_chars",
    ))
    body = kwargs.get("extra_body")
    if isinstance(body, dict) and isinstance(body.get("enable_thinking"), bool):
        configured["enable_thinking"] = body["enable_thinking"]
    result = {"configured": configured, "configured_source": "result.config.agent.kwargs", "steps": [],
              "usage": {"input_tokens_known": _tokens(contexts, "n_input_tokens") is not None,
                        "output_tokens_known": _tokens(contexts, "n_output_tokens") is not None}}
    failures = {"total": 0, "by_kind": {}, "reported_steps": 0, "total_steps": len(contexts)}
    for index, context in enumerate(contexts):
        metadata = context.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        step = _safe_fields(metadata, (
            "native_status", "native_error_type", "iteration_limit", "native_log_preserved",
            "native_parse_errors", "atif_supported",
        ))
        binding = _safe_fields(metadata.get("native_binding"), (
            "adapter_version", "harness_version", "candidate_sha", "candidate_source_sha256",
            "identity_prompt_version", "identity_prompt_sha256", "journal_enabled",
            "repeated_failure_guard", "tool_result_eviction", "read_file_max_chars",
            "max_turns", "max_iters", "context_window_tokens", "max_tokens", "temperature",
            "enable_thinking", "disable_memory_hooks",
        ))
        if binding:
            step["native_binding"] = binding
        # Prefer the terminal summary over incremental events, without counting both.
        summary = metadata.get("tool_failure_summary") or metadata.get("tool_failures")
        if isinstance(summary, dict):
            total = summary.get("total")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                step["tool_failures"] = {"total": total}
                failures["reported_steps"] += 1
                failures["total"] += total
                detailed = metadata.get("tool_failures") or {}
                by_tool = detailed.get("by_tool_kind") if isinstance(detailed, dict) else None
                derived = {}
                if isinstance(by_tool, dict):
                    clean = {}
                    for tool, values in by_tool.items():
                        if not isinstance(tool, str) or not isinstance(values, dict):
                            continue
                        clean[tool] = {k: v for k, v in values.items() if isinstance(k, str)
                                       and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
                        for kind, count in clean[tool].items():
                            derived[kind] = derived.get(kind, 0) + count
                    step["tool_failures"]["by_tool_kind"] = clean
                by_kind = summary.get("by_kind", derived)
                if isinstance(by_kind, dict):
                    kinds = {k: v for k, v in by_kind.items() if isinstance(k, str)
                             and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
                    step["tool_failures"]["by_kind"] = kinds
                    for kind, count in kinds.items():
                        failures["by_kind"][kind] = failures["by_kind"].get(kind, 0) + count
        if step:
            result["steps"].append({"step": index + 1, "source": "agent_result.metadata", **step})
    # No metadata is unknown, not evidence of zero failures (e.g. official Codex/Pi).
    result["tool_failures"] = failures if failures["reported_steps"] else None
    return result


def _link(path, output):
    # Relative to the report so links keep working after copying artifacts/ or
    # downloading the mirrored OSS prefix ({jobs,reports}/<name>/).
    return quote(Path(os.path.relpath(path, output)).as_posix())


def _error_message(info):
    """First line of the recorded exception, long enough to identify the cause."""
    if not isinstance(info, dict):
        return None
    message = info.get("exception_message")
    if not isinstance(message, str):
        return None
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    return lines[0][:300] if lines else None


def _trial_rows(rows, evidence):
    """One line per recorded trial, for following up on individual tasks."""
    result = []
    for identifier, row in sorted(rows.items(), key=lambda item: (item[1]["trial_name"], item[0])):
        exceptions = [row.get("exception_info")] + [s.get("exception_info") for s in row.get("step_results") or []]
        recorded = next((e for e in exceptions if e), None)
        error = recorded.get("exception_type") if recorded else None
        duration = None
        try:
            if row.get("started_at") and row.get("finished_at"):
                duration = round((datetime.fromisoformat(row["finished_at"])
                                  - datetime.fromisoformat(row["started_at"])).total_seconds(), 1)
        except (TypeError, ValueError):
            pass
        contexts = _contexts(row)
        # Harbor names trials "<task>[__<suffix>]"; the recorded field is authoritative.
        task = row.get("task_name") or row["trial_name"].rsplit("__", 1)[0]
        result.append({"trial": row["trial_name"], "task": task, "agent": row["agent_info"]["name"],
                       "model": (row["agent_info"].get("model_info") or {}).get("name", "unknown"),
                       "dataset": row.get("source") or "adhoc",
                       "reward": ((row.get("verifier_result") or {}).get("rewards") or {}).get("reward"),
                       "error": error, "error_message": _error_message(recorded),
                       "duration_sec": duration,
                       "input_tokens": _tokens(contexts, "n_input_tokens"),
                       "output_tokens": _tokens(contexts, "n_output_tokens"),
                       "diagnostics": _diagnostics(row, contexts),
                       "evidence": evidence[identifier]})
    return result


def aggregate(rows, planned=None, *, expected=None):
    """Aggregate evidence, optionally seeding groups with offline planned counts."""
    if planned is not None:
        _count(planned, "Planned trial count")
    if expected is not None:
        for count in expected.values():
            if count is not None:
                _count(count, "Planned group count")
        known = sum(count for count in expected.values() if count is not None)
        if all(count is not None for count in expected.values()):
            if planned is not None and planned != known:
                raise ValueError("Planned trial count disagrees with expected groups")
            planned = known
        elif planned is not None and known > planned:
            raise ValueError("Planned group counts exceed the planned trial count")
    groups = defaultdict(lambda: {"recorded": 0, "errors": 0, "scored": 0, "passed": 0,
                                  "reward_sum": 0.0, "input_tokens": 0, "output_tokens": 0,
                                  "input_tokens_missing": 0, "output_tokens_missing": 0})
    for key in expected or {}:
        groups[key]
    seen = set()
    for row in rows:
        identifier = row.get("id", row.get("trial_name"))
        if not identifier or identifier in seen:
            raise ValueError("Duplicate or missing trial identity")
        seen.add(identifier)
        agent = row["agent_info"]
        key = (agent["name"], (agent.get("model_info") or {}).get("name", "unknown"), row.get("source") or "adhoc")
        if expected is not None and key not in expected:
            raise ValueError(f"Recorded trial has an unexpected group: {key}")
        group = groups[key]
        group["recorded"] += 1
        error = row.get("exception_info") or any(s.get("exception_info") for s in row.get("step_results") or [])
        group["errors"] += bool(error)
        rewards = (row.get("verifier_result") or {}).get("rewards") or {}
        reward = rewards.get("reward")
        if not error and isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(reward):
            group["scored"] += 1
            group["passed"] += reward == 1
            group["reward_sum"] += reward
        contexts = _contexts(row)
        for field in ("input_tokens", "output_tokens"):
            value = _tokens(contexts, "n_" + field)
            if value is None:
                group[field + "_missing"] += 1
            else:
                group[field] += value
    result = []
    for (agent, model, dataset), group in sorted(groups.items()):
        count = expected[(agent, model, dataset)] if expected is not None else None
        if count is not None and group["recorded"] > count:
            raise ValueError("Recorded trials exceed the planned group count")
        group.update(agent=agent, model=model, dataset=dataset,
                     planned=count, missing=count - group["recorded"] if count is not None else None,
                     unscored=group["recorded"] - group["scored"],
                     mean_reward_scored=group["reward_sum"] / group["scored"] if group["scored"] else None,
                     pass_rate_recorded=group["passed"] / group["recorded"] if group["recorded"] else None,
                     pass_rate_planned=group["passed"] / count if count else None)
        for field in ("input_tokens", "output_tokens"):
            if not group["recorded"] or group[field + "_missing"] == group["recorded"]:
                group[field] = None
        result.append(group)
    total = len(rows)
    if planned is not None and total > planned:
        raise ValueError("Recorded trials exceed the planned trial count")
    passed = sum(group["passed"] for group in result)
    return {"recorded_trials": total, "planned_trials": planned,
            "missing_trials": planned - total if planned is not None else None,
            "pass_rate_planned": passed / planned if planned else None,
            "groups": result, "note": "Error and unscored trials are not passes. Scored-only means exclude errors; no official leaderboard claim."}


def _agent_key(config):
    # These adapter names are inherited/declared by the installed classes, not
    # inferred from Python class names (FrozenCodex, for example, is 'codex').
    identity = config.import_path or config.name
    registered = harness_name(identity)
    if config.name is None and registered:
        name = registered
    else:
        from harbor.agents.factory import AgentFactory
        name = AgentFactory.get_agent_class_from_config(config).name()
    if not isinstance(name, str) or not name:
        raise ValueError("Configured agent class has no valid name")
    model = "unknown"
    if config.model_name:
        # Harbor lazily imports LiteLLM, whose default cost-map load is remote.
        # Override even an explicit false, covering both import and parsing.
        previous = {key: os.environ.get(key) for key in
                    ("LITELLM_LOCAL_MODEL_COST_MAP", "LITELLM_OFFLINE_MODE")}
        try:
            os.environ.update(dict.fromkeys(previous, "True"))
            from harbor.agents.agent_auth_resolution import parse_model_name
            if not config.skip_model_parsing:
                from litellm.litellm_core_utils.get_llm_provider_logic import (
                    declared_authenticating_provider, resolve_model_and_provider_for_metadata,
                )
                # These provider-info paths otherwise perform OAuth. LiteLLM's
                # metadata resolver preserves their exact IDs without logging in.
                if declared_authenticating_provider(config.model_name) is not None:
                    model, _ = resolve_model_and_provider_for_metadata(config.model_name)
                    return name, model
            _, model = parse_model_name(config.model_name, skip_model_parsing=config.skip_model_parsing)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return name, model


def _expected_groups(folder):
    lock_path = folder / "lock.json"
    if lock_path.is_file():
        from harbor.models.job.lock import JobLock
        lock = JobLock.model_validate_json(lock_path.read_text())
        counts = defaultdict(int)
        for trial in lock.trials:
            counts[(*_agent_key(trial.agent), trial.task.source or "adhoc")] += 1
        return dict(counts)
    config_path = folder / "config.json"
    if not config_path.is_file():
        return None
    from harbor.models.job.config import JobConfig
    config = JobConfig.model_validate_json(config_path.read_text())
    if config.source_jobs:
        # Derived jobs need the resolved lock, not the ordinary task matrix.
        return None
    datasets = []
    for dataset in config.datasets:
        source = dataset.path.expanduser().resolve().name if dataset.is_local() else dataset.name
        names = dataset.task_names
        count = None
        # Only an already selected, exact local list is a trustworthy fallback.
        # Never enumerate/download a dataset or interpret a glob as one task.
        if (dataset.is_local() and names and not dataset.exclude_task_names
                and all(not any(c in name for c in "*?[") for name in names)
                and (dataset.n_tasks is None or dataset.n_tasks >= len(names))):
            count = len(set(names)) * config.n_attempts
        datasets.append((source or "adhoc", count))
    datasets.extend((task.source or "adhoc", config.n_attempts) for task in config.tasks)
    counts = {}
    for agent in config.agents:
        for source, count in datasets:
            key = (*_agent_key(agent), source)
            previous = counts.get(key, 0)
            counts[key] = previous + count if previous is not None and count is not None else None
    return counts


def summarize_job(folder, output):
    folder, output = Path(folder).resolve(), Path(output).resolve()
    from harbor.models.trial.result import TrialResult
    rows = {}
    links = []
    evidence = {}

    def add_trial(raw, path):
        # TrialResult supplies a random UUID when omitted; that is not evidence.
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ValueError("Missing trial identity")
        row = TrialResult.model_validate_json(json.dumps(raw), strict=True).model_dump(
            mode="json", context={"redact_sensitive_env": False})
        identifier = row["id"]
        if identifier in rows and rows[identifier] != row:
            raise ValueError(f"Conflicting evidence for trial {identifier}")
        rows[identifier] = row
        links.append((row["trial_name"], _link(path, output)))
        # Prefer the trial's own directory over the job-level embedded copy.
        if path != job_path or identifier not in evidence:
            evidence[identifier] = links[-1][1]

    job_path = folder / "result.json"
    job = json.loads(job_path.read_text()) if job_path.is_file() else {}
    planned = job.get("n_total_trials")
    if planned is not None:
        _count(planned, "Job n_total_trials")
    embedded = job.get("trial_results", [])
    if not isinstance(embedded, list):
        raise ValueError("Job trial_results must be a list")
    for raw in embedded:
        add_trial(raw, job_path)
    for path in sorted(folder.glob("*/result.json")):
        add_trial(json.loads(path.read_text()), path)
    expected = _expected_groups(folder)
    if not rows and not job_path.is_file() and expected is None:
        raise ValueError("No Harbor job or trial results found in the selected directory")
    summary = aggregate(list(rows.values()), planned, expected=expected)
    summary["trials"] = _trial_rows(rows, evidence)
    summary["diagnostics_version"] = DIAGNOSTICS_VERSION
    planned = summary["planned_trials"]
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def cell(value):
        return "<td>" + html.escape(str(value) if value is not None else "—") + "</td>"

    headers = ["Harness", "Model", "Dataset", "Planned", "Recorded", "Missing", "Errors", "Scored", "Passed", "Pass rate (recorded)", "Pass rate (planned)", "Mean reward (scored)"]
    table = "<tr>" + "".join("<th>" + html.escape(h) + "</th>" for h in headers) + "</tr>"
    for g in summary["groups"]:
        values = [g["agent"], g["model"], g["dataset"], g["planned"], g["recorded"], g["missing"], g["errors"], g["scored"], g["passed"], g["pass_rate_recorded"], g["pass_rate_planned"], g["mean_reward_scored"]]
        table += "<tr>" + "".join(cell(v) for v in values) + "</tr>"
    trial_headers = ["Trial", "Task", "Harness", "Model", "Dataset", "Reward", "Error", "Error message", "Duration (s)", "Input tokens", "Output tokens"]
    trials = "<tr>" + "".join("<th>" + html.escape(h) + "</th>" for h in trial_headers) + "</tr>"
    for t in summary["trials"]:
        name = f'<td><a href="{html.escape(t["evidence"], quote=True)}">{html.escape(t["trial"])}</a></td>'
        trials += "<tr>" + name + "".join(cell(t[k]) for k in ("task", "agent", "model", "dataset", "reward", "error", "error_message", "duration_sec", "input_tokens", "output_tokens")) + "</tr>"
    evidence_list = "".join(f'<li><a href="{html.escape(url, quote=True)}">{html.escape(name)}</a></li>' for name, url in links)
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Harness evaluation</title><style>body{font:15px system-ui;margin:24px;max-width:1200px;overflow-wrap:anywhere}.table-scroll{overflow-x:auto}table{border-collapse:collapse}th,td{padding:10px;border:1px solid #ddd;text-align:left;overflow-wrap:normal}th{background:#eef2f7}</style><h1>Harness evaluation</h1>'
    page += f'<p>Recorded: {summary["recorded_trials"]}; planned: {planned}; missing: {summary["missing_trials"]}; pass rate (planned): {summary["pass_rate_planned"]}</p><p>{html.escape(summary["note"])}</p><div class="table-scroll"><table>{table}</table></div>'
    page += f'<h2>Trials</h2><p>Each trial links to its result.json; agent/ and verifier/ in the same directory hold the trajectory and test output.</p><div class="table-scroll"><table>{trials}</table></div><h2>Source evidence</h2><ul>{evidence_list}</ul></html>'
    (output / "report.html").write_text(page, encoding="utf-8")
    return summary
