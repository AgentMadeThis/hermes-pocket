"""Release packaging regressions, without provider/serial/model dependencies."""
import ast
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ReleasePackaging(unittest.TestCase):
    def test_explicit_public_source_allowlist_matches_membership(self):
        manifest = json.loads((ROOT / 'PUBLIC_SOURCE_ALLOWLIST.json').read_text())
        actual = {str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file()}
        self.assertEqual(actual, set(manifest['files']) | {'PUBLIC_SOURCE_ALLOWLIST.json'})

    def test_no_binary_private_or_generated_payloads(self):
        for p in ROOT.rglob('*'):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            self.assertFalse(p.is_symlink())
            self.assertFalse(set(rel.parts) & {'__pycache__', '.git', 'evidence', 'node_modules', '.pio'})
            self.assertNotIn(p.suffix, {'.pyc', '.bin', '.elf', '.sqlite', '.db', '.log', '.png', '.gif'})
            p.read_bytes().decode('utf-8')

    def test_source_privacy_and_no_live_home_paths(self):
        for directory in ('plugin', 'integration', 'tests'):
            for p in (ROOT / directory).rglob('*.py'):
                text = p.read_text()
                self.assertIsNone(re.search(r'/home/[A-Za-z0-9_]+/|' + '/var/' + r'lib/|\b(?:10|192\.168)\.\d+\.\d+', text), str(p.relative_to(ROOT)))
        from plugin.music import APPROVED_SOURCES
        self.assertEqual(APPROVED_SOURCES, {'lounge', 'house', 'downtempo', 'work', 'nature', 'meditation', 'all'})

    def test_bridge_internal_import_closure_is_present(self):
        for p in (ROOT / 'integration/host/bridge').glob('*.py'):
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                    self.assertTrue((p.parent / (node.module.split('.')[0] + '.py')).is_file(), node.module)
        self.assertFalse((ROOT / 'integration/host/bridge/hermes.py').exists())
        self.assertFalse((ROOT / 'integration/host/bridge/__main__.py').exists())

    def test_placeholders_fail_owner_enrollment(self):
        from plugin.protocol import full_key
        cfg = json.loads((ROOT / 'config/pocket.extra.json.example').read_text())
        self.assertIs(cfg['home_music'], False)
        with self.assertRaises(ValueError):
            full_key(cfg['owners'][0])
        template = (ROOT / 'catalog/pocket-platform.yaml.TEMPLATE').read_text()
        self.assertIn('<EXACT_REVIEWED_40_LOWERCASE_HEX_COMMIT_SHA>', template)
        self.assertIn('Copyright (c) 2026 Hermes Pocket contributors', (ROOT / 'LICENSE').read_text())
