import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from hbench.__main__ import main
from hbench.datasets import RUNTIME_IMAGE, TAU3_MAIN_IMAGE
from hbench.jobs import load_profile


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        run = patch('subprocess.run')
        self.run = run.start()
        self.addCleanup(run.stop)

    def test_docker_profile_is_listed_without_execution(self):
        with patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(main(['list', 'clusters']), 0)
        self.assertIn('docker', json.loads(stdout.getvalue())['clusters'])
        self.run.assert_not_called()

    def test_docker_plan_and_dry_run_delegate_to_harbor(self):
        dataset = self.root / 'datasets' / 'terminal-bench'
        task = dataset / 'cpu-task'
        task.mkdir(parents=True)
        (task / 'task.toml').write_text('[environment]\ngpus = 0\n')
        (dataset / 'manifest.json').write_text(json.dumps({'profile': load_profile('benchmarks', 'terminal-bench')}))
        output = self.root / 'docker.yaml'
        with patch.dict(os.environ, {}, clear=True), patch('sys.stdout', new_callable=io.StringIO):
            result = main(['plan', '--benchmark', 'terminal-bench', '--cluster', 'docker',
                           '--dataset-root', str(dataset.parent), '--n-tasks', '1', '--exclude-gpu',
                           '--name', 'docker-test', '--output', str(output)])
        self.assertEqual(result, 0)
        job = yaml.safe_load(output.read_text())
        self.assertEqual(job['environment']['import_path'], 'hbench.docker_environment:FrozenDockerEnvironment')
        self.assertEqual(job['environment']['kwargs']['harness_runtime_image'], 'hbench/harness-runtime:0.1.0')
        self.assertEqual(json.loads(output.with_suffix('.plan.json').read_text())['planned_trials'], 3)
        self.run.assert_not_called()
        self.run.return_value.returncode = 0
        with patch('hbench.__main__.harbor_command', return_value='/local/harbor'):
            self.assertEqual(main(['run', '--config', str(output), '--dry-run']), 0)
        argv = self.run.call_args.args[0]
        self.assertEqual(argv, ['/local/harbor', 'run', '-c', str(output.resolve()),
                                '--dry-run', '--skip-credential-checks'])
        self.assertIn(str(Path(__file__).resolve().parents[2]), self.run.call_args.kwargs['env']['PYTHONPATH'])

    def test_matrix_config_renders_eighteen_combinations_and_summary_groups(self):
        root = self.root / 'datasets'
        for name in ('terminal-bench', 'swe-bench-verified', 'tau3-bench'):
            task = root / name / 'task-1'
            task.mkdir(parents=True)
            (task / 'task.toml').write_text('[environment]\ngpus = 0\n')
            (root / name / 'manifest.json').write_text(json.dumps({'profile': load_profile('benchmarks', name)}))
        matrix = yaml.safe_load((Path(__file__).resolve().parents[2] / 'configs/matrices/bailian-18.yaml').read_text())
        matrix.update(dataset_root=str(root), concurrency=3)
        config = self.root / 'matrix.yaml'
        config.write_text(yaml.safe_dump(matrix))
        output = self.root / 'plans' / 'matrix-job.yaml'
        with patch.dict(os.environ, {'HB_MODEL_A_ID': 'qwen-a', 'HB_MODEL_B_ID': 'qwen-b'}, clear=True), \
                patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(main(['matrix', '--config', str(config), '--output', str(output), '--name', 'm18']), 0)
        self.assertEqual(json.loads(stdout.getvalue())['combinations'], 18)
        job = yaml.safe_load(output.read_text())
        contract = json.loads(output.with_suffix('.plan.json').read_text())
        self.assertEqual(job['job_name'], 'm18')
        self.assertEqual(job['n_concurrent_trials'], 3)
        self.assertEqual(len(job['agents']), 6)
        self.assertEqual(contract['planned_trials'], 18)
        self.assertEqual(len({(c['dataset'], c['model'], c['harness']) for c in contract['combinations']}), 18)
        from harbor.models.job.config import JobConfig
        from hbench.summary import _expected_groups
        job_dir = self.root / 'job'
        job_dir.mkdir()
        (job_dir / 'config.json').write_text(JobConfig.model_validate(job).model_dump_json())
        groups = _expected_groups(job_dir)
        self.assertEqual(len(groups), 18)
        self.assertEqual(set(groups.values()), {1})
        self.assertEqual({key[1] for key in groups}, {'qwen-a', 'qwen-b'})
        override = self.root / 'plans' / 'override.yaml'
        with patch.dict(os.environ, {}, clear=True), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['matrix', '--config', str(config), '--output', str(override), '--name', 'o',
                                   '--cluster', 'ack', '--concurrency', '8', '--n-tasks', '1']), 0)
        job = yaml.safe_load(override.read_text())
        self.assertEqual(job['environment']['import_path'], 'hbench.environment:KubernetesEnvironment')
        self.assertEqual(job['n_concurrent_trials'], 8)
        self.run.assert_not_called()

    def test_matrix_unknown_settings_rejected_without_outputs(self):
        config = self.root / 'bad.yaml'
        config.write_text(yaml.safe_dump({'name': 'x', 'cluster': 'docker', 'models': ['example'],
                                          'benchmarks': ['terminal-bench'], 'harnesses': ['pi'], 'api_key': 'x'}))
        output = self.root / 'bad-job.yaml'
        with patch('sys.stderr', new_callable=io.StringIO) as stderr, self.assertRaises(SystemExit):
            main(['matrix', '--config', str(config), '--output', str(output)])
        self.assertIn('Unknown matrix settings', stderr.getvalue())
        self.assertFalse(output.exists())

    def test_existing_plan_outputs_refused_before_profile_rendering(self):
        for suffix in ('.yaml', '.plan.json'):
            with self.subTest(suffix=suffix):
                output = self.root / 'job.yaml'
                existing = output.with_suffix(suffix)
                existing.write_text('keep-existing')
                with patch('hbench.__main__.load_profile') as load, patch('hbench.__main__.render_job') as render, \
                        patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as error:
                        main(['plan', '--output', str(output), '--image-map', str(self.root / 'missing.json')])
                self.assertEqual(error.exception.code, 2)
                self.assertIn('already exists', stderr.getvalue())
                self.assertEqual(existing.read_text(), 'keep-existing')
                load.assert_not_called()
                render.assert_not_called()
                self.run.assert_not_called()
                existing.unlink()

    def test_malformed_profile_and_job_yaml_do_not_leak_source_credentials(self):
        for command in ('plan', 'run'):
            with self.subTest(command=command):
                config = self.root / f'malformed-{command}.yaml'
                config.write_text('api_key: "fake-secret-must-not-appear\n')
                argv = (['plan', '--model', str(config), '--cluster', 'docker',
                         '--output', str(self.root / 'not-written.yaml')]
                        if command == 'plan' else ['run', '--config', str(config), '--dry-run'])
                with patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as error:
                        main(argv)
                self.assertEqual(error.exception.code, 2)
                self.assertIn('Invalid YAML configuration', stderr.getvalue())
                self.assertNotIn('fake-secret-must-not-appear', stderr.getvalue())
                self.run.assert_not_called()
                self.assertFalse((self.root / 'not-written.yaml').exists())

    def test_secret_model_settings_refused_without_outputs_or_credential_error_leak(self):
        model = load_profile('models', 'example')
        model['metadata'] = {'nested': [{'access_token': 'fake-credential-must-not-leak'}]}
        model_path = self.root / 'model.yaml'
        model_path.write_text(yaml.safe_dump(model))
        output = self.root / 'job.yaml'
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as error:
                main(['plan', '--model', str(model_path), '--harness', 'codex', '--output', str(output)])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('Unsupported model settings', stderr.getvalue())
        self.assertNotIn('fake-credential-must-not-leak', stderr.getvalue())
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix('.plan.json').exists())
        self.run.assert_not_called()

    def test_existing_mirror_output_refused_before_inventory_or_skopeo(self):
        output = self.root / 'map.json'
        output.write_text('keep-existing')
        with patch('hbench.images.image_inventory') as inventory, patch('hbench.images.mirror') as mirror, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as error:
                main(['images', 'mirror', str(self.root / 'dataset'), '--registry', 'registry.example/team',
                      '--output', str(output), '--built-image-map', str(self.root / 'missing.json'), '--execute'])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('already exists', stderr.getvalue())
        inventory.assert_not_called()
        mirror.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(output.read_text(), 'keep-existing')

    def test_mirror_built_map_bridges_logical_and_published_refs(self):
        published = {RUNTIME_IMAGE: 'published.example/team/runtime:1',
                     TAU3_MAIN_IMAGE: 'published.example/team/tau3:1'}
        upstream = 'upstream.example/team/task:1'
        raw = b'{"schemaVersion":2}'
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                built = self.root / f'built-{wrapped}.json'
                built.write_text(json.dumps({'image_map': published, 'executed': False} if wrapped else published))
                output = self.root / f'mirrored-{wrapped}.json'
                self.run.reset_mock()
                self.run.side_effect = [subprocess.CompletedProcess([], 0),
                                        subprocess.CompletedProcess([], 0, stdout=raw)] * 3
                with patch('hbench.images.image_inventory', return_value=[RUNTIME_IMAGE, TAU3_MAIN_IMAGE, upstream]), \
                        patch('sys.stdout', new_callable=io.StringIO):
                    result = main(['images', 'mirror', str(self.root / 'dataset'), '--registry', 'mirror.example/team',
                                   '--built-image-map', str(built), '--output', str(output), '--execute'])
                self.assertEqual(result, 0)
                value = json.loads(output.read_text())
                self.assertTrue(value['executed'])
                for logical, source in published.items():
                    self.assertEqual(value['image_map'][logical], value['image_map'][source])
                    self.assertTrue(value['image_map'][logical].endswith('@' + digest))
                sources = {call.args[0][-2] for call in self.run.call_args_list if call.args[0][1] == 'copy'}
                self.assertEqual(sources, {'docker://' + ref for ref in [*published.values(), upstream]})
                self.assertEqual(self.run.call_count, 6)
                self.assertEqual([call.args[0] for call in self.run.call_args_list], value['commands'])

    def test_mirror_execute_rejects_unresolved_logical_refs_before_subprocess(self):
        for mapping in ({}, {RUNTIME_IMAGE: 'hbench/not-published:1'}):
            with self.subTest(mapping=mapping):
                built = self.root / 'built.json'
                built.write_text(json.dumps(mapping))
                output = self.root / 'map.json'
                with patch('hbench.images.image_inventory', return_value=[RUNTIME_IMAGE]), \
                        patch('hbench.images.mirror') as mirror, patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as error:
                        main(['images', 'mirror', str(self.root / 'dataset'), '--registry', 'registry.example/team',
                              '--built-image-map', str(built), '--output', str(output), '--execute'])
                self.assertEqual(error.exception.code, 2)
                self.assertIn('Build and publish', stderr.getvalue())
                mirror.assert_not_called()
                self.run.assert_not_called()
                self.assertFalse(output.exists())

    def test_mirror_dry_plan_allows_unpublished_logical_refs_without_subprocess(self):
        output = self.root / 'map.json'
        with patch('hbench.images.image_inventory', return_value=[RUNTIME_IMAGE]), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['images', 'mirror', str(self.root / 'dataset'), '--registry', 'registry.example/team',
                                   '--output', str(output)]), 0)
        value = json.loads(output.read_text())
        self.assertIn(RUNTIME_IMAGE, value['image_map'])
        self.assertFalse(value['executed'])
        self.run.assert_not_called()

    def test_cli_build_tau3_dataset_is_optional_and_execution_is_opt_in(self):
        runtime = 'registry.example/team/runtime:1'
        for dataset in (None, self.root / 'tau3'):
            with self.subTest(dataset=dataset), patch('hbench.images.build', return_value={'executed': False}) as build, \
                    patch('sys.stdout', new_callable=io.StringIO):
                argv = ['images', 'build', '--runtime-image', runtime]
                if dataset is not None:
                    argv += ['--tau3-dataset', str(dataset)]
                self.assertEqual(main(argv), 0)
                build.assert_called_once_with(runtime, 'hbench/tau3', dataset, execute=False)
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
