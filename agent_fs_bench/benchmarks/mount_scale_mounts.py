"""Mount lifecycle for each worker in the mount-scaling benchmark.

Profile commands are literal argv templates run concurrently per client, then
checked through mountinfo and per-agent sentinels; no shell, sudo or cleanup.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import re
import secrets
import subprocess
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MULTI_MOUNT = "multi-mount"
SHARED_DIRECTORY = "shared-directory"
_TOPOLOGIES = {MULTI_MOUNT, SHARED_DIRECTORY}
_MOUNTINFO = "/proc/self/mountinfo"
_UNESCAPE = re.compile(r"\\([0-7]{3})")
CGROUP_ROOT = Path('/sys/fs/cgroup/agent-fs-bench')
CGROUP_CPU_MAX = '100000 100000'
CGROUP_MEMORY_MAX = 2 * 1024**3


@dataclass(frozen=True)
class Profile:
    topology: str
    admin_root: Path
    private_mount: tuple[str, ...] = ()
    unmount: tuple[str, ...] = ()
    cgroup: bool = False


@dataclass(frozen=True)
class Client:
    workspace: Path
    cgroup: Path | None = None


def parse_profile(text: str, target_name: str, source: str = "mounts.toml") -> Profile:
    """Read ``[profiles.<target_name>]`` from private TOML text.

    No filesystem check: the controller may not mount ``admin_root``; each worker checks
    it on its own machine.
    """
    table = tomllib.loads(text)["profiles"][target_name]
    unknown = table.keys() - {'topology', 'admin_root', 'private_mount',
                              'unmount', 'cgroup'}
    if unknown:
        raise ValueError(f'{source}: unknown profile keys {sorted(unknown)}')
    cgroup = table.get('cgroup', False)
    if type(cgroup) is not bool:
        raise ValueError(f'{source}: cgroup must be a TOML boolean')
    topology = table["topology"]
    if topology not in _TOPOLOGIES:
        raise ValueError(f"{source}: topology must be {MULTI_MOUNT} or {SHARED_DIRECTORY}")
    admin_root = Path(table["admin_root"])
    if not admin_root.is_absolute():
        raise ValueError(f"{source}: admin_root must be an absolute path")

    return Profile(
        topology=topology,
        admin_root=admin_root,
        private_mount=tuple(table['private_mount']),
        unmount=tuple(table['unmount']),
        cgroup=cgroup,
    )


def _volume_path(path: Path, admin_root: Path) -> str:
    relative = path.relative_to(admin_root)
    return "/" if relative == Path(".") else "/" + relative.as_posix()


def _mount_points() -> dict[int, Path]:
    with open(_MOUNTINFO, encoding="utf-8") as handle:
        lines = handle.readlines()
    points = {}
    for line in lines:
        before, separator, _ = line.partition(" - ")
        fields = before.split()
        if separator and len(fields) >= 5:
            path = _UNESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4])
            points[int(fields[0])] = Path(path).resolve()
    return points


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _run(argv: list[str], log_path: Path, timeout_s: float) -> None:
    try:
        with log_path.with_suffix('.stdout.txt').open('w') as out, \
                log_path.with_suffix('.stderr.txt').open('w') as err:
            completed = subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                text=True, timeout=timeout_s,
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f'command exceeded {timeout_s:g}s; inspect {log_path.parent}'
        ) from None
    if completed.returncode:
        raise RuntimeError(
            f'command exited with status {completed.returncode}; '
            f'inspect {log_path.parent}'
        )


class Layout:
    """Prepared admin/local directories and the mount/unmount lifecycle."""

    def __init__(
        self,
        profile: Profile,
        work_dir: Path,
        data_root: Path,
        clients: int,
        *,
        log_dir: Path,
        timeout_s: float,
    ) -> None:
        if profile.topology not in _TOPOLOGIES:
            raise ValueError(f"unsupported topology: {profile.topology!r}")
        if not work_dir.is_dir() or not data_root.is_dir():
            raise ValueError("work_dir and data_root must exist")
        admin = profile.admin_root.resolve()
        work = work_dir.resolve()
        if _inside(work, admin) or _inside(admin, work):
            raise ValueError("work_dir must be outside admin_root")
        if data_root.resolve() == admin or not _inside(data_root.resolve(), admin):
            raise ValueError("data_root must be a directory under admin_root")

        multi = profile.topology == MULTI_MOUNT
        self.profile, self.work_dir, self.data_root = profile, work_dir, data_root
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self.mount_agent_elapsed_s: tuple[float, ...] = ()
        self.unmount_agent_elapsed_s: tuple[float, ...] = ()
        self._before: dict[int, Path] | None = None
        self._sentinel = secrets.token_hex(16)
        self._mounted = False

        client_values: list[Client] = []
        self.mountpoints: list[Path] = []
        for index in range(clients):
            admin_agent = data_root / f"agent-{index:04d}"
            local_root = work_dir / f"agent-{index:04d}"
            if admin_agent.exists() or local_root.exists():
                raise ValueError(f"layout directories already exist for agent {index}")
            admin_agent.mkdir()
            (admin_agent / "workspace").mkdir()
            local_root.mkdir()
            if multi:
                (local_root / "workspace").mkdir()
            mountpoint = local_root / 'workspace' if multi else local_root
            cgroup = None
            if profile.cgroup:
                digest = hashlib.sha256(str(mountpoint.resolve()).encode()).hexdigest()
                cgroup = CGROUP_ROOT / digest[:20]
            client_values.append(Client(local_root / 'workspace', cgroup))
            self.mountpoints.append(mountpoint)
        self.clients = tuple(client_values)
        self._write_sentinels()

    def mount(self) -> dict[str, int | float]:
        """Run every client's mount command, then verify outside the timer.

        A successful unmount clears the prior mountinfo baseline, so the same Layout
        can remount after the read fixture has been prepared through its first mount.
        """
        self.begin_mount()
        commands = [self.mount_command(index) for index in range(len(self.clients))]
        wall, self.mount_agent_elapsed_s = self._time(commands, "mount")
        counts = self.accept_mount()
        return {"mount_elapsed_s": wall, **counts}

    def begin_mount(self) -> None:
        """Snapshot mountinfo before commands run elsewhere (e.g. inside MPI ranks)."""
        if self._mounted or self._before is not None:
            raise RuntimeError("begin_mount() requires no active or unverified mount attempt")
        self._before = _mount_points()
        planned = {point.resolve() for point in self.mountpoints}
        roots = self._roots()
        if planned & set(self._before.values()):
            raise RuntimeError("a planned mountpoint is already mounted")
        if any(any(_inside(point, root) for root in roots)
               for point in self._before.values()):
            raise RuntimeError("a client subtree already contains a mountpoint")

    def accept_mount(self) -> dict[str, int]:
        """Verify mounts established outside this Layout and mark it mounted."""
        if self._before is None:
            raise RuntimeError("accept_mount() requires begin_mount()")
        try:
            counts = self.verify()
        except RuntimeError as exc:
            raise RuntimeError(
                f"mount verification failed: {exc}; mounts may remain; "
                f"inspect {self.work_dir} and {self.data_root}"
            ) from exc
        self._mounted = True
        return counts

    def mount_command(self, index: int) -> list[str]:
        """The single expanded private mount command of one client."""
        return self._command(index, self.profile.private_mount)

    def _roots(self) -> list[Path]:
        return [(self.work_dir / f"agent-{i:04d}").resolve()
                for i in range(len(self.clients))]

    def verify(self) -> dict[str, int]:
        """Check the actual mount count and the per-agent data aliases."""
        if self._before is None:
            raise RuntimeError("verify() requires a mount attempt")
        roots = self._roots()
        planned = {point.resolve() for point in self.mountpoints}
        current = _mount_points()
        scope = [
            point
            for mount_id, point in current.items()
            if mount_id not in self._before
            and any(_inside(point, root) for root in roots)
        ]
        if len(scope) != len(planned) or set(scope) != planned:
            extra = sorted(str(path) for path in set(scope) - planned)
            missing = sorted(str(path) for path in planned - set(scope))
            raise RuntimeError(
                f"mountpoints: {len(scope)} found, {len(planned)} expected"
                + (f"; extra {extra}" if extra else "")
                + (f"; missing {missing}" if missing else "")
            )
        self._check_aliases()
        for client in self.clients:
            if client.cgroup is not None:
                self._check_cgroup(client.cgroup)
        return {"measured_mounts": len(scope)}

    @staticmethod
    def _check_cgroup(path: Path) -> None:
        cpu = (path / 'cpu.max').read_text().strip()
        memory = (path / 'memory.max').read_text().strip()
        if (cpu, memory) != (CGROUP_CPU_MAX, str(CGROUP_MEMORY_MAX)):
            raise RuntimeError(f'{path}: expected 1 CPU / 2 GiB, measured cpu.max={cpu!r}, '
                               f'memory.max={memory!r}')
        if not (path / 'cgroup.procs').read_text().strip():
            raise RuntimeError(f'{path}: no mount client process in cgroup')

    def unmount(self) -> dict[str, int | float]:
        """Run every client's unmount command, then verify."""
        if not self._mounted:
            raise RuntimeError("unmount() requires a successful mount")
        commands = [self._command(index, self.profile.unmount)
                    for index in range(len(self.clients))]
        wall, self.unmount_agent_elapsed_s = self._time(commands, "unmount")
        roots = self._roots()
        remaining = [point for point in _mount_points().values()
                     if any(_inside(point, root) for root in roots)]
        if remaining:
            raise RuntimeError(
                f"unmount verification failed: {len(remaining)} mountpoint(s) "
                f"remain; inspect {self.work_dir} and {self.data_root}"
            )
        for client in self.clients:
            if client.cgroup is not None and client.cgroup.exists():
                raise RuntimeError(f'per-client cgroup remains after unmount: {client.cgroup}')
        self._mounted = False
        self._before = None
        return {"unmount_elapsed_s": wall, "remaining_mounts": 0}

    def _context(self, index: int) -> dict[str, object]:
        admin = self.profile.admin_root
        multi = self.profile.topology == MULTI_MOUNT
        private = self.data_root / f"agent-{index:04d}"
        local = self.work_dir / f"agent-{index:04d}"
        source = private / "workspace" if multi else private
        return {
            "source": _volume_path(source, admin),
            "mountpoint": str(local / "workspace" if multi else local),
            "subdir": _volume_path(source, admin),
            "agent": index,
        }

    def _command(self, index: int, template: Sequence[str]) -> list[str]:
        context = self._context(index)
        return [token.format_map(context) for token in template]

    def _time(
        self, commands: Sequence[list[str]], action: str
    ) -> tuple[float, tuple[float, ...]]:
        def run_client(index: int, argv: list[str]) -> float:
            started = time.perf_counter()
            log_path = self.log_dir / f'{action}-agent-{index:04d}'
            _run(argv, log_path, self.timeout_s)
            return time.perf_counter() - started

        started = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(commands)) as pool:
                futures = [pool.submit(run_client, index, argv)
                           for index, argv in enumerate(commands)]
                elapsed = tuple(future.result() for future in futures)
        except RuntimeError as exc:
            raise RuntimeError(
                f"{action} failed: {exc}; mounts may remain; inspect "
                f"{self.work_dir} and {self.data_root}"
            ) from exc
        return time.perf_counter() - started, elapsed

    def _write_sentinels(self) -> None:
        name = f".mount-scale-{self._sentinel}"
        for index in range(len(self.clients)):
            path = self.data_root / f"agent-{index:04d}" / "workspace" / name
            path.write_text(f"private-{index}", encoding="utf-8")

    def _check_aliases(self) -> None:
        name = f".mount-scale-{self._sentinel}"
        for index, client in enumerate(self.clients):
            try:
                actual = (client.workspace / name).read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"agent {index:04d} workspace alias check failed: {exc}"
                ) from exc
            if actual != f"private-{index}":
                raise RuntimeError(f"agent {index:04d} workspace aliases the wrong data")
