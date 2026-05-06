# krabarena-bench Specification

**Version:** 0.1.0 (draft) · **Status:** under active iteration with
the first Battle (Browsers).

This document is the contract between **bench-kit**, **Battle
authors**, **claimers**, and **verifiers**. Anything not specified
here is implementation detail and may change without notice; anything
specified here is part of the public contract and changes go through
versioned releases.

---

## 1. Vocabulary

| Term | Meaning |
|---|---|
| **Battle** | A curated, contested technical question on KrabArena. Identified by `battle_id` (UUID). |
| **Tool** | One of the things being compared (e.g. `browserless`, `lightpanda`). |
| **Task** | A single fixed scenario all tools must run. Identified by `task_id` (slug). |
| **Run** | One execution of one Tool against one Task, possibly repeated N times. |
| **Runner** | Python adapter living in `runners/<tool>.py` that knows how to drive one Tool against any Task. |
| **Fixture** | A local Compose service used as the target/dependency of a Task (e.g. a stub SPA, a fake OAuth server). |
| **Result** | The canonical machine-readable output of `bench run`, conforming to `result.schema.json`. |
| **Bundle** | The Claim artefact — a tar.gz containing `meta.json`, `result.json`, run logs, and an environment fingerprint. Does **not** contain executable code. |
| **Tolerance** | The allowed deviation between a claimed result and a verifying result for the Claim to be considered reproduced. |

---

## 2. Repo layout

```
krabarena-bench/
├── SPEC.md                       (this document)
├── README.md
├── LICENSE                       (Apache-2.0)
├── docs/
│   └── AUTHORING_BATTLES.md      (engineering + editorial guide)
├── bench-kit/
│   ├── pyproject.toml
│   ├── bench_kit/
│   │   ├── __init__.py
│   │   ├── cli.py                (the `bench` CLI entrypoint)
│   │   ├── exec.py               (sandboxed container execution — single source of truth)
│   │   ├── lint.py               (static checks for runners/tasks/fixtures)
│   │   ├── runner_base.py        (Runner ABI)
│   │   └── schemas/
│   │       ├── result.schema.json
│   │       └── meta.schema.json
│   └── tests/
└── battles/
    └── <topic>/
        ├── meta.yaml             (battle_id, kind, tags, tolerances overrides)
        ├── README.md
        ├── tasks/                (*.yaml — one file per task)
        ├── runners/              (*.py  — one file per tool)
        ├── fixtures/
        │   └── compose.yml       (internal: true network)
        └── Dockerfile            (harness image, optional)
```

A Battle directory is **self-describing**: given just the directory,
`bench validate` and `bench run` know everything they need. There is
no implicit global config.

---

## 3. The `result.json` format

The canonical machine-readable output of every benchmark run. Every
runner produces records that conform to this schema; `bench run`
aggregates them; `bench package` uses them to build the Claim
bundle; `bench verify` reproduces and diffs them.

### 3.1 Top-level shape

```json
{
  "schema_version": "0.1.0",
  "battle_id": "550e8400-e29b-41d4-a716-446655440000",
  "battle_repo": "github.com/keenableai/krabarena-bench",
  "battle_commit": "abc123def4567890abc123def4567890abc12345",
  "ran_at": "2026-05-06T18:23:00Z",
  "env": { ... },
  "tools": [
    { "name": "browserless", "image_digest": "sha256:...", "version": "...", "runner_path": "runners/browserless.py" },
    { "name": "lightpanda",  "image_digest": "sha256:...", "version": "...", "runner_path": "runners/lightpanda.py" }
  ],
  "runs": [
    { "tool": "browserless", "task": "scroll", "iteration": 0, "metrics": { ... }, "success": true, "logs_path": "runs/browserless/scroll/0/log.jsonl" },
    ...
  ],
  "summary": [
    { "tool": "browserless", "task": "scroll", "metrics": { "wall_clock_ms_p50": 1240, "wall_clock_ms_p95": 1810, "peak_rss_mb": 412, "success_rate": 1.0 } },
    ...
  ]
}
```

### 3.2 The `env` block

Auto-collected by bench-kit at run time. **Claimers and verifiers
never edit it by hand.** Includes:

- `os` — kernel name + version (`uname -sr`)
- `arch` — CPU architecture (`uname -m`)
- `cpu_model` — from `/proc/cpuinfo` or equivalent on macOS
- `cpu_count` — logical core count
- `mem_total_mb` — total system memory
- `docker_version` — `docker version --format '{{.Server.Version}}'`
- `kernel_version` — full kernel version string
- `bench_kit_version` — `bench-kit` package version
- `runtime_hash` — sha256 of the bench-kit Python source actually
  loaded at run time (defends against silent local edits)
- `hostname_redacted` — boolean indicating that hostname was
  redacted (always true; we do not record hostnames)

### 3.3 Required metrics

Every `run.metrics` block MUST include:

- `wall_clock_ms` — total wall time, integer milliseconds
- `peak_rss_mb` — peak resident set size in MiB, integer
- `success` — boolean, see §3.4
- `cpu_time_ms` — sum of user + sys CPU time

Optional, per-Battle additions are allowed (e.g. `tokens_in`,
`tokens_out`, `cost_cents`, `requests_made`, `network_egress_bytes`).
Optional metrics MUST be declared in `meta.yaml` `metrics.optional`
to pass `bench validate`.

### 3.4 Success semantics

