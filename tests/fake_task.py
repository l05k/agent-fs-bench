"""Drive a workload's `task(t)` in-process as the only task of a run, without MPI."""

import io
import json
from pathlib import Path
import tempfile

from agent_fs_bench import cluster
from agent_fs_bench.results import RunContext, Tables, read_table
from agent_fs_bench.targets import Target


class Comm:
    """A one-task communicator: every collective returns this task's own value."""

    def Split(self, color, key):
        return self

    def Get_size(self):
        return 1

    def Get_rank(self):
        return 0

    def Barrier(self):
        pass

    def gather(self, value, root=0):
        return [value]

    def bcast(self, value, root=0):
        return value


def run_task(module, args) -> dict[str, list[dict]]:
    """Run `module.task` for one task on a fresh target; return its rows per table.

    Rows pass the controller's own validation (`RunContext.emit`). The target sits
    under HOME, not /tmp, so its filesystem differs from tmpfs preparation dirs.
    """
    with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
        root = Path(directory)
        target = root / 'target'
        target.mkdir()
        spec = {'slots': [('local', 'host', (0,))], 'args': args,
                'benchmark': module.__name__.rsplit('.', 1)[-1].replace('_', '-'),
                'run_id': root.name, 'target': ('t', str(target))}
        protocol = io.StringIO()
        task = cluster.Task(Comm(), spec, 0, protocol)
        task.log_dir = root / 'logs'
        task.setup()
        module.task(task)
        (root / 'tables').mkdir()
        tables = Tables(root / 'tables')
        ctx = RunContext(spec['benchmark'], root.name, Target('t', target), target,
                         root / 'tables', tables)
        for line in protocol.getvalue().splitlines():
            kind, _, payload = line.partition(' ')
            if kind == 'AFB_ROW':
                ctx.emit(*json.loads(payload))
        tables.close()
        return {table: read_table(root / 'tables' / f'{table}.csv') for table in tables.schemas}
