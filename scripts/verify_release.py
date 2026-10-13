"""Verify the selected interpreter's installed package against the source tree."""
import hashlib
import json
import argparse
from pathlib import Path

import assistant_kernel
from assistant_kernel.config import KernelSettings


def verify(source_root: Path | None = None) -> dict:
    source = (source_root or Path(__file__).resolve().parents[1]) / 'src/assistant_kernel'
    installed = Path(assistant_kernel.__file__).parent
    entries = {}
    mismatches = []
    for path in source.rglob('*'):
        if not path.is_file() or path.suffix not in {'.py', '.md', '.yaml', '.json'} or '__pycache__' in path.parts:
            continue
        relative = path.relative_to(source)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries[relative.as_posix()] = digest
        deployed = installed / relative
        if not deployed.exists() or hashlib.sha256(deployed.read_bytes()).hexdigest() != digest:
            mismatches.append(relative.as_posix())
    extras = sorted(path.relative_to(installed).as_posix() for path in installed.rglob('*')
                    if path.is_file() and path.suffix in {'.py', '.md', '.yaml', '.json'}
                    and '__pycache__' not in path.parts and path.relative_to(installed).as_posix() not in entries)
    if extras:
        raise RuntimeError(f'installed package contains files absent from source: {extras}')
    if mismatches:
        raise RuntimeError(f'installed package differs from source: {mismatches}')
    return {'version':assistant_kernel.__version__, 'schema_version':assistant_kernel.__schema_version__,
            'matched_files':len(entries), 'source_hashes':entries}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-root',type=Path)
    args=parser.parse_args()
    print(json.dumps(verify(args.source_root), sort_keys=True))
