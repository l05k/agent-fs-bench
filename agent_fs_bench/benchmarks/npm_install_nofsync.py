"""npm/pnpm materialization without any harness-initiated fsync.

Same workload, fixtures and commands as ``npm-install``; the only difference is
``CONFIG['flush'] = False``: the timed project flush, the pre-timing input fsync
and the between-batch ``os.sync()`` are all skipped. Rows therefore carry only
``install_*`` metrics; ``flush_s``/``durable_s`` are not measured.
"""

from __future__ import annotations

import argparse
from typing import Any

from agent_fs_bench.benchmarks import npm_install
from agent_fs_bench.results import RunContext

CONFIG: dict[str, Any] = {**npm_install.CONFIG, "flush": False}

def parse_args(argv: list[str]) -> argparse.Namespace:
    return npm_install.parse_args(argv, CONFIG)

def run(ctx: RunContext, args: argparse.Namespace) -> None:
    npm_install.run(ctx, args)
