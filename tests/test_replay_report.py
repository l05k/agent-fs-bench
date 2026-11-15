import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_fs_bench.replay_report import summarize


class PairedReportTests(unittest.TestCase):
    def test_excludes_unpaired_failures_and_opens_each_task_with_its_own_reference(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            spec = {'benchmark': 'swe', 'agent': 'codex', 'model': 'openai/qwen3.8-max'}
            cases = {task: {**spec, 'task_id': task} for task in ('short', 'long', 'divergent')}
            data = {'ext4': {'short': [1, 3], 'long': [100, 100], 'divergent': [2, 2]},
                    'apsara': {'short': [1, 1], 'long': [150, 250], 'divergent': [1, None]}}
            for target, samples in data.items():
                run = root / target
                run.mkdir()
                (run / 'config.json').write_text(json.dumps({'benchmark': 'agent-replay',
                    'source_sha256': 'same', 'config': {'tasks': 1, 'repeats': 2},
                    'planned_targets': [target], 'targets': [{'name': target,
                        'info': {'hosts': {'local': {'cases': cases}}}}]}))
                outcomes, times = [], []
                for case, repeats in samples.items():
                    for repeat, seconds in enumerate(repeats, 1):
                        row = {'target': target, 'case': case, 'benchmark': 'swe',
                               'agent': 'codex', 'task_id': case, 'tasks': 1,
                               'sample': 0, 'repeat': repeat}
                        outcomes.append({**row, 'status': 'divergence' if seconds is None else 'ok'})
                        if seconds is not None:
                            times.append({**row, 'tool_s': seconds})
                for table, rows in (('outcomes', outcomes), ('replays', times)):
                    with (run / f'{table}.csv').open('w', newline='') as handle:
                        writer = csv.DictWriter(handle, fieldnames=rows[0])
                        writer.writeheader()
                        writer.writerows(rows)
            panel, = summarize([root / 'ext4', root / 'apsara'], 'ext4')['panels']
            self.assertEqual((panel['planned_tasks'], panel['valid_tasks']), (3, 2))
            self.assertEqual(panel['task_ratios']['apsara'], {'short': 0.5, 'long': 2.0})
            self.assertEqual(panel['median_ratio']['apsara'], 1.25)
            self.assertEqual(panel['outcomes']['apsara']['divergence'], 1)
