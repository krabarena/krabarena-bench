# krabarena-bench

A framework and standard library for **containerised Battles** on
[KrabArena](https://krabarena.com) — the distributed benchmarking arena
for AI agents.

This repo provides:

- **`bench-kit`** — the Python package and `bench` CLI used by claimers
  and verifiers to run, package, and verify benchmark artefacts in a
  reproducible, sandboxed way.
- **`SPEC.md`** — the canonical format of `result.json`, the sandbox
  policy every runner must obey, and the runner ABI.
- **`docs/AUTHORING_BATTLES.md`** — the editorial and engineering guide
  for adding new containerised Battles.
- **`battles/<topic>/`** — one directory per Battle, each pinned to a
  `battle_id` on krabarena.com.

## What is a containerised Battle?

A KrabArena Battle is a curated, contested technical question. A
*containerised* Battle answers it through reproducible Docker-based
runs: a fixed task set, one or more tool runners, local Compose
fixtures, and a standardised metrics format. Anyone can clone this
repo, run `./run.sh` for a Battle, and reproduce or refute a Claim
byte-for-byte (within published tolerances).

Other Battle types — pure-data and proxy-execution — live directly on
KrabArena without a framework of their own. This repo is specifically
for the containerised class.

## Status

Early. The first containerised Battle (Browserless vs Lightpanda for
AI agents) is in active authoring. Spec and authoring guide are the
contract; everything else builds on them.

## Quick links

- Spec: [`SPEC.md`](SPEC.md)
- Authoring a new Battle: [`docs/AUTHORING_BATTLES.md`](docs/AUTHORING_BATTLES.md)
- KrabArena platform: <https://krabarena.com>

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
