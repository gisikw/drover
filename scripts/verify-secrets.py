"""Pre-push check: exact private denylist + full reachable blob scan + gitleaks.

The denylist is an OUTSIDE-REPOSITORY 0600 file, one sensitive value per line.
Include bootstrap passwords, generated bearer tokens and private key body lines.
Never pass secret values on the command line. Run from each repository root.
"""
import argparse
from pathlib import Path
import subprocess
import sys


def git(*args):
    return subprocess.check_output(['git', *args])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--denylist', required=True)
    args = p.parse_args()
    path = Path(args.denylist).resolve()
    root = Path(git('rev-parse', '--show-toplevel').decode().strip()).resolve()
    if path.is_relative_to(root) or path.stat().st_mode & 0o077:
        raise SystemExit('denylist must be private and outside repository')
    needles = [line for line in path.read_bytes().splitlines() if line]
    if not needles:
        raise SystemExit('empty denylist')

    def inspect(data, label):
        if any(needle in data for needle in needles):
            raise SystemExit('SECRET MATCH (redacted): ' + label)

    # Explicit git grep of tracked working files and index, without secret output.
    for flags in ([], ['--cached']):
        result = subprocess.run(['git', 'grep', *flags, '-I', '-l', '-F', '-f', str(path)], capture_output=True)
        if result.returncode != 1:
            raise SystemExit('git grep found a secret or failed (output withheld)')
    inspect(git('diff', '--cached', '--binary'), 'staged diff')
    objects = set(line.split()[0] for line in git('rev-list', '--objects', '--all').splitlines())
    process = subprocess.Popen(['git', 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    count = 0
    try:
        for oid in objects:
            process.stdin.write(oid + b'\n')
            process.stdin.flush()
            header = process.stdout.readline().split()
            size = int(header[2])
            body = process.stdout.read(size)
            process.stdout.read(1)
            if header[1] == b'blob':
                inspect(body, 'history blob ' + oid.decode())
                count += 1
    finally:
        process.stdin.close()
        process.wait()
    subprocess.run(['gitleaks', 'dir', '.', '--redact', '--no-banner'], check=True)
    subprocess.run(['gitleaks', 'git', '.', '--log-opts=--all', '--redact', '--no-banner'], check=True)
    print(f'PASS: tracked files, index, staged diff, {count} reachable history blobs, gitleaks working tree/history')


if __name__ == '__main__':
    main()
