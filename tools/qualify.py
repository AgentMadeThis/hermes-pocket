"""Qualification only; read selected PM generation without launcher/bootstrap.
Run with system Python -B. No installation or production configuration changes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.timeout < 600:
        parser.error('full qualification timeout must be at least 600 seconds')
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(args.hermes_source))
    from pm.environments import project_python, selected_venv, site_packages
    python = project_python(args.hermes_source)
    site = site_packages(selected_venv(args.hermes_source))
    out = args.evidence_dir.resolve()
    if out.is_relative_to(root):
        parser.error('evidence must be outside the public source tree')
    out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='qualification-', dir=out) as work:
        work = Path(work)
        home = work / 'home'; home.mkdir(mode=0o700)
        tmp = work / 'tmp'; tmp.mkdir(mode=0o700)
        env = {'HOME': str(home), 'HERMES_HOME': str(home), 'TMPDIR': str(tmp),
               'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
        code = ('import sys,runpy; sys.path[:0]=' + repr([str(site), str(args.hermes_source)])
                + '; sys.argv=' + repr([str(root/'tools/check_candidate.py'), '--hermes-source', str(args.hermes_source), '--report', 'stdout'])
                + ';runpy.run_path(sys.argv[0],run_name="__main__")')
        started = time.monotonic()
        with (out/'stdout.json').open('w') as stdout, (out/'stderr.txt').open('w') as stderr:
            child = subprocess.Popen([str(python), '-I', '-B', '-W', 'error::ResourceWarning', '-c', code],
                                     env=env, cwd=work, stdout=stdout, stderr=stderr)
            (out/'process.json').write_text(json.dumps({'pid': child.pid, 'timeout': args.timeout}))
            try:
                rc = child.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(); rc = 124
        result = {'exit': rc, 'elapsed_seconds': round(time.monotonic()-started, 3),
                  'python': str(python), 'site': str(site), 'timeout_seconds': args.timeout,
                  'cleared_environment': True, 'launcher_invoked': False}
    result['outer_cleanup_verified'] = not work.exists()
    (out/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))
    return rc

if __name__ == '__main__':
    raise SystemExit(main())
