# Copyright 2024-2026 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Offline CLI regression tests for the standalone benchmark layout."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import release_cli
import snapshot


ROOT = Path(__file__).resolve().parents[1]


class StandaloneCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'framework'
        self.repo.mkdir()
        (self.repo / 'pom.xml').write_text(
            '<project><properties><revision>fixture</revision></properties></project>')
        self.manifest = json.loads((ROOT / 'contracts/framework-v1.json').read_text())
        for item in self.manifest['tests']:
            source = self.repo / item['source']
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('// Offline fixture; never compiled.\n')
        self.other_cwd = self.root / 'unrelated'
        self.other_cwd.mkdir()
        self.original_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.original_cwd)
        patches = {
            'environment': mock.patch.dict(os.environ, {}, clear=True),
            'freeze': mock.patch('snapshot.freeze', return_value={'version': 'fixture'}),
            'verify': mock.patch('runtime.framework_contract.verify',
                                 return_value={'passed': True, 'classes': {}}),
            'maven': mock.patch('release_cli.subprocess.run',
                                return_value=mock.Mock(returncode=0)),
            'git': mock.patch('release_cli.subprocess.check_output',
                              side_effect=lambda command: b'a' * 40 + b'\n'
                              if 'rev-parse' in command else b''),
        }
        for name, patch in patches.items():
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

    def invoke(self, cli, output, repo=None, verify_only=True):
        argv = ['--output', str(output)]
        if repo is not None:
            argv += ['--repo', str(repo)]
        if cli == 'contracts':
            argv.insert(0, 'contracts')
            if verify_only:
                argv.append('--verify-only')
            main = release_cli.main
        else:
            main = snapshot.main
        with redirect_stdout(io.StringIO()):
            return main(argv)

    def assert_parser_error(self, cli, output, message, repo=None):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            self.invoke(cli, output, repo)
        self.assertEqual(error.exception.code, 2)
        self.assertIn('error:', stderr.getvalue())
        self.assertIn(message, stderr.getvalue())
        self.assertFalse(output.parent.exists())
        self.freeze.assert_not_called()
        self.verify.assert_not_called()
        self.maven.assert_not_called()
        self.git.assert_not_called()

    def assert_candidate(self, cli, output):
        if cli == 'snapshot':
            self.freeze.assert_called_with(self.repo, output)
        else:
            self.verify.assert_called_with(self.repo, self.manifest)
            self.git.assert_any_call(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'])
            receipt = json.loads((output / 'framework-contracts.json').read_text())
            self.assertEqual(receipt['candidate_sha'], 'a' * 40)
            self.assertFalse(receipt['fresh_execution'])
        self.maven.assert_not_called()

    def test_missing_or_empty_configuration_creates_no_output(self):
        for value in (None, ''):
            if value is None:
                os.environ.pop('AGENTSCOPE_JAVA_REPO', None)
            else:
                os.environ['AGENTSCOPE_JAVA_REPO'] = value
            for cli in ('snapshot', 'contracts'):
                with self.subTest(cli=cli, environment=value):
                    output = self.root / ('missing-' + cli + str(value)) / 'output'
                    self.assert_parser_error(cli, output, '--repo or set AGENTSCOPE_JAVA_REPO')

    def test_environment_fallback_is_independent_of_cwd(self):
        os.environ['AGENTSCOPE_JAVA_REPO'] = str(self.repo)
        for index, cwd in enumerate((ROOT, self.other_cwd)):
            os.chdir(cwd)
            for cli in ('snapshot', 'contracts'):
                with self.subTest(cli=cli, cwd=cwd):
                    output = self.root / ('env-' + cli + str(index))
                    self.assertEqual(self.invoke(cli, output), 0)
                    self.assert_candidate(cli, output)

    def test_explicit_repo_overrides_environment_from_any_cwd(self):
        os.environ['AGENTSCOPE_JAVA_REPO'] = str(self.root / 'invalid-env-repo')
        for index, cwd in enumerate((ROOT, self.other_cwd)):
            os.chdir(cwd)
            for cli in ('snapshot', 'contracts'):
                with self.subTest(cli=cli, cwd=cwd):
                    output = self.root / ('explicit-' + cli + str(index))
                    self.assertEqual(self.invoke(cli, output, self.repo), 0)
                    self.assert_candidate(cli, output)

    def test_relative_explicit_repo_resolves_from_caller_cwd(self):
        os.chdir(self.root)
        for cli in ('snapshot', 'contracts'):
            with self.subTest(cli=cli):
                output = self.root / ('relative-' + cli)
                self.assertEqual(self.invoke(cli, output, Path('framework')), 0)
                self.assert_candidate(cli, output)

    def test_invalid_explicit_root_never_falls_back_or_creates_output(self):
        os.environ['AGENTSCOPE_JAVA_REPO'] = str(self.repo)
        pom_directory = self.root / 'pom-is-directory'
        (pom_directory / 'pom.xml').mkdir(parents=True)
        for index, repo in enumerate((self.root / 'absent', self.other_cwd, pom_directory, ROOT)):
            for cli in ('snapshot', 'contracts'):
                with self.subTest(cli=cli, repo=repo):
                    output = self.root / ('invalid-' + cli + str(index)) / 'output'
                    self.assert_parser_error(cli, output, 'root pom.xml', repo)

    def test_invalid_environment_root_creates_no_output(self):
        os.environ['AGENTSCOPE_JAVA_REPO'] = str(self.other_cwd)
        for cli in ('snapshot', 'contracts'):
            with self.subTest(cli=cli):
                output = self.root / ('invalid-env-' + cli) / 'output'
                self.assert_parser_error(cli, output, 'root pom.xml')

    def test_missing_contract_source_is_checked_before_output(self):
        (self.repo / self.manifest['tests'][0]['source']).unlink()
        output = self.root / 'missing-contract' / 'output'
        with self.assertRaisesRegex(ValueError, 'Candidate lacks required contract'):
            self.invoke('contracts', output, self.repo)
        self.assertFalse(output.parent.exists())
        self.verify.assert_not_called()
        self.maven.assert_not_called()
        self.git.assert_not_called()

    def test_contract_execution_uses_framework_repo_without_real_maven(self):
        output = self.root / 'mocked-contract-run'
        os.chdir(self.other_cwd)
        os.environ['AGENTSCOPE_JAVA_REPO'] = str(self.repo)
        self.assertEqual(self.invoke('contracts', output, verify_only=False), 0)
        self.assertEqual(self.maven.call_args.kwargs['cwd'], self.repo)
        self.assertEqual(self.maven.call_args.args[0][0], 'mvn')
        self.verify.assert_called_once_with(self.repo, self.manifest)
        receipt = json.loads((output / 'framework-contracts.json').read_text())
        self.assertTrue(receipt['fresh_execution'])
        self.assertEqual(receipt['maven_exit'], 0)


class StandaloneEntryPointTest(unittest.TestCase):
    def test_missing_configuration_from_unrelated_cwd_creates_no_output(self):
        environment = dict(os.environ)
        environment.pop('AGENTSCOPE_JAVA_REPO', None)
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            for script, command in (('snapshot.py', []), ('release_cli.py', ['contracts'])):
                with self.subTest(script=script):
                    output = cwd / script / 'output'
                    result = subprocess.run(
                        [sys.executable, '-B', str(ROOT / script), *command,
                         '--output', str(output)],
                        cwd=cwd, env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('--repo or set AGENTSCOPE_JAVA_REPO', result.stderr)
                    self.assertIn('error:', result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertFalse(output.parent.exists())


if __name__ == '__main__':
    unittest.main()
