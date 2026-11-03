"""Single source of truth for the results-directory helpers.

The benchmark scripts stay standalone: the body of this file is copied into
each script between generated-block markers by ``tools/sync_results_core.py``.
Never import this module from the benchmark scripts.

Edit this file, then run ``uv run tools/sync_results_core.py``.
"""

import datetime as _results_datetime
from pathlib import Path as _ResultsPath


RESULTS_DIRNAME = "results"


def utc_run_id(moment=None):
    """Return a UTC run label such as ``20261102T042113Z``."""
    moment = moment or _results_datetime.datetime.now(
        _results_datetime.timezone.utc
    )
    return moment.astimezone(_results_datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )


def results_run_dir(script_name, anchor=None, moment=None):
    """Create and return ``<project>/results/<script_name>/<utc-run-id>``.

    ``anchor`` is the benchmark script path (default: this file), so the
    location never depends on the current working directory. A second run in
    the same second would otherwise overwrite the first one's artifacts, so a
    ``-2``, ``-3``, ... suffix is appended until the directory is unused.
    """
    anchor_path = _ResultsPath(anchor if anchor is not None else __file__)
    root = (
        anchor_path.resolve().parent / RESULTS_DIRNAME / script_name
    )
    run_id = utc_run_id(moment)
    path = root / run_id
    suffix = 1
    while path.exists():
        suffix += 1
        path = root / f"{run_id}-{suffix}"
    path.mkdir(parents=True, exist_ok=True)
    return path
