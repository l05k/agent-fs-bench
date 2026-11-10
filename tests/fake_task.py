"""Drive a workload's `task(t)` in-process without MPI.

`World` emulates mpi4py's ``COMM_WORLD`` for N tasks in threads: every collective is a
real rendezvous with a timeout, so a task that skips, reorders or splits a collective
fails instead of hanging. Single-task runs cannot detect any of that.
"""

import io
from pathlib import Path
import tempfile
import threading
import time

from agent_fs_bench import cluster
from agent_fs_bench.results import RunContext, Tables, read_table
from agent_fs_bench.targets import Target

TIMEOUT = 60.0


class _Exchange:
    """One communicator's collective state: every member must call the same collective."""

    def __init__(self, size, timeout):
        self._size = size
        self._timeout = timeout
        self._condition = threading.Condition()
        self._arrived = 0
        self._kind = None
        self._values = {}
        self._generation = 0
        # Results stay keyed by generation: a task that wakes late must read its own
        # collective's values even if the others already completed the next one.
        self._results = {}
        self._aborted = None

    def abort(self, error):
        with self._condition:
            if self._aborted is None:
                self._aborted = error
            self._condition.notify_all()

    def call(self, position, kind, value=None):
        with self._condition:
            generation = self._generation
            if self._aborted is not None:
                raise RuntimeError(f'collective {kind}: another task failed') from self._aborted
            if self._arrived == 0:
                self._kind = kind
            elif self._kind != kind:
                raise RuntimeError(
                    f'task at position {position} called {kind} while task '
                    f'{next(iter(self._values))} called {self._kind}: every task must call '
                    'the same collectives in the same order')
            self._values[position] = value
            self._arrived += 1
            if self._arrived == self._size:
                self._results[generation + 1] = [self._values[position]
                                                 for position in range(self._size)]
                self._values = {}
                self._arrived = 0
                self._kind = None
                self._generation += 1
                self._condition.notify_all()
                return self._results[generation + 1]
            deadline = time.monotonic() + self._timeout
            while self._generation == generation:
                if self._aborted is not None:
                    raise RuntimeError(
                        f'collective {kind}: another task failed') from self._aborted
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f'collective {kind} timed out after {self._timeout:g}s: '
                        f'{self._arrived} of {self._size} tasks arrived')
                self._condition.wait(remaining)
            return self._results[generation + 1]


class Comm:
    """One mpi4py-like communicator over the tasks of `members`, in that order."""

    def __init__(self, members, position, exchange):
        self._members = members
        self._position = position
        self._exchange = exchange

    def _my_position(self):
        return self._position

    def Get_size(self):
        return len(self._members)

    def Get_rank(self):
        return self._my_position()

    def Barrier(self):
        self._exchange.call(self._my_position(), 'Barrier')

    def gather(self, value, root=0):
        values = self._exchange.call(self._my_position(), 'gather', value)
        return values if self._my_position() == root else None

    def bcast(self, value, root=0):
        return self._exchange.call(self._my_position(), 'bcast', value)[root]


class World(Comm):
    """Fake ``MPI.COMM_WORLD``: one thread per task, real collectives, one timeout."""

    def __init__(self, size, timeout=TIMEOUT):
        self.timeout = timeout
        self._local = threading.local()
        self._exchanges = []
        self._splits = {}
        # Reentrant: Split creates its communicator's exchange while holding this lock.
        self._lock = threading.RLock()
        super().__init__(tuple(range(size)), -1, self._exchange_for(size))

    def _exchange_for(self, size):
        exchange = _Exchange(size, self.timeout)
        with self._lock:
            self._exchanges.append(exchange)
        return exchange

    def _my_position(self):
        position = getattr(self._local, 'rank', None)
        if position is None:
            raise RuntimeError('this thread is not bound to a task')
        return position

    def attach(self, rank):
        """Bind the calling thread to one task's rank."""
        self._local.rank = rank

    def Split(self, color, key):
        entries = self._exchange.call(self._my_position(), 'Split', (color, key))
        members = tuple(rank for _key, rank in sorted(
            (entry[1], rank) for rank, entry in enumerate(entries) if entry[0] == color))
        # Like mpi4py, every task of one color gets the same communicator.
        with self._lock:
            exchange = self._splits.get(color)
            if exchange is None:
                exchange = self._splits[color] = self._exchange_for(len(members))
        return Comm(members, members.index(self._my_position()), exchange)

    def abort(self, error):
        """Release every task blocked in a collective; the run has already failed."""
        with self._lock:
            exchanges = list(self._exchanges)
        for exchange in exchanges:
            exchange.abort(error)


def run_tasks(module, args, placement, *, timeout=TIMEOUT, node_args=None):
    """Run `module.task` for every task; return the emitted rows per table.

    `placement` is a list of ``(node, hostname)`` per task as in the real `JobSpec`. Rows
    pass the controller's own validation (`RunContext.emit`). The target sits under HOME,
    not /tmp, so its filesystem differs from tmpfs preparation dirs.

    `node_args(node)` may return that machine's args for paths production gives each
    machine separately (its own filesystem); by default every task shares `args`.
    """
    world = World(len(placement), timeout)
    benchmark = module.__name__.rsplit('.', 1)[-1].replace('_', '-')
    with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
        root = Path(directory)
        target = root / 'target'
        target.mkdir()
        protocols = [io.StringIO() for _task in placement]
        errors = []

        def run(index, node):
            # Task construction calls Split, a world collective, so it runs here too.
            world.attach(index)
            try:
                spec = cluster.JobSpec(module.__name__, benchmark, root.name,
                                       Target('t', target),
                                       args if node_args is None else node_args(node),
                                       tuple(placement))
                task = cluster.Task(world, spec, protocols[index])
                task.log_dir = root / 'logs' / node
                task.setup()
                module.task(task)
            except BaseException as error:
                errors.append(error)
                world.abort(error)

        threads = [threading.Thread(target=run, args=(index, node), name=f'task-{index}')
                   for index, (node, _hostname) in enumerate(placement)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0]
        (root / 'tables').mkdir()
        tables = Tables(root / 'tables')
        ctx = RunContext(benchmark, root.name, Target('t', target), target,
                         root / 'tables', tables)
        for protocol in protocols:
            for table, keys, metrics in cluster.parse_messages(protocol.getvalue()).rows:
                ctx.emit(table, keys, metrics)
        tables.close()
        return {table: read_table(root / 'tables' / f'{table}.csv') for table in tables.schemas}

