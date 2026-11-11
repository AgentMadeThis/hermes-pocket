"""Offline selected regressions and standalone admission; no bootstrap/install.

Invoke using the qualified committed Hermes interpreter with -I -B. No CLI
launcher, PM synchronization, credentials, hardware or model calls are used.
This is a trusted-source harness, not an OS sandbox for hostile plugins.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()
            and '.git' not in p.relative_to(root).parts
            and not any(x.startswith('qualification') for x in p.relative_to(root).parts)
            and p.name not in {'RELEASE_MANIFEST.json', 'V2_SOURCE_MANIFEST.json'} and not p.name.startswith('TEST_REPORT')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    before = hashes(root)
    # Caller must supply a private scratch parent; never default to system tmp.
    scratch = Path(os.environ['TMPDIR']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='pocket-release-check-', dir=scratch) as work:
        work = Path(work)
        tree, home, tmp = work / 'candidate', work / 'home', work / 'tmp'
        home.mkdir(mode=0o700); tmp.mkdir(mode=0o700); tree.mkdir()
        for rel in before:
            source = root / rel
            if source.is_symlink():
                raise RuntimeError('symlink in candidate')
            target = tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        (home / 'config.yaml').write_text('gateway:\n  multiplex_profiles: false\n  platforms:\n    pocket:\n      enabled: true\nplugins:\n  enabled: [pocket-platform]\n  isolation: in_process\n  entries:\n    pocket-platform:\n      enabled: true\n      settings:\n        dedicated_pocket_only: true\n')
        target_plugin = home / 'plugins/pocket-platform'
        target_plugin.parent.mkdir()
        shutil.copytree(tree / 'plugin', target_plugin)
        # No inherited provider credentials, profile, approval bypass or PYTHONPATH.
        os.environ.clear()
        os.environ.update(HOME=str(home), HERMES_HOME=str(home), TMPDIR=str(tmp),
                          PATH=os.defpath, LANG='C.UTF-8', PYTHONDONTWRITEBYTECODE='1',
                          PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', HERMES_DISABLE_LAZY_INSTALLS='1')
        tempfile.tempdir = str(tmp)
        # Linux AF_UNIX limits pathname length, not target inode location.
        # Test-only fd alias preserves the actual private socket and SO_PEERCRED
        # while keeping this intentionally long candidate workspace in scope.
        import asyncio
        native_start = asyncio.start_unix_server
        native_open = asyncio.open_unix_connection
        async def short_start(callback, *, path, **kwargs):
            target = Path(path)
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                return await native_start(callback, path=f'/proc/self/fd/{fd}/{target.name}', cleanup_socket=False, **kwargs)
            finally:
                os.close(fd)
        async def short_open(path, **kwargs):
            target = Path(path)
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                return await native_open(f'/proc/self/fd/{fd}/{target.name}', **kwargs)
            finally:
                os.close(fd)
        asyncio.start_unix_server = short_start
        asyncio.open_unix_connection = short_open
        os.chdir(tree)
        sys.dont_write_bytecode = True
        sys.path[:0] = [str(args.hermes_source.resolve(strict=True)), str(tree), str(tree / 'tests/plugin')]
        # Defence in depth for trusted fixtures in this process. Subprocesses
        # receive the cleared environment; their code is separately reviewed.
        fence = {'active': True}
        def audit(event, values):
            if not fence['active']:
                return
            if event == 'open':
                path, mode, flags = values
                if isinstance(path, (str, bytes)):
                    path = Path(os.fsdecode(path)).absolute()
                    if str(path).startswith('/dev/') and str(path) not in ('/dev/null', '/dev/urandom'):
                        raise PermissionError('hardware access forbidden')
                    write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
                    if write and not path.resolve().is_relative_to(work):
                        raise PermissionError('write outside disposable workspace forbidden')
            if event in ('socket.connect', 'socket.bind'):
                address = values[1]
                if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
                    raise PermissionError('non-loopback network forbidden')
                if isinstance(address, str) and not Path(address).resolve().is_relative_to(work):
                    raise PermissionError('foreign Unix socket forbidden')
            if event == 'subprocess.Popen':
                executable = Path(values[0]).resolve()
                if executable != Path(sys.executable).resolve():
                    raise PermissionError('non-Python subprocess forbidden')
        sys.addaudithook(audit)
        report = {'scope': 'selected existing plugin and native/backend fixtures; not whole-project or hardware E2E',
                  'fixture_only': True, 'dependencies_installed': False, 'production_changed': False,
                  'candidate_input_digest': hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                  'runs': {}}
        class Tee(io.StringIO):
            def write(self, value):
                sys.__stderr__.write(value); sys.__stderr__.flush()
                return super().write(value)
        stream = Tee()
        for label, directories in [('offline', [('tests/plugin', 'test_*.py')]),
                                   ('native', [('tests/plugin', 'native_cases.py'), ('tests/integration/backend', 'native_backend.py'), ('tests/plugin', 'v2_cases.py'), ('tests/plugin', 'gateway_cases.py'), ('tests/plugin', 'admission_cases.py')])]:
            suite = unittest.TestSuite()
            for directory, pattern in directories:
                suite.addTests(unittest.TestLoader().discover(str(tree / directory), pattern=pattern))
            def ids(suite):
                for item in suite:
                    if isinstance(item, unittest.TestSuite):
                        yield from ids(item)
                    else:
                        yield item.id()
            nodes = sorted(ids(suite))
            assert len(nodes) == len(set(nodes))
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            report['runs'][label] = {'node_ids': nodes, 'tests_run': result.testsRun,
                'failures': len(result.failures), 'errors': len(result.errors),
                'skipped': len(result.skipped), 'passed': result.wasSuccessful(),
                'failure_details': [{'node': t.id(), 'detail': text.replace(str(work), '<disposable>')}
                                    for t, text in result.failures + result.errors]}
        from hermes_cli.plugin_validate import validate_plugin_dir
        # This library path does not invoke install-deps/PM or CLI bootstrap.
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            admission = validate_plugin_dir(tree / 'plugin').to_dict()
        report['standalone_validation'] = json.loads(json.dumps(admission).replace(str(work), '<disposable>'))
        from gateway.platforms.base import BasePlatformAdapter
        from importlib.metadata import version
        stamp = json.loads((args.hermes_source / 'install-stamp.json').read_text())
        report['native_runtime'] = {'base_version': stamp.get('baseVersion'), 'commit': stamp.get('commit'),
            'python': sys.version.split()[0], 'base_api_module': BasePlatformAdapter.__module__,
            'base_api_in_requested_tree': Path(sys.modules[BasePlatformAdapter.__module__].__file__).resolve().is_relative_to(args.hermes_source.resolve()),
            'meshcore_distribution': None}
        try:
            report['native_runtime']['meshcore_distribution'] = version('meshcore')
        except Exception:
            pass
        # End audit fence scope by outputting after the disposable run: the report
        # is intentionally passed through stdout, never written via the fence.
        report['source_unchanged_after_tests'] = before == hashes(root)
        # rmtree uses relative dir_fd opens: the path-only audit fence cannot
        # resolve those. Drop it only after all fixture/plugin execution ends.
        fence['active'] = False
        os.chdir(root)
    report['disposable_cleanup_verified'] = not work.exists()
    print(json.dumps(report, indent=2))
    return 0 if (all(r['passed'] for r in report['runs'].values())
                 and report['disposable_cleanup_verified']
                 and report['source_unchanged_after_tests']
                 and report['standalone_validation']['ok']) else 1

if __name__ == '__main__':
    raise SystemExit(main())
