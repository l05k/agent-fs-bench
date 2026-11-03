#!/usr/bin/env python3
"""Regenerate the mini-dev-workflow source repository fixture.

Upstream ``YoloFS/perf-eval`` keeps its ``source-repo`` as a Git submodule
(``fixtures/mini-dev-workflow/source-repo``) that was later deleted, so this
repository only ships the *resulting* tree under
``data/mini-dev-workflow/repo/``. The benchmark needs the history, not just the
final tree, so this tool rebuilds it deterministically:

1. write the base tree (``hello v1``, no helper),
2. replay the checked-in ``*-edit.cmds`` fixtures and commit each step,
3. verify the final tree matches ``data/mini-dev-workflow/repo/``,
4. rewrite ``series.json`` with the generated commit ids, and
5. export ``source-repo.bundle`` so the repository stays distributable.

The generated ``source-repo/`` directory itself is ignored by Git; commit the
bundle and ``series.json`` instead. Dates and identities are pinned, so
re-running the tool produces the same commit ids.

    uv run tools/gen_mini_source_repo.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
FIXTURE_DIR = REPO_ROOT / "data" / "mini-dev-workflow"
SERIES = FIXTURE_DIR / "series.json"
REFERENCE = FIXTURE_DIR / "repo"
SOURCE_REPO = FIXTURE_DIR / "source-repo"
BUNDLE = FIXTURE_DIR / "source-repo.bundle"

PINNED_DATE = "2026-03-30T00:00:00Z"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "mini-dev-workflow fixture",
    "GIT_AUTHOR_EMAIL": "fixture@mini-dev-workflow.invalid",
    "GIT_COMMITTER_NAME": "mini-dev-workflow fixture",
    "GIT_COMMITTER_EMAIL": "fixture@mini-dev-workflow.invalid",
    "GIT_AUTHOR_DATE": PINNED_DATE,
    "GIT_COMMITTER_DATE": PINNED_DATE,
}

BASE_MESSAGE = "initial import"

BASE_FILES = {
    "Makefile": (
        "CC ?= gcc\n"
        "CFLAGS ?= -O2\n"
        "\n"
        "all: src/hello\n"
        "\n"
        "src/hello.o: src/hello.c src/hello.h\n"
        "\t$(CC) $(CFLAGS) -c src/hello.c -o src/hello.o\n"
        "\n"
        "src/hello: src/hello.o\n"
        "\t$(CC) src/hello.o -o src/hello\n"
        "\n"
        "clean:\n"
        "\trm -f src/hello.o src/hello\n"
    ),
    "src/hello.h": "#pragma once\n\nconst char *message(void);\n",
    "src/hello.c": (
        "#include <stdio.h>\n"
        '#include "hello.h"\n'
        "\n"
        "const char *message(void) {\n"
        '    return "hello v1";\n'
        "}\n"
        "\n"
        "int main(void) {\n"
        "    puts(message());\n"
        "    return 0;\n"
        "}\n"
    ),
}


class FixtureError(RuntimeError):
    """The fixture could not be generated or does not match its reference."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env={**os.environ, **GIT_IDENTITY},
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise FixtureError(
            f"git {' '.join(args)} failed with exit code {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def load_commands(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FixtureError(f"cannot read command file {path}: {exc}") from exc

    if "%%" in lines:
        commands: list[str] = []
        current: list[str] = []
        for line in lines:
            if line == "%%":
                if current:
                    commands.append("\n".join(current) + "\n")
                    current = []
                continue
            if not current and line.lstrip().startswith("#"):
                continue
            current.append(line)
        if current:
            commands.append("\n".join(current) + "\n")
        return commands

    commands = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            commands.append(stripped + "\n")
    return commands


def load_series(series_path: Path) -> dict:
    try:
        series = json.loads(series_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FixtureError(f"cannot read {series_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FixtureError(f"cannot parse {series_path}: {exc}") from exc
    if not isinstance(series, dict) or not isinstance(series.get("commits"), list):
        raise FixtureError(f"{series_path}: missing commits list")
    return series


def run_edits(repo: Path, fixture_dir: Path, commit: dict) -> None:
    filename = commit.get("edit_commands")
    if not isinstance(filename, str) or not filename:
        raise FixtureError(f"commit {commit.get('id')} has no edit_commands")
    for command in load_commands(fixture_dir / filename):
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", command],
            cwd=repo,
            env={**os.environ, "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise FixtureError(
                f"edit command for {commit.get('id')} failed with exit "
                f"code {result.returncode}"
            )


def write_base_tree(repo: Path) -> None:
    for relative, content in BASE_FILES.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def compare_with_reference(repo: Path, reference: Path) -> None:
    for relative in sorted(BASE_FILES):
        expected = (reference / relative).read_text(encoding="utf-8")
        actual = (repo / relative).read_text(encoding="utf-8")
        if expected != actual:
            raise FixtureError(
                f"generated {relative} does not match {reference / relative}; "
                "the fixture edit commands and the reference tree diverged"
            )


def dump_series(series: dict) -> str:
    """Serialise ``series.json`` keeping each commit's file list on one line."""
    lines = ["{"]
    lines.append(f'  "base_commit": {json.dumps(series["base_commit"])},')
    lines.append('  "commits": [')
    entries = []
    for commit in series["commits"]:
        members = [
            f'{json.dumps(key)}: {json.dumps(commit[key])}'
            for key in ("id", "message")
        ]
        members.append(
            f'"files": {json.dumps(commit["files"], separators=(", ", ": "))}'
        )
        members.extend(
            f'{json.dumps(key)}: {json.dumps(commit[key])}'
            for key in ("search_commands", "read_commands", "edit_commands")
        )
        fields = ["    {"]
        fields.extend(f"      {member}," for member in members[:-1])
        fields.append(f"      {members[-1]}")
        fields.append("    }")
        entries.append("\n".join(fields))
    lines.append(",\n".join(entries))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def generate(fixture_dir: Path, *, force: bool) -> dict:
    series_path = fixture_dir / SERIES.name
    reference = fixture_dir / REFERENCE.name
    source_repo = fixture_dir / SOURCE_REPO.name
    bundle = fixture_dir / BUNDLE.name
    series = load_series(series_path)
    if source_repo.exists():
        if not force:
            raise FixtureError(
                f"{source_repo} already exists; pass --force to regenerate it"
            )
        shutil.rmtree(source_repo)

    source_repo.mkdir(parents=True)
    git(source_repo, "init", "--quiet", "--initial-branch=main")
    git(source_repo, "config", "user.name", GIT_IDENTITY["GIT_AUTHOR_NAME"])
    git(source_repo, "config", "user.email", GIT_IDENTITY["GIT_AUTHOR_EMAIL"])
    git(source_repo, "config", "commit.gpgsign", "false")

    write_base_tree(source_repo)
    git(source_repo, "add", "--all")
    git(source_repo, "commit", "--no-gpg-sign", "--quiet", "-m", BASE_MESSAGE)
    base_commit = git(source_repo, "rev-parse", "HEAD")

    generated = []
    for commit in series["commits"]:
        message = commit.get("message")
        if not isinstance(message, str) or not message:
            raise FixtureError(f"commit {commit.get('id')} has no message")
        run_edits(source_repo, fixture_dir, commit)
        git(source_repo, "add", "--all")
        status = git(source_repo, "status", "--porcelain")
        if not status:
            raise FixtureError(
                f"edit commands for {commit.get('id')} produced no change; "
                "the base tree does not match the fixture expectations"
            )
        git(source_repo, "commit", "--no-gpg-sign", "--quiet", "-m", message)
        generated.append(git(source_repo, "rev-parse", "HEAD"))

    compare_with_reference(source_repo, reference)

    updated = dict(series)
    updated["base_commit"] = base_commit
    updated["commits"] = [
        {**commit, "id": new_id}
        for commit, new_id in zip(series["commits"], generated)
    ]
    series_path.write_text(dump_series(updated), encoding="utf-8")

    if bundle.exists():
        bundle.unlink()
    git(source_repo, "bundle", "create", str(bundle), "--all")

    return {
        "source_repo": str(source_repo),
        "bundle": str(bundle),
        "base_commit": base_commit,
        "commits": generated,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild data/mini-dev-workflow/source-repo from the checked-in "
            "edit commands and export it as a Git bundle."
        )
    )
    parser.add_argument(
        "--fixture-dir",
        default=str(FIXTURE_DIR),
        help="mini-dev-workflow fixture directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild the source repository even if it already exists",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = generate(Path(args.fixture_dir).absolute(), force=args.force)
    except FixtureError as exc:
        print(f"fixture generation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
