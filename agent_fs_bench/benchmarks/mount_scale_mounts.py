"""Mount lifecycle for the single-host mount-scaling benchmark.

Profile commands are literal argv templates run concurrently per client, then
checked through mountinfo and per-agent sentinels; no shell, sudo or cleanup.
"""

from __future__ import annotations

import concurrent.futures
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


@dataclass(frozen=True)
class Profile:
    topology: str
    admin_root: Path
    private_mount: tuple[str, ...] = ()
    shared_mount: tuple[str, ...] = ()
    unmount: tuple[str, ...] = ()


@dataclass(frozen=True)
class Client:
    workspace: Path
    shared: tuple[Path, ...]


def parse_profile(text: str, target_name: str, source: str = "mounts.toml") -> Profile:
    """Read ``[profiles.<target_name>]`` from private TOML text.

    No filesystem check: the controller may not mount ``admin_root``; each worker checks
    it on its own machine.
    """
    table = tomllib.loads(text)["profiles"][target_name]
    topology = table["topology"]
    if topology not in _TOPOLOGIES:
        raise ValueError(f"{source}: topology must be {MULTI_MOUNT} or {SHARED_DIRECTORY}")
    admin_root = Path(table["admin_root"])
    if not admin_root.is_absolute():
        raise ValueError(f"{source}: admin_root must be an absolute path")

    def command(key: str, required: bool) -> tuple[str, ...]:
        if key not in table:
            if required:
                raise ValueError(f"{source}: profile is missing {key}")
            return ()
        return tuple(table[key])

    return Profile(
        topology=topology,
        admin_root=admin_root,
        private_mount=command("private_mount", True),
        shared_mount=command("shared_mount", topology == MULTI_MOUNT),
        unmount=command("unmount", True),
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
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f'command exceeded {timeout_s:g}s; inspect {log_path.parent}'
        ) from exc
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
        shared_paths: Sequence[Path],
        clients: int,
        *,
        log_dir: Path,
        timeout_s: float,
    ) -> None:
        if profile.topology not in _TOPOLOGIES:
            raise ValueError(f"unsupported topology: {profile.topology!r}")
        shared = tuple(shared_paths)
        if (
            not work_dir.is_dir() or not data_root.is_dir()
            or not all(path.is_dir() for path in shared)
        ):
            raise ValueError("work_dir, data_root and shared_paths must exist")
        admin = profile.admin_root.resolve()
        work = work_dir.resolve()
        if _inside(work, admin) or _inside(admin, work):
            raise ValueError("work_dir must be outside admin_root")
        for label, path in [("data_root", data_root)] + [
            (f"shared_paths[{index}]", path) for index, path in enumerate(shared)
        ]:
            if path.resolve() == admin or not _inside(path.resolve(), admin):
                raise ValueError(f"{label} must be a directory under admin_root")

        multi = profile.topology == MULTI_MOUNT
        self.profile, self.work_dir, self.data_root = profile, work_dir, data_root
        self.shared_paths = shared
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
            shared_dirs = [local_root / f"shared-{number}"
                           for number in range(1, len(shared) + 1)]
            if multi:
                (local_root / "workspace").mkdir()
                for path in shared_dirs:
                    path.mkdir()
            client_values.append(Client(local_root / "workspace", tuple(shared_dirs)))
            self.mountpoints += ([local_root / "workspace", *shared_dirs]
                                 if multi else [local_root])
        self.clients = tuple(client_values)
        self._write_sentinels()

    def mount(self) -> dict[str, int | float]:
        """Run every client's mount commands, then verify outside the timer."""
        if self._before is not None:
            raise RuntimeError("mount() has already been attempted")
        if self.profile.topology == SHARED_DIRECTORY:
            targets = (
                self.data_root / f"agent-{index:04d}" / f"shared-{number}"
                for index in range(len(self.clients))
                for number in range(1, len(self.shared_paths) + 1)
            )
            absent = next((p for p in targets if p.exists() or p.is_symlink()), None)
            if absent is not None:
                raise RuntimeError(f"shared-directory target must stay absent: {absent}")
        self._before = _mount_points()
        planned = {point.resolve() for point in self.mountpoints}
        roots = self._roots()
        if planned & set(self._before.values()):
            raise RuntimeError("a planned mountpoint is already mounted")
        if any(any(_inside(point, root) for root in roots)
               for point in self._before.values()):
            raise RuntimeError("a client subtree already contains a mountpoint")

        plans = [self._plan(index) for index in range(len(self.clients))]
        wall, self.mount_agent_elapsed_s = self._time(plans, "mount")
        try:
            counts = self.verify()
        except RuntimeError as exc:
            raise RuntimeError(
                f"mount verification failed: {exc}; mounts may remain; "
                f"inspect {self.work_dir} and {self.data_root}"
            ) from exc
        self._mounted = True
        return {"mount_elapsed_s": wall, **counts}

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
        return {"measured_mounts": len(scope)}

    def unmount(self) -> dict[str, int | float]:
        """Run every client's unmount commands in reverse, then verify."""
        if not self._mounted:
            raise RuntimeError("unmount() requires a successful mount")
        plans = [self._plan(i, unmount=True) for i in range(len(self.clients))]
        wall, self.unmount_agent_elapsed_s = self._time(plans, "unmount")
        roots = self._roots()
        remaining = [point for point in _mount_points().values()
                     if any(_inside(point, root) for root in roots)]
        if remaining:
            raise RuntimeError(
                f"unmount verification failed: {len(remaining)} mountpoint(s) "
                f"remain; inspect {self.work_dir} and {self.data_root}"
            )
        self._mounted = False
        return {"unmount_elapsed_s": wall, "remaining_mounts": 0}

    def _commands(self, index: int) -> list[tuple[tuple[str, ...], dict[str, object]]]:
        """Per-client commands; private first, then one per shared resource."""
        admin = self.profile.admin_root
        multi = self.profile.topology == MULTI_MOUNT
        private = self.data_root / f"agent-{index:04d}"
        local = self.work_dir / f"agent-{index:04d}"
        source = private / "workspace" if multi else private
        context: dict[str, object] = {
            "source": _volume_path(source, admin),
            "mountpoint": str(local / "workspace" if multi else local),
            "subdir": _volume_path(source, admin),
            "binds": ",".join(
                f"{_volume_path(shared, admin)}:"
                f"{_volume_path(private / f'shared-{number}', admin)}"
                for number, shared in enumerate(self.shared_paths, start=1)
            ),
            "agent": index,
        }
        if not multi:
            return [(self.profile.private_mount, context)]
        return [(self.profile.private_mount, context)] + [
            (self.profile.shared_mount, {
                **context,
                "source": _volume_path(shared, admin),
                "mountpoint": str(local / f"shared-{number}"),
            })
            for number, shared in enumerate(self.shared_paths, start=1)
        ]

    def _plan(self, index: int, *, unmount: bool = False) -> list[list[str]]:
        commands = self._commands(index)
        if unmount:
            return [
                self._expand(self.profile.unmount, context)
                for _, context in reversed(commands)
            ]
        return [self._expand(template, context) for template, context in commands]

    @staticmethod
    def _expand(template: Sequence[str], context: dict[str, object]) -> list[str]:
        return [token.format_map(context) for token in template]

    def _time(
        self, plans: Sequence[Sequence[list[str]]], action: str
    ) -> tuple[float, tuple[float, ...]]:
        def run_client(index: int, commands: Sequence[list[str]]) -> float:
            started = time.perf_counter()
            for step, argv in enumerate(commands):
                log_path = self.log_dir / f'{action}-agent-{index:04d}-{step:02d}'
                _run(argv, log_path, self.timeout_s)
            return time.perf_counter() - started

        started = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(plans)) as pool:
                futures = [pool.submit(run_client, index, plan)
                           for index, plan in enumerate(plans)]
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
        for index, path in enumerate(self.shared_paths):
            (path / name).write_text(f"shared-{index}", encoding="utf-8")

    def _check_aliases(self) -> None:
        name = f".mount-scale-{self._sentinel}"
        for index, client in enumerate(self.clients):
            expected = [("workspace", client.workspace, f"private-{index}")]
            expected += [(f"shared-{n}", path, f"shared-{n - 1}")
                         for n, path in enumerate(client.shared, start=1)]
            for label, path, content in expected:
                try:
                    actual = (path / name).read_text(encoding="utf-8")
                except OSError as exc:
                    raise RuntimeError(
                        f"agent {index:04d} {label} alias check failed: {exc}"
                    ) from exc
                if actual != content:
                    raise RuntimeError(
                        f"agent {index:04d} {label} aliases the wrong data"
                    )
