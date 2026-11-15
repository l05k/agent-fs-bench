"""Move verified experiment summaries in one Markdown document; never raw samples.

The test machine keeps its raw tables. It computes per-group statistics, records
the formula and source runs, and packages only those statistics here. Import
neither executes commands nor recomputes results.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re


SUMMARY_COLUMNS = ('series', 'x', 'metric', 'median', 'q1', 'q3', 'n')
FAILURE_COLUMNS = ('group', 'series', 'label', 'count')
META_KEYS = {'xlabel', 'ylabel', 'unit', 'logx', 'logy', 'series_order',
             'source_runs', 'formula'}
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
    if name == 'summary/failures.csv':
        return path
    if (len(path.parts) == 2 and path.parts[0] == 'summary'
            and path.suffix in {'.csv', '.json'} and TOKEN.fullmatch(path.stem)):
        return path
    raise ValueError(f'Only plan.md, execution.md and summary/<group>.{csv,json} '
                     f'are exported; raw results stay on the test machine: {name}')


def _number(value: str, name: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError(f'{name}: expected a number, measured {value!r}') from None
    if not math.isfinite(seconds):
        raise ValueError(f'{name}: expected a finite number, measured {value!r}')
    return seconds


def _summary(text: str, name: str) -> list[str]:
    reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
    if tuple(reader.fieldnames or ()) != SUMMARY_COLUMNS:
        raise ValueError(f'{name}: columns must be {",".join(SUMMARY_COLUMNS)}')
    rows, keys = list(reader), set()
    if not rows:
        raise ValueError(f'{name}: no summary rows')
    for index, row in enumerate(rows, 2):
        if any(value is None for value in row.values()):
            raise ValueError(f'{name}:{index}: row width differs from the header')
        key = row['series'], row['x'], row['metric']
        if any(not part.strip() for part in key):
            raise ValueError(f'{name}:{index}: empty series, x or metric')
        if key in keys:
            raise ValueError(f'{name}:{index}: duplicate series/x/metric {key}')
        keys.add(key)
        median, q1, q3 = (_number(row[key], f'{name}:{index} {key}') for key in ('median', 'q1', 'q3'))
        if not q1 <= median <= q3:
            raise ValueError(f'{name}:{index}: expected q1 <= median <= q3, measured '
                             f'{q1}, {median}, {q3}')
        try:
            count = int(row['n'])
        except ValueError:
            raise ValueError(f'{name}:{index}: n must be an integer, measured {row["n"]!r}') from None
        if count < 1 or str(count) != row['n']:
            raise ValueError(f'{name}:{index}: n must be a positive canonical integer')
    return list(dict.fromkeys(row['series'] for row in rows))


def _meta(text: str, name: str, rows: list[str]) -> None:
    try:
        meta = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f'{name}: invalid JSON: {error}') from None
    if not isinstance(meta, dict) or set(meta) != META_KEYS:
        raise ValueError(f'{name}: metadata keys must be exactly {sorted(META_KEYS)}')
    for key in ('xlabel', 'ylabel', 'unit', 'formula'):
        if not isinstance(meta[key], str) or not meta[key].strip():
            raise ValueError(f'{name}: {key} must be a nonempty string')
    for key in ('logx', 'logy'):
        if type(meta[key]) is not bool:
            raise ValueError(f'{name}: {key} must be true or false')
    runs = meta['source_runs']
    if (not isinstance(runs, list) or not runs
            or any(not isinstance(run, str) or not TOKEN.fullmatch(run) for run in runs)):
        raise ValueError(f'{name}: source_runs must be nonempty path-safe aliases')
    if len(set(runs)) != len(runs):
        raise ValueError(f'{name}: duplicate source_runs')
    order = meta['series_order']
    if (not isinstance(order, list) or not order
            or any(not isinstance(series, str) or not series for series in order)):
        raise ValueError(f'{name}: series_order must be a nonempty list of names')
    if order != rows:
        raise ValueError(f'{name}: series_order must equal the CSV first-appearance order')


def _failures(text: str, groups: set[str], series: dict[str, list[str]]) -> None:
    reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
    if tuple(reader.fieldnames or ()) != FAILURE_COLUMNS:
        raise ValueError(f'summary/failures.csv: columns must be {",".join(FAILURE_COLUMNS)}')
    keys = set()
    for index, row in enumerate(reader, 2):
        if any(value is None for value in row.values()):
            raise ValueError(f'summary/failures.csv:{index}: row width differs from the header')
        group, label = row['group'], row['label']
        if group not in groups or row['series'] not in series[group]:
            raise ValueError(f'summary/failures.csv:{index}: unknown group or series')
        if not TOKEN.fullmatch(label):
            raise ValueError(f'summary/failures.csv:{index}: label must be one ASCII token')
        if (group, row['series'], label) in keys:
            raise ValueError(f'summary/failures.csv:{index}: duplicate failure label')
        keys.add((group, row['series'], label))
        try:
            count = int(row['count'])
        except ValueError:
            raise ValueError(f'summary/failures.csv:{index}: count must be an integer') from None
        if count < 0 or str(count) != row['count']:
            raise ValueError(f'summary/failures.csv:{index}: count must be a canonical nonnegative integer')


def _validate(files: dict[str, str]) -> None:
    for name in files:
        _path(name)
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents):
            raise ValueError(f'Artifact is also used as a directory: {name}')
    for name in ('plan.md', 'execution.md'):
        if not files.get(name, '').strip():
            raise ValueError(f'Packet needs a nonempty {name}')
    groups = {PurePosixPath(name).stem for name in files
              if name.startswith('summary/') and name.endswith('.csv')
              and name != 'summary/failures.csv'}
    if not groups:
        raise ValueError('A packet needs at least one summary/<group> pair')
    expected = {'plan.md', 'execution.md', 'summary/failures.csv'}
    expected |= {f'summary/{group}.{suffix}' for group in groups for suffix in ('csv', 'json')}
    unexpected = sorted(set(files) - expected)
    if unexpected:
        raise ValueError(f'Undeclared exchange artifacts: {unexpected}')
    series = {}
    for group in groups:
        csv_name, meta_name = f'summary/{group}.csv', f'summary/{group}.json'
        if csv_name not in files or meta_name not in files:
            raise ValueError(f'{group}: summary needs both .csv and .json')
        series[group] = _summary(files[csv_name], csv_name)
        _meta(files[meta_name], meta_name, series[group])
    if 'summary/failures.csv' not in files:
        raise ValueError('A packet needs summary/failures.csv, even with only a header')
    _failures(files['summary/failures.csv'], groups, series)


def _manifest(batch: str, files: dict[str, str]) -> dict:
    if not TOKEN.fullmatch(batch) or len(batch) > 80:
        raise ValueError('Batch ID must be 1–80 ASCII letters, digits, dots, underscores or hyphens')
    return {'kind': 'summary', 'batch': batch, 'files': [
        {'path': name, 'bytes': len(text.encode('utf-8')),
         'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        for name, text in sorted(files.items())]}


def export_packet(root: Path, output: Path, *, batch: str) -> dict:
    """Package an already reviewed summary staging directory, never a live result tree."""
    root = root.resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if output.resolve().is_relative_to(root):
        raise ValueError('Write the packet outside the reviewed staging directory')
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f'Staging must contain only regular text files: {path}')
        relative = path.relative_to(root).as_posix()
        if path.is_dir() and (relative == 'results' or relative.startswith('results/')):
            raise ValueError('Raw results stay on the test machine; export only summary/<group>')
        if path.is_dir() and relative != 'summary':
            raise ValueError(f'Unexpected staging directory: {relative}')
        if path.is_file():
            name = relative
            _path(name)
            files[name] = _text(path.read_bytes())
    _validate(files)
    manifest = _manifest(batch, files)
    longest = max((len(match.group()) for text in files.values()
                   for match in re.finditer(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    blocks = [f'# Experiment summary exchange: {batch}\n\n'
              'Verified statistics only; raw samples remain on the test machine. '
              'Import does not execute commands.\n\n',
              f'{fence}afb-manifest\n{json.dumps(manifest, indent=2, ensure_ascii=False)}\n{fence}\n\n']
    for name, text in sorted(files.items()):
        blocks.append(f'{fence}afb-file {name}\n{text}{fence}\n\n')
    packet = ''.join(blocks)
    with output.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(packet)
    return {'batch': batch, 'kind': 'summary', 'files': len(files),
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
    if manifest['kind'] != 'summary':
        raise ValueError('Unsupported exchange kind')
    expected = _manifest(manifest['batch'], files)
    if manifest != expected:
        raise ValueError('File list, byte count or SHA-256 mismatch; recopy the complete source document')
    _validate(files)
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
    export = commands.add_parser('export', help='Package a reviewed summary staging directory')
    export.add_argument('--batch', required=True)
    export.add_argument('--root', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--reviewed', action='store_true', required=True,
                        help='Confirm the staging copy contains no private host/path/endpoint or secret')
    unpack = commands.add_parser('import', help='Check and restore the summaries; do not run them')
    unpack.add_argument('document', type=Path)
    unpack.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'export':
            result = export_packet(args.root, args.output, batch=args.batch)
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
