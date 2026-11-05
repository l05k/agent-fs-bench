"""Read the compiler version selected by the actual Makefile."""

import os
from pathlib import Path
import subprocess
import tempfile

MARKER = "AGENT_FS_BENCH_COMPILER_VERSION"


def read_compiler_version(directory: Path) -> str:
    """Return the first version line from the makefile-selected ``$(CC)``.

    A temporary ``MAKEFILES`` fragment adds one diagnostic target that runs
    ``$(CC) --version``. The *install suffix prevents Kbuild from resyncing
    configuration. It changes no build variable or measured make command;
    inherited make variables and recursive make retain their normal precedence.
    """
    target = "agent-fs-bench-compiler-version-install"
    fragment = (f".PHONY: {target}\n{target}:\n"
                f"\t@printf '%s\\n' '{MARKER}'\n\t@$(CC) --version\n")
    with tempfile.TemporaryDirectory(prefix="agent-fs-bench-make-") as temporary:
        probe = Path(temporary) / "compiler.mk"
        probe.write_text(fragment, encoding="utf-8")
        environment = os.environ.copy()
        environment["MAKEFILES"] = " ".join(filter(None, (
            environment.get("MAKEFILES"), str(probe),
        )))
        result = subprocess.run(
            ["make", "--silent", "--no-print-directory", target],
            cwd=directory,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(
            f"cannot read compiler version: {result.stderr.strip()}"
        )
    lines = result.stdout.splitlines()
    try:
        index = lines.index(MARKER)
    except ValueError:
        raise RuntimeError("make did not run the compiler-version target") from None
    version = [line.strip() for line in lines[index + 1:] if line.strip()]
    if not version:
        raise RuntimeError("the makefile-selected compiler printed no version")
    return version[0]
