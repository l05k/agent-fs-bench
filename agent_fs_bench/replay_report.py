"""Summarize a complete, single-agent replay cohort without unpaired timings."""

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
from statistics import median

from .analysis import load_snapshots
from .results import read_table

STATUSES = ('ok', 'capture_missing', 'divergence', 'error', 'timeout')


def _rows(run: Path, table: str, target: str) -> dict:
    path = run / f'{table}.csv'
    rows = {}
    for row in read_table(path) if path.is_file() else ():
        if row['target'] != target:
            continue
        key = row['case'], int(row['repeat'])
        if key in rows or row['tasks'] != '1' or row['sample'] != '0':
            raise ValueError(f'{run}: invalid or duplicate {target}/{table} observation {key}')
        rows[key] = row
    return rows


def summarize(run_dirs: list[Path], reference: str) -> dict:
    snapshots = load_snapshots(run_dirs, ())
    first = snapshots[0]
    if first['benchmark'] != 'agent-replay' or first['config']['tasks'] != 1:
        raise ValueError('The paired report requires single-agent replay runs')
    counts = Counter()
    cohort = None
    outcomes = defaultdict(dict)
    repeats = first['config']['repeats']
    models = set()
    for run, config in zip(run_dirs, snapshots, strict=True):
        if config['planned_targets'] != [target['name'] for target in config['targets']]:
            raise ValueError(f'{run}: unfinished target; not a complete cohort run')
        for target in config['targets']:
            name = target['name']
            facts = next(iter(target['info']['hosts'].values()))
            if cohort is None:
                cohort = facts
            if facts != cohort:
                raise ValueError(f'{run}/{name}: cohort, task inputs, or replay tools differ')
            counts[name] += 1
            cases = facts['cases']
            planned = {(case, repeat) for case in cases for repeat in range(1, repeats + 1)}
            statuses = _rows(run, 'outcomes', name)
            times = _rows(run, 'replays', name)
            if (set(statuses) != planned or set(times) != {
                    key for key, row in statuses.items() if row['status'] == 'ok'}):
                raise ValueError(f'{run}/{name}: incomplete or inconsistent replay rows')
            for key, row in statuses.items():
                case, _ = key
                spec = cases[case]
                status = row['status']
                if status not in STATUSES or any(row[k] != spec[k] for k in ('benchmark', 'task_id', 'agent')):
                    raise ValueError(f'{run}/{name}: invalid replay outcome for {case}')
                if spec.get('model'):
                    models.add(spec['model'])
                seconds = None
                if status == 'ok':
                    measured = times[key]
                    if any(row[k] != measured[k] for k in ('benchmark', 'task_id', 'agent')):
                        raise ValueError(f'{run}/{name}: timing row does not match outcome for {case}')
                    seconds = float(measured['tool_s'])
                    if seconds < 0 or not math.isfinite(seconds):
                        raise ValueError(f'{run}/{name}: invalid tool time for {case}')
                outcomes[case].setdefault(name, []).append((status, seconds))
    if cohort is None or len(counts) < 2 or reference not in counts or len(set(counts.values())) != 1:
        raise ValueError('Provide the same number of completed cohort runs for every target, including the reference')
    if len(models) > 1:
        raise ValueError('A paired report compares one captured model')

    panels = {}
    for case, spec in cohort['cases'].items():
        key = spec['benchmark'], spec['agent']
        panel = panels.setdefault(key, {'benchmark': key[0], 'agent': key[1],
            'planned_tasks': 0, 'valid_tasks': 0, 'zero_reference_tasks': 0,
            'outcomes': {name: dict.fromkeys(STATUSES, 0) for name in counts},
            'task_ratios': {name: {} for name in counts}})
        panel['planned_tasks'] += 1
        samples = outcomes[case]
        for name, observations in samples.items():
            for status, _ in observations:
                panel['outcomes'][name][status] += 1
        if any(status != 'ok' for observations in samples.values() for status, _ in observations):
            continue
        medians = {name: median(seconds for _, seconds in observations)
                   for name, observations in samples.items()}
        baseline = medians[reference]
        if baseline == 0:
            panel['zero_reference_tasks'] += 1
            continue
        panel['valid_tasks'] += 1
        for name, seconds in medians.items():
            panel['task_ratios'][name][spec['task_id']] = seconds / baseline
    for panel in panels.values():
        panel['median_ratio'] = {name: median(values.values()) if values else None
                                 for name, values in panel['task_ratios'].items()}
    return {'reference': reference, 'model': next(iter(models), None), 'panels': list(panels.values())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--reference', default='ext4')
    args = parser.parse_args()
    print(json.dumps(summarize(args.runs, args.reference), indent=2))


if __name__ == '__main__':
    main()
