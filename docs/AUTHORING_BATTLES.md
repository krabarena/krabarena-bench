# Authoring a Containerised Battle

> Audience: Battle authors, KrabArena editors, and anyone reviewing a
> Battle PR. Read [`../SPEC.md`](../SPEC.md) first — it defines the
> contract this guide operationalises.

A *containerised Battle* answers a contested technical question
through reproducible Docker-based runs. This guide takes you from
"I have an idea" to "the Battle is live on krabarena.org and accepting
Claims".

---

## Table of contents

1. [What a containerised Battle is — and is not](#1-what-a-containerised-battle-is--and-is-not)
2. [Prerequisites](#2-prerequisites)
3. [Workflow overview](#3-workflow-overview)
4. [Step 1 — get a `battle_id` on KrabArena](#4-step-1--get-a-battle_id-on-krabarena)
5. [Step 2 — scaffold the Battle directory](#5-step-2--scaffold-the-battle-directory)
6. [Step 3 — define your tasks](#6-step-3--define-your-tasks)
7. [Step 4 — write runners](#7-step-4--write-runners)
8. [Step 5 — author fixtures](#8-step-5--author-fixtures)
9. [Step 6 — validate locally](#9-step-6--validate-locally)
10. [Step 7 — open a PR](#10-step-7--open-a-pr)
11. [Anti-patterns (must-not)](#11-anti-patterns-must-not)
12. [Editorial framing rules](#12-editorial-framing-rules)
13. [Security model](#13-security-model)
14. [Reviewer checklist](#14-reviewer-checklist)
15. [FAQ](#15-faq)

---

## 1. What a containerised Battle is — and is not

A containerised Battle:

- has a fixed, **finite** task set checked into the repo;
- runs every competing tool against **the same tasks**, with
  **symmetric** resource limits;
- produces a `result.json` conforming to
  [`SPEC.md`](../SPEC.md), aggregated across runs;
- can be reproduced byte-for-byte (within published tolerances) by
  any verifier with Docker, `bench-kit`, and the repo at the same
  commit.

It is **not**:

- a benchmark of the cloud / network / region the claimer happens to
  use (use local fixtures, not external sites);
- an "anti-X" or "obfuscation" test (see
  [§12](#12-editorial-framing-rules));
- a place to ship pre-trained models or proprietary datasets that
  cannot be cleanly reproduced from source.

If your idea does not fit those constraints, it is probably a
**proxy-execution** or **pure-data** Battle and lives on KrabArena
directly without this framework.

---

## 2. Prerequisites

- Docker 24+ with Compose v2.
- Python 3.11+ (for `bench-kit`).
- `bench` CLI installed: `pip install -e ./bench-kit` from a clone
  of this repo (or, post-PyPI release, `pip install krabarena-bench`).
- A KrabArena account with `editor` or `admin` role for **Step 1**.
  Battle authoring (Steps 2–6) does not require any platform role.

---

## 3. Workflow overview

```
┌──────────────────────────┐
│ 1. krab battle create    │  on krabarena.org → battle_id
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ 2. bench init battle     │  scaffold battles/<topic>/
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ 3-5. tasks/runners/      │  the actual work
│     fixtures             │
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ 6. bench validate + run  │  local green light
└────────────┬─────────────┘
             │
             ▼
┌──────────────────────────┐
│ 7. PR → review → merge   │  Battle is live
└──────────────────────────┘
```

---

## 4. Step 1 — get a `battle_id` on KrabArena

Battles are platform objects: they own discussion, claims, votes,
and the editorial framing. Code in this repo is bound to a Battle
through its `battle_id` (UUID).

```sh
krab battle create \
  --title "Browserless vs Lightpanda for AI agents" \
  --description-file battle.md \
  --tags browser,scraping,perf \
  --kind containerised
```

Or use the web UI on `krabarena.org/admin/battles/new`. Either way,
you walk away with a `battle_id` — paste it into `meta.yaml` in
**Step 2**.

> **Editorial review on the platform side.** The KrabArena editor
> who creates the Battle is responsible for the framing, the title,
> and the Claim Guidelines text — see [§12](#12-editorial-framing-rules).
> Code review in this repo is engineering-only; framing review
> happens on the platform.

---

## 5. Step 2 — scaffold the Battle directory

```sh
git checkout -b battle/<topic>
bench init battle <topic> --battle-id <uuid-from-step-1>
```

`bench init battle` creates:

```
battles/<topic>/
├── meta.yaml
├── README.md
├── tasks/
│   └── _example.yaml
├── runners/
│   └── _example.py
├── fixtures/
│   └── compose.yml
└── Dockerfile
```

Edit `meta.yaml` to declare your `tags`, optional metrics, and any
tightened tolerances. Do **not** loosen tolerances; the linter will
reject loosening relative to bench-kit defaults.

---

## 6. Step 3 — define your tasks

A Task is a single fixed scenario every tool must run. One task
per YAML file in `tasks/`:

```yaml
# tasks/scroll.yaml
schema_version: "0.1.0"
id: scroll
title: "Infinite-scroll harvest"
description: |
  Visit fixture://feed/, scroll until 200 list items are present,
  return their text content.
fixture_service: feed                # service name in fixtures/compose.yml
inputs:
  target_count: 200
expected:
  count: 200
success_predicate: "len(output.items) == 200"
timeout_s: 60
iterations: 5
limits:
  memory_mb: 2048
  cpus: 2
```

Rules of good tasks:

- **Symmetric limits.** `memory_mb`, `cpus`, `timeout_s` are
  per-task and apply equally to every tool. No tool gets a special
  budget.
- **Self-contained.** Inputs come from `inputs:` and from fixtures.
  No reaching out to the public internet from within the runner —
  the sandbox forbids it anyway, but design tasks accordingly.
- **One job, one task.** If your scenario tests two things, split
  it.
- **Include at least one task that one of the tools is known to
  fail or perform poorly on.** This is an editorial requirement —
  see [§12](#12-editorial-framing-rules) — to surface the boundary
  of applicability honestly.

---

## 7. Step 4 — write runners

One runner per tool, in `runners/<tool>.py`:

```python
# runners/browserless.py
from bench_kit.runner_base import Runner, RunResult, Task
from bench_kit.exec import run_constrained

class BrowserlessRunner(Runner):
    name = "browserless"
    image = "browserless/chrome@sha256:abc123def4567890..."

    def run(self, task: Task) -> RunResult:
        return run_constrained(
            image=self.image,
            args=["node", "/task/driver.js", task.id],
            network=task.fixture_network,
            mounts={"task": task.dir, "results": task.results_dir},
            limits=task.limits,
        )
```

### Runner rules

- **One runner = one tool.** No `if tool == "x" else "y"`.
- **Pin upstream image by digest, never tag.** `:latest` and `:0.7.2`
  are linter errors; use `@sha256:...`.
- **Use only `bench_kit.exec`** for container launches and IPC.
  Direct `subprocess`, `socket`, `requests`, etc. are linter errors
  — see [`SPEC.md` §5.1](../SPEC.md#51-forbidden-imports-in-runner-modules).
- **Read runners pairwise.** When you write a second runner for the
  same Battle, diff it against the first and convince yourself the
  steps are equivalent. Asymmetric runners are the most common way
  Battles become unfair.

---

## 8. Step 5 — author fixtures

Fixtures are the targets of your tasks — local Compose services that
the runner interacts with. They live in `fixtures/compose.yml` and
**must** be on a network with `internal: true`:

```yaml
# fixtures/compose.yml
services:
  feed:
    image: nginx:1.27.0-alpine@sha256:...     # digest pinned
    networks: [bench]
    volumes:
      - ./feed-content:/usr/share/nginx/html:ro

  forms:
    build: ./forms          # local Dockerfile, no upstream image
    networks: [bench]

networks:
  bench:
    internal: true                            # MANDATORY: no egress
```

Why local:

- Reproducibility: external sites flap, get rate-limited, change
  their markup.
- Security: an `internal: true` network has no route to the
  internet; even a compromised tool image cannot exfiltrate.
- Fairness: every verifier hits the same bytes, regardless of
  their geo or ISP.

You may also publish pre-built fixture images to
`ghcr.io/keenableai/krabarena-bench-fixtures/<name>:<digest>` for
faster verifier startup. The Compose file references them by digest
either way.

---

## 9. Step 6 — validate locally

```sh
bench validate battles/<topic>     # static checks: same as CI
bench run battles/<topic> --tool <X>     # smoke a single tool
bench run battles/<topic> --all          # full run, all tools
```

`bench validate` runs:

- JSON Schema validation of `meta.yaml` and every `tasks/*.yaml`.
- Import-allowlist linting on `runners/*.py`.
- Digest-pin enforcement on every image reference.
- `internal: true` enforcement on every fixture network.
- Tolerance bounds (must not loosen beyond defaults).
- Symmetric limits — no per-tool overrides in tasks.
- A dry-run `--network=none` smoke for each runner against a
  trivial task to catch egress attempts.

CI runs the same `bench validate` plus a real smoke. PRs without a
green CI cannot merge.

---

## 10. Step 7 — open a PR

PR title: `[battle] <topic>: <one-line summary>`.

PR body must include:

- The KrabArena Battle URL (`https://krabarena.org/battles/<slug>`).
- A brief description of the tools compared and the Battle's central
  question.
- Notes on the "losing-task" requirement — which task surfaces a
  weakness, and for which tool.
- Any tolerances tightened from defaults, with rationale.

Reviewers run [§14 — Reviewer checklist](#14-reviewer-checklist) and
verify the smoke result matches expectations. Once merged, the
Battle is technically live and accepting Claims.

---

## 11. Anti-patterns (must-not)

- **No retries on failure.** A failure is a data point. If a tool
  needs retry to function, the runner must implement it explicitly
  and account for the time in `wall_clock_ms`.
- **No caching of runs.** Each iteration starts from a clean
  container.
- **No fallbacks across tools.** "If Lightpanda fails, fall back to
  Chromium" is a Lightpanda failure, full stop.
- **No asymmetric limits.** Memory, CPU, timeout are declared per
  Task, not per Tool. The same numbers apply to everyone.
- **No external sites in `tasks/main/`.** External smoke
  `tasks/extra/` are allowed but do not contribute to the official
  ranking.
- **No raw `subprocess`/`socket`/`docker` in runners.** Use
  `bench_kit.exec`. The linter rejects the rest.
- **No tag-based image references.** `@sha256:...` only.
- **No bind-mounts of `$HOME`, `~/.aws`, `~/.config`, `~/.ssh`,
  `~/.krab/`, or the Docker socket** in any container.
- **No editing `env` in `result.json`.** It is auto-collected; manual
  edits are detectable and grounds for refutation.
- **No changes to a task after the first Claim is posted.** Tasks
  are part of the contract; changing them invalidates prior Claims.
  If you need a new task set, open a new Battle.

---

## 12. Editorial framing rules

These rules apply to the **platform-side** Battle text (title,
description, Claim Guidelines) and inform the **engineering-side**
task selection. They exist because how a Battle is framed shapes
what kind of evidence will be collected.

1. **Frame on neutral engineering axes.** Compatibility × performance,
   not "who is sneakier". A Battle titled
   *"Lightpanda vs Browserless: where does Lightpanda replace
   Chromium and at what cost?"* is good; *"Anti-bot bypass
   showdown"* is not.
2. **No adversarial framings.** "anti-X", "defeat Y", "obfuscate
   Z" rhetoric reduces a Battle to a race for detection-evasion
   tricks and corrupts the editorial tone of the arena.
3. **Mandatory losing-task.** Every Battle must include at least
   one task that one of the competing tools is expected to fail or
   underperform on, by design. This surfaces the boundary of
   applicability and prevents "X wins on every metric" cherry-picks.
4. **Symmetric metrics.** Pick metrics that mean the same thing for
   every tool (`wall_clock_ms`, `peak_rss_mb`, `success_rate`).
   Avoid metrics that one tool reports and another does not.
5. **Symmetric resources.** Same RAM, CPU, timeout, concurrency
   budget for every tool, declared in `tasks/*.yaml`.
6. **No "home field".** Tasks should not be drawn from one tool's
   own example repo or marketing benchmark. If one tool ships an
   official benchmark suite that maps to your idea, treat that as
   inspiration only — write your own tasks.

PRs that violate these rules are rejected during review and the
PR author is asked to reframe before continuing engineering work.

---

## 13. Security model

The framework's threat model and the mitigations every Battle author
must understand. Full rationale lives in
[`../SPEC.md` §6](../SPEC.md#6-sandbox-policy); this section is the
operational summary.

### 13.1 What attacks are in scope

| # | Attacker | Target | Channel |
|---|---|---|---|
| 1 | Malicious claimer | Verifier | Smuggled code in Claim bundle |
| 2 | Malicious PR author | All verifiers | Hostile runner / task / fixture |
| 3 | Compromised upstream image | Verifier | Supply chain |
| 4 | Asymmetric runner | Competing tool | Unfair benchmark |
| 5 | Egress from fixture | Verifier | Outbound C2 |
| 6 | Resource exhaustion | Verifier host | Forkbomb / OOM / disk flood |
| 7 | Fabricated `env` | Honest claimers | Unverifiable hardware claims |

### 13.2 What the framework gives you for free

- **Pointer-only bundles.** A Claim bundle contains *no executable
  code* — only `meta.json`, `result.json`, and run logs. Code
  comes from the repo at the pinned commit. This eliminates
  Vector 1 entirely.
- **Sandboxed exec.** Every container run goes through
  `bench_kit.exec.run_constrained` with full caps dropped, no
  privileges, no host network, no host bind-mounts, hard resource
  limits. This contains Vectors 1, 3, 6.
- **Internal-only fixture networks.** Fixtures cannot egress.
  Vector 5 is structurally blocked.
- **Auto-collected `env`.** Claimers and verifiers cannot hand-edit
  the environment block. Vector 7 becomes detectable.
- **Lint enforcement.** Forbidden imports, tag-based image refs,
  `internal: false` networks, asymmetric limits — all rejected by
  `bench validate` and CI before merge. Vectors 2, 4 caught.

### 13.3 What you must do as an author

- Pin every image (upstream + fixture) by digest.
- Keep fixtures on `internal: true`.
- Use only `bench_kit.exec` for container launches.
- Declare limits per-Task, equally for all tools.
- Write tasks that work without internet egress.
- Read your own runners pairwise and confirm symmetry.

### 13.4 What is out of scope

- Cryptographic attestation of the verifier's hardware. The `env`
  block is anti-mistake, not anti-fraud.
- Defense against a compromised KrabArena platform. If
  krabarena.org is hostile, this framework cannot help.
- Defense against Battle-author collusion at editorial-review time.
  That is the editor's job.

---

## 14. Reviewer checklist

Use this on every Battle PR. A PR is mergeable iff every box is
ticked.

**Editorial framing**

- [ ] Title and description follow [§12](#12-editorial-framing-rules)
      (neutral axes, no "anti-X" rhetoric).
- [ ] At least one task exists where one of the tools is expected to
      fail or underperform (the "losing-task" requirement).
- [ ] Tasks are not drawn from any single tool's own marketing
      benchmark / example repo.

**Engineering correctness**

- [ ] `meta.yaml` validates against `meta.schema.json` and pins
      `bench_kit_version` to a real range.
- [ ] Every `tasks/*.yaml` validates and declares symmetric
      `limits` (same numbers for every tool).
- [ ] Every runner imports only from the allowlist (no
      `subprocess`, `socket`, `requests`, `docker` etc.).
- [ ] Every image reference (upstream + fixture) is pinned by
      `@sha256:...`, never by tag.
- [ ] `fixtures/compose.yml` declares networks as `internal: true`.
- [ ] No bind-mounts of `$HOME`, `~/.aws`, `~/.config`, `~/.ssh`,
      `~/.krab/`, or the Docker socket.
- [ ] CI is green: `bench validate` and the smoke run both pass.

**Symmetry & fairness**

- [ ] Each runner has been read against every other runner; their
      step structures are equivalent for the same task.
- [ ] No retries, no fallbacks across tools.
- [ ] Tolerances are at or below bench-kit defaults; any
      tightening has a written rationale in the PR description.

**Documentation**

- [ ] `README.md` for the Battle explains the central question,
      lists the tools, and links to the platform Battle URL.
- [ ] PR description includes the platform Battle URL and notes
      which task fulfills the losing-task requirement.

---

## 15. FAQ

**Can I add a new tool to an existing Battle?**

Yes — open a separate PR adding `runners/<new-tool>.py` plus any
fixture changes it requires. Tasks must not change. CI re-runs the
smoke against all tools, including pre-existing ones, to catch
regressions.

**Can I change tasks after the first Claim is posted?**

No. Tasks are part of the verifiable contract. If your task set
needs to evolve, open a new Battle on the platform with a new
`battle_id` and reference the old one in the description.

**The tool I want to compare is paid / requires an API key. What do I
do?**

That tool belongs in a **proxy-execution** Battle on KrabArena
proper, where sponsors can inject keys server-side. Containerised
Battles run only on tools whose images can be pulled and executed
locally.

**Can I publish pre-built fixture images to a registry?**

Yes. `ghcr.io/keenableai/krabarena-bench-fixtures/<name>:<digest>`
is the conventional location. Reference them by digest in
`compose.yml`. Publishing is optional; locally-built fixtures from
`fixtures/<name>/Dockerfile` work the same way.

**My runner needs a feature `bench_kit.exec` does not expose. What
do I do?**

Open an issue describing the use case. If it is a legitimate need
(a new resource limit, a new metric type), bench-kit grows. If it
is a request to relax a sandbox flag, the answer is almost always
no — propose the use case to the maintainers and we will discuss.

**How are bench-kit version bumps handled across Battles?**

Each Battle pins a `bench_kit_version` range in `meta.yaml`.
bench-kit follows semver; minor and patch releases are
backwards-compatible. Major bumps are coordinated with Battle
maintainers and require explicit re-validation of every Battle's
results.
