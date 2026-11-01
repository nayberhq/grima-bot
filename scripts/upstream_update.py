#!/usr/bin/env python3
"""Prepare and test a Saruman merge in a separate worktree. Never pushes."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess
import sys

UPSTREAM = 'https://github.com/Baviaan/lotro.git'
UPSTREAM_REF = 'upstream/master'


def git(root, *args, capture=False):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          text=True, stdout=subprocess.PIPE if capture else None)


def configure_upstream(root):
    remotes = git(root, 'remote', capture=True).stdout.splitlines()
    if 'upstream' not in remotes:
        git(root, 'remote', 'add', 'upstream', UPSTREAM)
    else:
        url = git(root, 'remote', 'get-url', 'upstream', capture=True).stdout.strip()
        if url != UPSTREAM:
            raise RuntimeError(f'Expected upstream to be {UPSTREAM}; found {url}. No merge attempted.')
    git(root, 'remote', 'set-url', '--push', 'upstream', 'DISABLED')
    if 'origin' in remotes:
        origin = git(root, 'remote', 'get-url', '--push', 'origin', capture=True).stdout.strip()
        if 'baviaan/lotro' in origin.lower():
            raise RuntimeError('origin points to Saruman. Set origin to your own grima-bot fork first.')


def verify(candidate):
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                   cwd=candidate, check=True)
    # Upstream's locale tools are Python 2; compile only the Python 3 application.
    files = sorted((candidate / 'source').glob('*.py'))
    files += [candidate / 'setup.py', candidate / 'scripts' / 'upstream_update.py']
    subprocess.run([sys.executable, '-m', 'py_compile', *map(str, files)],
                   cwd=candidate, check=True)
    git(candidate, 'diff', '--check')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-fetch', action='store_true',
                        help='Use the already-fetched upstream/master ref (offline operation).')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    root = Path(git(root, 'rev-parse', '--show-toplevel', capture=True).stdout.strip())
    if git(root, 'status', '--porcelain', capture=True).stdout.strip():
        raise RuntimeError('Commit or stash local changes before preparing an upstream update.')
    configure_upstream(root)
    if not args.no_fetch:
        git(root, 'fetch', 'upstream')
    incoming = int(git(root, 'rev-list', '--count', f'HEAD..{UPSTREAM_REF}', capture=True).stdout)
    if incoming == 0:
        print('grima-bot already includes the fetched Saruman history. Nothing to merge.')
        return 0

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    branch = f'upstream-sync/{stamp}'
    candidate = root.parent / 'grima-upstream-worktrees' / stamp
    candidate.parent.mkdir(parents=True, exist_ok=True)
    previous_head = git(root, 'rev-parse', 'HEAD', capture=True).stdout.strip()
    git(root, 'worktree', 'add', '-b', branch, str(candidate), 'HEAD')
    print(f'Preparing {incoming} upstream commit(s) in {candidate}', flush=True)
    try:
        git(candidate, '-c', 'user.name=grima-bot', '-c',
            'user.email=grima-bot@users.noreply.github.com',
            'merge', '--no-ff', '--no-edit', UPSTREAM_REF)
        verify(candidate)
    except subprocess.CalledProcessError:
        print(f'Update needs attention in {candidate}. Conflicts or failed tests are preserved.', file=sys.stderr)
        print('Your original branch has not been changed. Nothing was pushed.', file=sys.stderr)
        return 1

    print(f'Checks passed on {branch}. Review the changes before accepting them:')
    print(f'  git diff {shlex.quote(previous_head)}..{shlex.quote(branch)}')
    print('To accept the tested update on your original branch:')
    print(f'  git merge --ff-only {shlex.quote(branch)}')
    print(f'To remove the extra checkout after review: git worktree remove {shlex.quote(str(candidate))}')
    print('Nothing was pushed. Live Discord verification is still needed before deployment.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
