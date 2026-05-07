"""``bench`` command-line entrypoint.

This module is the only public CLI surface of bench-kit. Subcommands
delegate into pure-Python helpers that are independently testable;
the CLI itself is a thin argparse wrapper.

Stubs for ``run``, ``package``, and ``verify`` are present so that
``bench --help`` matches the documented surface from day one; their
implementations land in a follow-up PR alongside the sandboxed
container exec helper.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from bench_kit import SPEC_VERSION, __version__
from bench_kit.init import InitError, init_battle
from bench_kit.package import PackageError, package_bundle
from bench_kit.run import RunError, RunOptions, run_battle
from bench_kit.validate import validate_battle
from bench_kit.verify import VerifyError, verify_bundle

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_RUNTIME_FAILED = 4


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    return int(handler(args))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench",
        description="krabarena-bench reference CLI",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bench-kit {__version__} (spec {SPEC_VERSION})",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    _add_init(sub)
    _add_validate(sub)
    _add_run(sub)
    _add_package(sub)
    _add_verify(sub)
    return parser


def _add_init(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("init", help="scaffold a new artefact")
    init_sub = p.add_subparsers(dest="kind", metavar="<kind>")

    battle = init_sub.add_parser("battle", help="scaffold a new containerised Battle")
    battle.add_argument("slug", help="slug for the Battle (becomes battles/<slug>/)")
    battle.add_argument(
        "--battle-id",
        required=True,
        help="UUID of the Battle on krabarena.org (from `krab battle create`)",
    )
    battle.add_argument(
        "--out",
        type=Path,
        default=None,
        help="parent directory; battles/<slug>/ is created under it (default: cwd)",
    )
    battle.set_defaults(_handler=_handle_init_battle)


def _add_validate(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("validate", help="run static checks against a Battle directory")
    p.add_argument("battle_dir", type=Path, help="path to battles/<slug>/")
    p.set_defaults(_handler=_handle_validate)


def _add_run(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "run",
        help="execute a Battle's runners and produce result.json",
    )
    p.add_argument("battle_dir", type=Path, help="path to battles/<slug>/")
    p.add_argument(
        "--tools",
        type=str,
        default=None,
        help="comma-separated runner names to execute (default: all)",
    )
    p.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="override results root (default: <battle_dir>/results)",
    )
    p.add_argument(
        "--battle-repo",
        type=str,
        default="github.com/keenableai/krabarena-bench",
        help="canonical repo slug recorded in result.json",
    )
    p.set_defaults(_handler=_handle_run)


def _add_package(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "package",
        help="bundle a result.json into a Claim artefact (claim.tar.gz)",
    )
    p.add_argument("result", type=Path, help="path to result.json")
    p.add_argument(
        "--output",
        type=Path,
        default=Path("claim.tar.gz"),
        help="output bundle path (default: claim.tar.gz in cwd)",
    )
    p.set_defaults(_handler=_handle_package)


def _add_verify(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "verify",
        help="reproduce a Claim bundle and emit a verify-result.json",
    )
    p.add_argument("bundle", type=Path, help="path to claim.tar.gz")
    p.add_argument(
        "--source",
        type=Path,
        required=True,
        help="local checkout of battle_repo at exactly battle_commit",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("verify-result.json"),
        help="output verify-result.json path (default: ./verify-result.json)",
    )
    p.set_defaults(_handler=_handle_verify)


def _add_stub(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
) -> None:
    p = sub.add_parser(name, help=help_text)
    p.set_defaults(_handler=_handle_stub, _stub_name=name)


def _handle_init_battle(args: argparse.Namespace) -> int:
    try:
        target = init_battle(args.slug, args.battle_id, parent_dir=args.out)
    except InitError as exc:
        print(f"bench init: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"created {target}")
    print(
        "next steps:\n"
        "  1. edit meta.yaml — fill in title, tags, optional metrics\n"
        "  2. write tasks/*.yaml and runners/*.py for your tools\n"
        "  3. configure fixtures/compose.yml (internal: true networks)\n"
        "  4. run `bench validate` from the repo root"
    )
    return EXIT_OK


def _handle_validate(args: argparse.Namespace) -> int:
    report = validate_battle(args.battle_dir)
    if report.ok:
        print(f"{report.battle_dir}: ok")
        return EXIT_OK
    for issue in report.issues:
        print(str(issue), file=sys.stderr)
    print(
        f"\n{report.battle_dir}: {len(report.issues)} issue(s); see above",
        file=sys.stderr,
    )
    return EXIT_VALIDATION_FAILED


def _handle_run(args: argparse.Namespace) -> int:
    tools_arg = args.tools
    tools: tuple[str, ...] | None = (
        tuple(t.strip() for t in tools_arg.split(",") if t.strip()) if tools_arg else None
    )
    options = RunOptions(
        tools=tools,
        results_dir=args.results_dir,
        battle_repo=args.battle_repo,
    )
    try:
        out = run_battle(args.battle_dir, options)
    except RunError as exc:
        print(f"bench run: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILED
    print(f"wrote {out}")
    return EXIT_OK


def _handle_package(args: argparse.Namespace) -> int:
    try:
        out = package_bundle(args.result, args.output)
    except PackageError as exc:
        print(f"bench package: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILED
    print(f"wrote {out}")
    return EXIT_OK


def _handle_verify(args: argparse.Namespace) -> int:
    try:
        report = verify_bundle(
            args.bundle,
            source_dir=args.source,
            output_path=args.output,
        )
    except VerifyError as exc:
        print(f"bench verify: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILED
    print(f"verdict: {report.verdict}")
    print(f"wrote {args.output}")
    return EXIT_OK if report.verdict == "match" else EXIT_VALIDATION_FAILED


def _handle_stub(args: argparse.Namespace) -> int:
    print(
        f"bench {args._stub_name}: not yet implemented (lands in next PR)",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
