"""Filesystem benchmarks with shared targets and portable measurements."""

# Experiment preconditions are plain `assert` statements; `python -O` would skip them all.
if not __debug__:
    raise SystemExit('agent_fs_bench must not run under python -O: its validity checks are asserts')
