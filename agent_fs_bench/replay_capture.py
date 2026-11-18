"""Collect disjoint replay cassettes over SSH, then distribute the accepted cohort."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

from .benchmarks import agent_replay as replay
from .results import PROJECT_ROOT, source_digest, write_json
from .ssh import Host, load_hosts, ssh_argv

CASSETTE_FILES = ('manifest.json', 'flows.mitm', 'tools.json')


def identity(case: replay.ReplayCase) -> dict:
    return {'name': case.name, 'benchmark': case.benchmark,
            'task_id': case.task_id, 'agent': case.agent,
            'mounts': [mount.container for mount in case.mounts]}


def input_facts(case: replay.ReplayCase) -> dict:
    return {'task_sha256': replay.fingerprint(case.task),
            'initial_sha256': [replay.fingerprint(mount.source) for mount in case.mounts]}


def cassette_digest(path: Path) -> str:
    return replay.fingerprint(path, [path / name for name in CASSETTE_FILES])


def shards(cases: list[replay.ReplayCase], hosts: tuple[Host, ...]) -> list[tuple[Host, list]]:
    if not hosts:
        raise ValueError('capture requires at least one remote host')
    return [(host, cases[index::len(hosts)]) for index, host in enumerate(hosts)
            if index < len(cases)]


def remote_command(host: Host, argv: list[str]) -> list[str]:
    root = Path(host.root) if host.root else PROJECT_ROOT
    python = host.python or str(root / '.venv' / 'bin' / 'python')
    command = [python, '-u', '-m', 'agent_fs_bench.replay_capture', *argv]
    return [*ssh_argv(host), f'cd {shlex.quote(str(root))} && exec {shlex.join(command)}']


def ext4_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=False)
    fstype = subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '-T', str(path)],
                                     text=True).strip()
    if fstype != 'ext4':
        raise ValueError(f'capture workspace must use ext4, got {fstype}: {path}')


def capture_case(case: replay.ReplayCase, root: Path, model: str, upstream: str,
                 key: str) -> dict:
    facts = input_facts(case)
    directory = root / 'work' / case.name
    directory.mkdir(parents=True)
    command = [str(replay.REPLAY_ROOT / '.venv' / 'bin' / 'agent-mitm-replay'), 'record',
               '--agent', case.agent, '--model', model, '--upstream', upstream,
               '--task', str(case.task), '--cassette', str(root / 'cassettes' / case.name),
               '--output', str(root / 'outputs' / case.name)]
    for index, mount in enumerate(case.mounts):
        destination = directory / f'mount-{index}'
        replay.restore_verified(mount.source, destination, facts['initial_sha256'][index])
        command += ['--mount', f'{destination}:{mount.container}']
    subprocess.run(['sync', '-f', '--', str(directory)], check=True)
    with (root / f'{case.name}.log').open('x') as log:
        process = subprocess.run(command, env={**os.environ, 'OPENAI_API_KEY': key},
                                 stdout=log, stderr=subprocess.STDOUT)
    output = root / 'outputs' / case.name
    summary = json.loads((output / 'summary.json').read_text())
    status = summary['status']
    if (summary['mode'] != 'record' or status not in ('ok', 'error', 'timeout', 'divergence')
            or (process.returncode == 0) != (status == 'ok')):
        raise ValueError(f'capture exit status contradicts its summary: {output}')
    row = {**identity(case), 'status': status, 'inputs': facts}
    if status == 'ok':
        cassette = root / 'cassettes' / case.name
        manifest = json.loads((cassette / 'manifest.json').read_text())
        if (manifest['schema_version'] != 3 or manifest['agent'] != case.agent
                or manifest['model'] != model or manifest['mounts'] != row['mounts']):
            raise ValueError(f'capture identity differs from plan: {case.name}')
        row['cassette_sha256'] = cassette_digest(cassette)
        shutil.rmtree(directory)
    return row


def worker() -> None:
    request = json.loads(sys.stdin.readline())
    if sys.platform != 'linux' or source_digest() != request['source_sha256']:
        raise ValueError('capture hosts require Linux and the controller source revision')
    cases = replay.load_cases(replay.cases_path(request['cases_file']))
    selected = [replay.select_case(cases, item['name']) for item in request['cases']]
    if [identity(case) for case in selected] != request['cases']:
        raise ValueError('capture host has a different cohort')
    root = PROJECT_ROOT / 'local' / 'replay-capture' / request['batch']
    ext4_directory(root)
    executable = replay.REPLAY_ROOT / '.venv' / 'bin' / 'agent-mitm-replay'
    if not executable.is_file():
        raise ValueError('initialize the replay submodule and its locked environment')
    for case in selected:
        # A classified task failure does not replace or retry that task. Setup errors
        # propagate and stop this host; previously emitted outcomes remain available.
        try:
            row = capture_case(case, root, request['model'], request['upstream'], request['key'])
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
            print(json.dumps({**identity(case), 'status': 'setup_failure',
                              'hostname': socket.gethostname(), 'remote_cassette': None}), flush=True)
            raise
        row['hostname'] = socket.gethostname()
        row['remote_cassette'] = str(root / 'cassettes' / case.name)
        print(json.dumps(row), flush=True)


def fetch_cassette(host: Host, source: str, destination: Path, stderr) -> None:
    destination.mkdir(mode=0o700)
    command = ['tar', '-cf', '-', '-C', source, *CASSETTE_FILES]
    with subprocess.Popen([*ssh_argv(host), shlex.join(command)],
                          stdout=subprocess.PIPE, stderr=stderr) as remote:
        try:
            subprocess.run(['tar', '-xf', '-', '-C', str(destination)],
                           stdin=remote.stdout, stderr=stderr, check=True)
        finally:
            remote.stdout.close()
        if remote.wait():
            raise RuntimeError(f'{host.name}: cassette transfer failed')


def collect_host(host: Host, cases: list[replay.ReplayCase], request: dict,
                 output: Path, record) -> None:
    expected = iter(cases)
    with (output / 'hosts' / f'{host.name}.stderr.log').open('x') as stderr:
        with subprocess.Popen(remote_command(host, ['--worker']), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=stderr, text=True) as process:
            process.stdin.write(json.dumps({**request, 'cases': [identity(case) for case in cases]}))
            process.stdin.close()
            try:
                for line in process.stdout:
                    row = json.loads(line)
                    case = next(expected)
                    if any(row[key] != value for key, value in identity(case).items()):
                        raise ValueError(f'{host.name}: unexpected capture outcome')
                    if row['status'] == 'ok':
                        destination = output / 'cassettes' / case.name
                        fetch_cassette(host, row.pop('remote_cassette'), destination, stderr)
                        if cassette_digest(destination) != row['cassette_sha256']:
                            raise ValueError(f'{host.name}: cassette changed during transfer')
                    else:
                        row.pop('remote_cassette')
                    record(host.name, row)
                if process.wait() or next(expected, None) is not None:
                    raise RuntimeError(f'{host.name}: capture stopped; inspect its stderr log')
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()


def collect(args: argparse.Namespace) -> int:
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise ValueError('set OPENAI_API_KEY on the capture controller')
    cases = replay.load_cases(replay.cases_path(args.cases_file))
    hosts = load_hosts(args.hosts_file)
    assignments = shards(cases, hosts)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    (output / 'hosts').mkdir()
    (output / 'cassettes').mkdir(mode=0o700)
    batch = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    digest = source_digest()
    index = {'source_sha256': digest, 'model': args.model, 'batch': batch,
             'cases': [{**identity(case), 'host': host.name, 'status': 'not_run'}
                       for host, group in assignments for case in group], 'host_errors': []}
    rows = {row['name']: row for row in index['cases']}
    write_json(output / 'index.json', index)
    lock = threading.Lock()

    def record(host: str, row: dict) -> None:
        with lock:
            rows[row['name']].update(row, host=host)
            write_json(output / 'index.json', index)
            print(f"{host}: {row['name']} {row['status']}", flush=True)

    request = {'cases_file': args.cases_file, 'batch': batch, 'model': args.model,
               'upstream': args.upstream, 'source_sha256': digest, 'key': key}
    with ThreadPoolExecutor(max_workers=len(assignments)) as pool:
        futures = {pool.submit(collect_host, host, group, request, output, record): host
                   for host, group in assignments}
        for future in as_completed(futures):
            host = futures[future]
            try:
                future.result()
            except Exception as error:
                # Retain completed hosts and the original not_run denominator. No
                # replacement capture, retry or reassignment after a host failure.
                with lock:
                    index['host_errors'].append(host.name)
                    write_json(output / 'index.json', index)
                print(f'{host.name}: {error}', file=sys.stderr, flush=True)
    print(f'Capture index: {output / "index.json"}', flush=True)
    return int(bool(index['host_errors']) or any(row['status'] != 'ok' for row in rows.values()))


def install(cases_file: str) -> None:
    if sys.platform != 'linux':
        raise ValueError('replay hosts require Linux')
    cases = {case.name: case for case in replay.load_cases(replay.cases_path(cases_file))}
    local = PROJECT_ROOT / 'local'
    local.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='replay-import-', dir=local) as temporary:
        root = Path(temporary)
        subprocess.run(['tar', '-xf', '-', '-C', str(root)], stdin=sys.stdin.buffer, check=True)
        index = json.loads((root / 'index.json').read_text())
        if source_digest() != index['source_sha256']:
            raise ValueError('replay host has a different source revision')
        expected = {case.name: identity(case) for case in cases.values()}
        observed = {row['name']: {key: row[key] for key in identity(cases[row['name']])}
                    for row in index['cases']}
        if observed != expected or len(index['cases']) != len(cases):
            raise ValueError('replay host has a different cohort')
        for case in cases.values():
            if case.cassette.exists():
                raise ValueError(f'refusing to overwrite cassette: {case.cassette}')
        accepted = [row for row in index['cases'] if row['status'] == 'ok']
        for row in accepted:
            case = cases[row['name']]
            if input_facts(case) != row['inputs']:
                raise ValueError(f'replay inputs differ from capture: {case.name}')
            if cassette_digest(root / 'cassettes' / case.name) != row['cassette_sha256']:
                raise ValueError(f'cassette differs from capture: {case.name}')
        for row in accepted:
            case = cases[row['name']]
            case.cassette.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(root / 'cassettes' / case.name, case.cassette)
        print(json.dumps({'installed': len(accepted), 'planned': len(index['cases'])}), flush=True)


def distribute_host(host: Host, source: Path, cases_file: str, names: list[str]) -> None:
    command = ['tar', '-cf', '-', '-C', str(source), 'index.json',
               *(f'cassettes/{name}' for name in names)]
    with subprocess.Popen(command, stdout=subprocess.PIPE) as archive:
        try:
            subprocess.run(remote_command(host, ['--install', '--cases-file', cases_file]),
                           stdin=archive.stdout, check=True)
        finally:
            archive.stdout.close()
        if archive.wait():
            raise RuntimeError('cannot archive captured cassettes')
    print(f'{host.name}: installed {len(names)} cassettes', flush=True)


def distribute(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    index = json.loads((source / 'index.json').read_text())
    names = [row['name'] for row in index['cases'] if row['status'] == 'ok']
    if not names:
        raise ValueError('no successful cassettes to distribute')
    hosts = load_hosts(args.hosts_file)
    if not hosts:
        raise ValueError('distribution requires at least one remote host')
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = [pool.submit(distribute_host, host, source, args.cases_file, names) for host in hosts]
        for future in as_completed(futures):
            future.result()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ['--worker']:
        worker()
        return 0
    if argv and argv[0] == '--install':
        parser = argparse.ArgumentParser()
        parser.add_argument('--install', action='store_true')
        parser.add_argument('--cases-file', required=True)
        install(parser.parse_args(argv).cases_file)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('collect', 'distribute'):
        command = commands.add_parser(action)
        command.add_argument('--hosts-file', type=Path, default=PROJECT_ROOT / 'local' / 'hosts.toml')
        command.add_argument('--cases-file', default='local/agent-replay.toml')
        if action == 'collect':
            command.add_argument('--output', type=Path, required=True)
            command.add_argument('--model', required=True, help='openai/<model>')
            command.add_argument('--upstream', required=True, help='Provider base URL including API prefix')
        else:
            command.add_argument('--source', type=Path, required=True)
    args = parser.parse_args(argv)
    return collect(args) if args.action == 'collect' else distribute(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f'replay capture: {error}') from None
