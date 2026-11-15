"""Copy reviewed experiment files in one Markdown document; never execute or plot them."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re


TEXT_SUFFIXES = {'.md', '.csv', '.json', '.txt', '.patch'}
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
BLOCK = re.compile(r'(`{3,})(afb-manifest|afb-file [^\r\n]+)\n\Z')


def _text(data: bytes) -> str:
    text = data.decode('utf-8').replace('\r\n', '\n')
    if '\r' in text or '\x00' in text:
        raise ValueError('Only UTF-8 text with LF or CRLF line endings is accepted')
    return text + '\n' if text and not text.endswith('\n') else text


def _path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path.as_posix() != name or path.is_absolute()
            or any(not TOKEN.fullmatch(part) or part.startswith('.') for part in path.parts)):
        raise ValueError(f'Unsafe packet path: {name!r}')
    if name in {'plan.md', 'execution.md'}:
        return path
    if path.parts[0] in {'inventory', 'legacy'} and len(path.parts) >= 2:
        if path.suffix in TEXT_SUFFIXES:
            return path
    if (len(path.parts) == 4 and path.parts[0] == 'results'
            and (path.name == 'config.json' or path.suffix == '.csv')):
        return path
    raise ValueError(f'Not an exchange artifact: {name}')


def _csv(text: str, name: str) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
    header = reader.fieldnames
    if not header or any(not column for column in header) or len(set(header)) != len(header):
        raise ValueError(f'{name}: missing or duplicate CSV columns')
    rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError(f'{name}: CSV row width does not match header')
    return header, rows


def _validate(kind: str, files: dict[str, str]) -> None:
    if kind not in {'inventory', 'results'}:
        raise ValueError(f'Unknown packet kind: {kind!r}')
    for name in files:
        _path(name)
        if any(parent.as_posix() in files for parent in _path(name).parents):
            raise ValueError(f'Artifact is also used as a directory: {name}')
    for name in ('plan.md', 'execution.md'):
        if not files.get(name, '').strip():
            raise ValueError(f'Packet needs a nonempty {name}')
    runs: dict[str, dict[str, str]] = {}
    for name, text in files.items():
        path = _path(name)
        if path.parts[0] == 'results':
            runs.setdefault(path.parent.as_posix(), {})[path.name] = text
        # Historical formats remain opaque. Do not reinterpret or repair them.
    if kind == 'inventory':
        if runs or not any(name.startswith('inventory/') for name in files):
            raise ValueError('Inventory needs inventory/ artifacts; historical data belongs in legacy/')
        return
    if not runs:
        raise ValueError('A results packet needs at least one results/<benchmark>/<run-id>/ run')
    for directory, artifacts in runs.items():
        if 'config.json' not in artifacts:
            raise ValueError(f'{directory}: missing config.json')
        snapshot = json.loads(artifacts['config.json'])
        required = {'benchmark', 'run_id', 'git_commit', 'source_sha256', 'config',
                    'nodes', 'planned_targets', 'targets', 'tables'}
        if not isinstance(snapshot, dict) or not required <= snapshot.keys():
            raise ValueError(f'{directory}: incomplete current result metadata; use legacy/ for old data')
        _, benchmark, run_id = PurePosixPath(directory).parts
        if (snapshot['benchmark'], snapshot['run_id']) != (benchmark, run_id):
            raise ValueError(f'{directory}: run identity differs from config.json')
        planned = snapshot['planned_targets']
        completed = [target['name'] for target in snapshot['targets']]
        if (len(set(planned)) != len(planned) or len(set(completed)) != len(completed)
                or not set(completed) <= set(planned)):
            raise ValueError(f'{directory}: inconsistent planned/completed targets')
        for table, schema in snapshot['tables'].items():
            name = f'{table}.csv'
            if name not in artifacts:
                raise ValueError(f'{directory}: missing declared table {name}')
            header, _ = _csv(artifacts[name], f'{directory}/{name}')
            if header != schema['keys'] + schema['metrics']:
                raise ValueError(f'{directory}/{name}: columns differ from config.json')
        for name, text in artifacts.items():
            if not name.endswith('.csv'):
                continue
            header, rows = _csv(text, f'{directory}/{name}')
            if 'target' not in header or any(row['target'] not in planned for row in rows):
                raise ValueError(f'{directory}/{name}: unknown or missing target')
            # A failed target can leave a CSV before its schema is checkpointed.
            # Keep it, and keep its target unfinished; analysis already filters it.


def _manifest(kind: str, batch: str, files: dict[str, str]) -> dict:
    if not TOKEN.fullmatch(batch) or len(batch) > 80:
        raise ValueError('Batch ID must be 1–80 ASCII letters, digits, dots, underscores or hyphens')
    return {'kind': kind, 'batch': batch, 'files': [
        {'path': name, 'bytes': len(text.encode('utf-8')),
         'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        for name, text in sorted(files.items())]}


def export_packet(root: Path, output: Path, *, kind: str, batch: str) -> dict:
    """Package only an explicitly reviewed staging directory, not a live result tree."""
    root = root.resolve(strict=True)
    if output.resolve().is_relative_to(root):
        raise ValueError('Write the packet outside the reviewed staging directory')
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f'Staging must contain only regular text files: {path}')
        if path.is_file():
            name = path.relative_to(root).as_posix()
            _path(name)
            files[name] = _text(path.read_bytes())
    _validate(kind, files)
    manifest = _manifest(kind, batch, files)
    longest = max((len(match.group()) for text in files.values()
                   for match in re.finditer(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    blocks = [f'# Experiment exchange: {batch}\n\n'
              'Reviewed text artifacts; no code is executed by import.\n\n',
              f'{fence}afb-manifest\n{json.dumps(manifest, indent=2, ensure_ascii=False)}\n{fence}\n\n']
    for name, text in sorted(files.items()):
        blocks.append(f'{fence}afb-file {name}\n{text}{fence}\n\n')
    packet = ''.join(blocks)
    with output.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(packet)
    return {'batch': batch, 'kind': kind, 'files': len(files),
            'document_bytes': len(packet.encode('utf-8'))}


def _unpack(text: str) -> tuple[dict, dict[str, str]]:
    lines = text.splitlines(keepends=True)
    manifest, files = None, {}
    index = 0
    while index < len(lines):
        match = BLOCK.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        fence, label = match.groups()
        end = index + 1
        while end < len(lines) and lines[end].removesuffix('\n') != fence:
            end += 1
        if end == len(lines):
            raise ValueError(f'Unclosed exchange block: {label}')
        body = ''.join(lines[index + 1:end])
        if label == 'afb-manifest':
            if manifest is not None:
                raise ValueError('Duplicate exchange manifest')
            manifest = json.loads(body)
        else:
            name = label.removeprefix('afb-file ')
            _path(name)
            if name in files:
                raise ValueError(f'Duplicate exchange artifact: {name}')
            files[name] = body
        index = end + 1
    if not isinstance(manifest, dict) or set(manifest) != {'kind', 'batch', 'files'}:
        raise ValueError('Missing or invalid exchange manifest')
    expected = _manifest(manifest['kind'], manifest['batch'], files)
    if manifest != expected:
        raise ValueError('File list, byte count or SHA-256 mismatch; recopy the complete source document')
    _validate(manifest['kind'], files)
    return manifest, files


def import_packet(document: Path, output: Path) -> dict:
    """Validate the entire packet before creating a new output directory."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    raw = document.read_bytes()
    manifest, files = _unpack(_text(raw))
    output.mkdir(parents=True)
    for name, text in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
    receipt = {**manifest, 'packet_sha256': hashlib.sha256(raw).hexdigest()}
    (output / 'exchange.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export', help='Package an already reviewed text staging directory')
    export.add_argument('--kind', choices=('inventory', 'results'), required=True)
    export.add_argument('--batch', required=True)
    export.add_argument('--root', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--reviewed', action='store_true', required=True,
                        help='Confirm this staging copy contains no private host/path/endpoint or secret')
    unpack = commands.add_parser('import', help='Check and restore text artifacts; do not run them')
    unpack.add_argument('document', type=Path)
    unpack.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'export':
            result = export_packet(args.root, args.output, kind=args.kind, batch=args.batch)
        else:
            manifest = import_packet(args.document, args.output)
            result = {'batch': manifest['batch'], 'kind': manifest['kind'],
                      'files': len(manifest['files']), 'output': str(args.output)}
    except (ValueError, OSError, KeyError, TypeError, csv.Error) as error:
        parser.exit(1, f'exchange: {error}\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
