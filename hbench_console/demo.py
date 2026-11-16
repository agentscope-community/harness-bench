"""Generate clearly-labeled fixture evidence so the console can be exercised offline.

Every job written here contains a fixture.json marker and fake trial results;
the UI shows a banner for it. Nothing in this module calls a model or Harbor.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import random
from pathlib import Path
import uuid

from harbor.models.job.config import JobConfig
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import AgentInfo, ExceptionInfo, TrialResult
from harbor.models.verifier.result import VerifierResult

from .config import REPO_ROOT, Settings

HARNESSES = [("codex", "hbench.agents:FrozenCodex", "0.167.2"),
             ("pi", "hbench.agents:FrozenPi", "1.5.2"),
             ("agentscope-java", "hbench.agents:AgentScopeJava", "1.4.0")]
MODELS = ["fixture-model-a", "fixture-model-b"]
DATASETS = {"terminal-bench": 0.62, "swe-bench-verified": 0.54, "tau3-bench": 0.70}
# Per-agent offsets make the heatmap show meaningful differences.
ADJUST = {"codex": 0.06, "pi": 0.0, "agentscope-java": -0.10}


def build(out: Path, tasks_per_dataset: int, seed: int) -> dict:
    if out.exists():
        raise SystemExit(f"拒绝覆盖已存在的目录: {out}")
    out.mkdir(parents=True)
    rng = random.Random(seed)
    (out / "fixture.json").write_text(json.dumps({
        "fixture": True,
        "note": "演示数据：通过伪随机奖励生成，仅用于验证控制台界面。",
    }, ensure_ascii=False, indent=2) + "\n")
    tasks = {name: [f"{name.split('-')[0]}-task-{i:02d}" for i in range(1, tasks_per_dataset + 1)]
             for name in DATASETS}
    config = JobConfig(
        job_name=out.name,
        agents=[{"import_path": import_path, "model_name": model, "skip_model_parsing": True}
                for _, import_path, _ in HARNESSES for model in MODELS],
        datasets=[{"path": str(REPO_ROOT / "artifacts" / "datasets" / name), "task_names": names}
                  for name, names in tasks.items()])
    (out / "config.json").write_text(json.dumps(config.model_dump(mode="json"), indent=2) + "\n")
    planned = len(HARNESSES) * len(MODELS) * sum(len(names) for names in tasks.values())
    (out / "result.json").write_text(json.dumps({
        "job_name": out.name, "n_total_trials": planned, "trial_results": [],
        "fixture": True}, ensure_ascii=False, indent=2) + "\n")

    written, skipped = 0, 0
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for dataset, source_rate in DATASETS.items():
        for agent, import_path, version in HARNESSES:
            for model_index, model in enumerate(MODELS):
                for task_index, task in enumerate(tasks[dataset]):
                    # Leave a few holes so "missing" is exercised as well.
                    if (task_index + model_index) % 17 == 0:
                        skipped += 1
                        continue
                    chance = source_rate + ADJUST[agent] + (0.04 if model_index == 0 else 0)
                    roll = rng.random()
                    error = roll > 0.95
                    unscored = not error and roll > 0.92
                    reward = None if unscored else (1.0 if roll < chance else 0.0)
                    trial_name = f"{task[:32]}__{uuid.uuid4().hex[:7]}"
                    folder = out / trial_name
                    folder.mkdir()
                    started = base + timedelta(minutes=rng.randint(0, 90))
                    finished = started + timedelta(seconds=rng.randint(20, 900))
                    trial = TrialResult(
                        task_name=task, trial_name=trial_name,
                        trial_uri=folder.as_uri(), source=dataset,
                        task_id=LocalTaskId(path=str(REPO_ROOT / "artifacts" / "datasets" / dataset / task)),
                        task_checksum=f"fixture-{dataset}-{task}",
                        config=TrialConfig(task={"path": str(folder)}, trial_name=trial_name),
                        agent_info=AgentInfo(name=agent, version=version,
                                             model_info={"name": model}),
                        agent_result={"n_input_tokens": rng.randint(1200, 9000),
                                      "n_output_tokens": rng.randint(200, 3000)},
                        verifier_result=None if unscored else VerifierResult(rewards={"reward": reward}),
                        exception_info=ExceptionInfo(
                            exception_type="AgentTimeoutError",
                            exception_message="fixture timeout",
                            exception_traceback="fixture: no real execution",
                            occurred_at=started + timedelta(seconds=30)) if error else None,
                        started_at=started, finished_at=finished)
                    (folder / "result.json").write_text(trial.model_dump_json(indent=2) + "\n")
                    (folder / "trial.log").write_text(
                        f"fixture trial {trial_name}: {agent} / {model} / {task}\n", encoding="utf-8")
                    (folder / "agent").mkdir()
                    (folder / "agent" / "native.jsonl").write_text(json.dumps({
                        "fixture": True, "event": "trial", "task": task}) + "\n", encoding="utf-8")
                    (folder / "agent" / "trajectory.json").write_text(json.dumps({
                        "fixture": True, "task": task, "agent": agent, "model": model,
                        "messages": [{"role": "user", "content": "fixture task prompt"},
                                     {"role": "assistant", "content": "fixture answer"}]},
                        ensure_ascii=False, indent=2) + "\n")
                    (folder / "verifier").mkdir()
                    (folder / "verifier" / "output.txt").write_text(
                        f"fixture verifier: reward={reward}\n", encoding="utf-8")
                    written += 1
    return {"job": out.name, "path": str(out), "planned_trials": planned,
            "recorded_trials": written, "missing_trials": planned - written, "skipped": skipped}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate fixture evidence for the console")
    parser.add_argument("--name", help="Job directory name; default demo-fixture-<timestamp>")
    parser.add_argument("--tasks-per-dataset", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--jobs-root", type=Path)
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    root = (args.jobs_root or settings.jobs_root).resolve()
    name = args.name or "demo-fixture-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    if not name.startswith("demo-fixture-"):
        raise SystemExit("演示作业名称必须以 demo-fixture- 开头，避免与真实结果混淆")
    print(json.dumps(build(root / name, max(1, args.tasks_per_dataset), args.seed),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