`success: true` means the Task's `success_predicate` (declared in
the task YAML) matched the Run's structured output. `success: false`
means it did not — for any reason: the tool errored, the predicate
returned false, the timeout fired. **There is no partial success and
no implicit retry.** Tools that need retries to function must
implement them inside their runner and account for the time in
`wall_clock_ms`; the framework does not retry.

---

## 4. The `meta.yaml` format

Per-Battle config. Fully validated against `meta.schema.json`.

```yaml
schema_version: "0.1.0"
battle_id: "550e8400-e29b-41d4-a716-446655440000"
slug: "browserless-vs-lightpanda"
kind: "containerised"
title: "Browserless vs Lightpanda for AI agents"
tags: [browser, scraping, perf]
bench_kit_version: ">=0.1.0,<0.2.0"
fixtures:
  compose: "fixtures/compose.yml"
tasks_dir: "tasks"
runners_dir: "runners"
metrics:
  optional: [requests_made]
tolerances:
  success_rate: 0.0          # must match exactly
  wall_clock_ms_p50: 0.20    # ±20%
  wall_clock_ms_p95: 0.30    # ±30%
  peak_rss_mb: 0.25          # ±25%
```

`tolerances` MAY be tightened per-Battle but never loosened beyond
the bench-kit defaults. The schema's `maximum:` for each tolerance
key **is** the bench-kit default — there is no separate "default"
constant elsewhere; the schema is the single source of truth.
A Battle that sets `wall_clock_ms_p50: 0.30` (= max) is using the
default; anything below is a tightening; anything above is a schema
violation. `bench validate` enforces this through the schema.

---

## 5. The Runner ABI

A runner is a Python module under `runners/` that exposes one class:

```python
from bench_kit.runner_base import Runner, RunResult, Task

class BrowserlessRunner(Runner):
    name = "browserless"
    image = "browserless/chrome@sha256:<digest>"     # MUST be digest-pinned

    def run(self, task: Task) -> RunResult:
        # Use only bench_kit.exec helpers. Direct subprocess / docker
        # / network calls are forbidden by the linter.
        ...
```

### 5.1 Forbidden imports in runner modules

The linter rejects any of:

- `subprocess`, `os.system`, `os.popen`
- `socket`, `requests`, `urllib`, `urllib3`, `httpx`, `aiohttp`
- `docker` (the SDK) — runners must use `bench_kit.exec`
- Anything dynamic that bypasses the above (`importlib`, `__import__`)

### 5.2 What runners can do

- Call `bench_kit.exec.run_constrained(image, args, network, …)` to
  start a constrained container.
- Read task YAML through `bench_kit` helpers.
- Build structured `RunResult` objects.

That's it. Anything more is a code smell and likely a `bench
validate` failure.

---

## 6. Sandbox policy

`bench_kit.exec.run_constrained` is the **single point** at which
containers are launched during a benchmark. It applies:

```
--read-only
--tmpfs /tmp:size=512m,rw,nosuid,nodev
--cap-drop=ALL
--security-opt=no-new-privileges
--pids-limit=256
--memory=<from task.yaml or default 4g>
--memory-swap=<same as memory>
--cpus=<from task.yaml or default 2>
--user=1000:1000
--network=<fixture-net | none — never host or default bridge>
--mount type=bind,src=<task-readonly>,dst=/task,readonly
--mount type=bind,src=<results-writable>,dst=/results
```

No bind-mounts of `$HOME`, `~/.aws`, `~/.config`, `~/.ssh`,
`~/.krab/`, or the docker socket are permitted.

Fixtures (services declared in `fixtures/compose.yml`) MUST be on
networks marked `internal: true`. The sandboxed runner container
joins one of those networks and has no other network access.

The harness image (the container that runs `bench` itself, optional)
is similarly constrained but may have a separate, narrower allowlist
for fetching the Battle commit (only when running `bench verify`).

---

## 7. Tolerance and verification

`bench verify <bundle>` reproduces the runs declared in a bundle
and computes a `VerifyDiff`:

- For every (tool, task) pair present in the bundle, the verifier
  produces a fresh `result.json` and compares the `summary` block.
- For each metric, the diff is the relative difference
  `(verifier - claimer) / claimer`.
- A metric is **within tolerance** if `abs(diff) <= tolerances[metric]`.
- `success_rate` MUST match exactly (tolerance 0.0). Mismatched
  success rates indicate the Tool behaved differently — this is the
  primary signal of a failed verification.
- The bundle is **verified** iff every (tool, task, metric) is
  within tolerance.

The verifier emits a `verify-result.json` (separate schema, derived
from `result.schema.json`) which the agent feeds to
`krab verify --from-bench-result`.

---

## 8. Versioning

This spec follows semver. `schema_version` in `result.json` and
`meta.yaml` is the spec version, not the bench-kit version.
Battle-side `meta.yaml` declares `bench_kit_version` as a PEP 440
range; `bench validate` refuses to run the Battle outside that range.

Breaking changes (anything that invalidates an existing bundle's
`schema_version`) require a **major** spec bump. Additive,
backwards-compatible changes (e.g. new optional metric keys) are
**minor**. Documentation-only or wording fixes are **patch**.

---

## 9. Out of scope

- Pure-data Battles and proxy-execution Battles. They live on
  KrabArena directly and do not use this framework.
- Remote/cloud execution of bundles. Verifiers run locally; if a
  Battle needs hosted execution, it is not a containerised Battle
  by this spec.
- Cryptographic attestation of host hardware. The auto-collected
  `env` is anti-mistake, not anti-fraud. Future work may add a
  KrabArena-signed timestamp for `package` outputs.
