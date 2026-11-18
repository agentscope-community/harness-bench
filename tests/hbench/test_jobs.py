from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from hbench.jobs import expand, load_profile, render_job, validate_execution
from hbench.summary import aggregate


class JobTests(unittest.TestCase):
    def test_native_matrix_protocols_and_budgets(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"HB_ANTHROPIC_BASE_URL": "https://example.test/anthropic"}):
            root = Path(temp)
            profile = self.dataset(root)
            for names, model in ((["hermes", "openclaw"], "bailian-model-b"),
                                 (["claude-code", "hermes", "openclaw"], "anthropic-compatible")):
                job, plan = render_job([profile], [load_profile("harnesses", n) for n in names],
                                       load_profile("models", model), load_profile("clusters", "docker"), root)
                self.assertEqual(plan["planned_trials"], len(names) * 2)
                for agent in job["agents"]:
                    self.assertEqual(agent["kwargs"]["max_tokens"], 16384)
                    self.assertEqual(agent["kwargs"]["context_window_tokens"], 131072)
            with self.assertRaisesRegex(ValueError, "claude-code"):
                render_job([profile], [load_profile("harnesses", "claude-code")],
                           load_profile("models", "bailian-model-b"), load_profile("clusters", "docker"), root)

    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        run = patch('subprocess.run')
        self.run = run.start()
        self.addCleanup(run.stop)
        self.addCleanup(self.run.assert_not_called)

    def render(self, root, profile, *, model=None, harness=None, cluster=None):
        return render_job([profile], [harness or load_profile('harnesses', 'codex')],
                          model if model is not None else load_profile('models', 'example'),
                          cluster or load_profile('clusters', 'kubernetes'), root)

    def dataset(self, root):
        p = root / 'terminal-bench'
        p.mkdir()
        profile = load_profile('benchmarks', 'terminal-bench')
        (p / 'manifest.json').write_text(json.dumps({'profile': profile}))
        for name, gpus in [('a', 0), ('b', 1)]:
            task = p / name
            task.mkdir()
            (task / 'task.toml').write_text(f'[environment]\ngpus = {gpus}\n')
        return profile

    def test_render_matrix_and_keep_secrets_out(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'HB_MODEL_API_KEY': 'secret-do-not-write', 'HB_TAU_API_KEY': 'tau-secret'}):
            root = Path(temp)
            profile = self.dataset(root)
            job, plan = render_job([profile], [load_profile('harnesses', n) for n in ['codex', 'pi', 'agentscope-java']],
                load_profile('models', 'example'), load_profile('clusters', 'kubernetes'), root, attempts=2, exclude_gpu=True)
            self.assertEqual(plan['planned_trials'], 6)
            self.assertEqual(job['datasets'][0]['task_names'], ['a'])
            self.assertEqual(job['retry']['max_retries'], 0)
            raw = yaml.safe_dump(job) + json.dumps(plan)
            self.assertNotIn('secret-do-not-write', raw)
            self.assertNotIn('tau-secret', raw)
            self.assertIn('${HB_TAU_API_KEY:-}', raw)
            self.assertEqual(plan['model']['api_key_name'], 'HB_MODEL_API_KEY')
            with self.assertRaisesRegex(ValueError, 'example'):
                validate_execution(job)
            self.run.assert_not_called()

    def test_empty_matrix_rejected_before_profile_access(self):
        for benchmarks, harnesses in (([], []), ([], [{}]), ([{}], [])):
            with self.subTest(benchmarks=benchmarks, harnesses=harnesses):
                with self.assertRaisesRegex(ValueError, 'at least one benchmark and one harness'):
                    render_job(benchmarks, harnesses, {}, {}, Path('/not-prepared'))

    def test_docker_profile_renders_frozen_native_backend_without_cluster_settings(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            'HB_MODEL_API_KEY': 'model-secret', 'HB_TAU_API_KEY': 'tau-secret',
            'HB_RUNTIME_IMAGE': 'hbench/harness-runtime:codex-0.167.2-pi-1.5.2',
        }):
            root = Path(temp)
            profile = self.dataset(root)
            cluster = load_profile('clusters', 'docker')
            model = load_profile('models', 'example')
            harnesses = [load_profile('harnesses', n) for n in ('codex', 'pi', 'agentscope-java')]
            before = deepcopy((cluster, model, harnesses))
            job, plan = render_job([profile], harnesses, model, cluster, root,
                                   concurrency=2, attempts=2, exclude_gpu=True)
            self.assertEqual(job['environment']['import_path'], 'hbench.docker_environment:FrozenDockerEnvironment')
            self.assertEqual(job['environment']['kwargs'], {
                'harness_runtime_image': 'hbench/harness-runtime:codex-0.167.2-pi-1.5.2',
                'image_map': {}, 'auto_mounting': True, 'keep_containers': False,
            })
            self.assertEqual(job['n_concurrent_trials'], 2)
            self.assertEqual(plan['planned_trials'], 6)
            self.assertEqual(plan['backend'], 'docker')
            self.assertEqual(plan['execution'], 'not_executed')
            self.assertEqual([a['kwargs']['version'] for a in job['agents']], ['0.167.2', '1.5.2', '1.6.0'])
            raw = yaml.safe_dump(job) + json.dumps(plan)
            self.assertNotIn('model-secret', raw)
            self.assertNotIn('tau-secret', raw)
            self.assertEqual((cluster, model, harnesses), before)

    def test_existing_cluster_profiles_remain_kubernetes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = self.dataset(root)
            for name in ('kubernetes', 'ack', 'acs'):
                with self.subTest(name=name):
                    job, plan = self.render(root, profile, cluster=load_profile('clusters', name))
                    self.assertEqual(job['environment']['import_path'], 'hbench.environment:KubernetesEnvironment')
                    self.assertEqual(plan['backend'], 'kubernetes')

    def test_unknown_backend_and_mixed_docker_cluster_settings_fail_before_expansion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = self.dataset(root)
            for backend in ('ecs', '', None, ['docker']):
                with self.subTest(backend=backend):
                    cluster = load_profile('clusters', 'docker')
                    cluster['backend'] = backend
                    with patch('hbench.jobs.expand', side_effect=AssertionError('unexpected expansion')):
                        with self.assertRaisesRegex(ValueError, 'Unsupported execution backend'):
                            self.render(root, profile, cluster=cluster)
            cluster = load_profile('clusters', 'docker')
            cluster['kwargs']['pod_overrides'] = {'spec': {}}
            with self.assertRaisesRegex(ValueError, 'Kubernetes-only'):
                self.render(root, profile, cluster=cluster)

    def test_docker_image_mapping_and_runtime_execution_guard(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'HB_MODEL_API_KEY': 'secret'}):
            root = Path(temp)
            profile = self.dataset(root)
            model = load_profile('models', 'example')
            model.update(name='actual-model', base_url='https://model.invalid/v1')
            mapping = {'hbench/tau3-main:0.1.0': 'local/tau3-main:1'}
            job, _ = render_job([profile], [load_profile('harnesses', 'codex')], model,
                                load_profile('clusters', 'docker'), root, image_map=mapping)
            self.assertEqual(job['environment']['kwargs']['image_map'], mapping)
            validate_execution(job)
            for image in ('', 'example.invalid/harness-runtime:1'):
                with self.subTest(image=image):
                    job['environment']['kwargs']['harness_runtime_image'] = image
                    with self.assertRaisesRegex(ValueError, 'harness_runtime_image'):
                        validate_execution(job)

    def test_sensitive_env_references_preserved_in_raw_serialization(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PRIVATE_KEY': 'never-serialize-this'}):
            root = Path(temp)
            profile = self.dataset(root)
            for section in ('environment_env', 'verifier_env'):
                for value in ('${PRIVATE_KEY}', '${PRIVATE_KEY:-}', ''):
                    with self.subTest(section=section, value=value):
                        model = load_profile('models', 'example')
                        model[section]['SERVICE_TOKEN'] = value
                        job, plan = self.render(root, profile, model=model)
                        target = job['environment']['env'] if section == 'environment_env' else job['verifier']['env']
                        self.assertEqual(target['SERVICE_TOKEN'], value)
                        self.assertNotIn('never-serialize-this', yaml.safe_dump(job) + json.dumps(plan))

    def test_sensitive_env_literals_defaults_and_overridden_literals_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = self.dataset(root)
            for section in ('environment_env', 'verifier_env'):
                for value in ('literal-credential', '${PRIVATE_KEY:-literal-credential}',
                              'Bearer ${PRIVATE_KEY}', '${PRIVATE_KEY}${OTHER_KEY}', 0, False, None):
                    with self.subTest(section=section, value=value):
                        model = load_profile('models', 'example')
                        model[section]['SERVICE_TOKEN'] = value
                        with self.assertRaisesRegex(ValueError, 'Sensitive env') as error:
                            self.render(root, profile, model=model)
                        self.assertNotIn('literal-credential', str(error.exception))
            model = load_profile('models', 'example')
            model['environment_env']['SERVICE_TOKEN'] = 'literal-credential'
            model['verifier_env']['SERVICE_TOKEN'] = '${PRIVATE_KEY:-}'
            with self.assertRaisesRegex(ValueError, 'Sensitive env'):
                self.render(root, profile, model=model)
            for section in ('environment_env', 'verifier_env'):
                model = load_profile('models', 'example')
                model[section]['OPAQUE'] = '${PRIVATE_KEY:-literal-credential}'
                with self.subTest(section=section), self.assertRaisesRegex(ValueError, 'Sensitive env'):
                    self.render(root, profile, model=model)

    def test_nonsecret_env_templates_and_verifier_overrides_kept(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = self.dataset(root)
            model = load_profile('models', 'example')
            model['environment_env'].update(SERVICE_TOKEN='${PRIVATE_KEY}', MODE='${MODE:-normal}')
            model['verifier_env'].update(SERVICE_TOKEN='${VERIFIER_KEY:-}', MODE='test')
            job, _ = self.render(root, profile, model=model)
            self.assertEqual(job['environment']['env']['SERVICE_TOKEN'], '${PRIVATE_KEY}')
            self.assertEqual(job['verifier']['env']['SERVICE_TOKEN'], '${VERIFIER_KEY:-}')
            self.assertEqual(job['environment']['env']['MODE'], '${MODE:-normal}')
            self.assertEqual(job['verifier']['env']['MODE'], 'test')

    def test_unknown_model_settings_rejected_before_expansion(self):
        extras = {'api_key': 'literal-credential', 'env': {'PRIVATE_KEY': 'literal-credential'},
                  'access_token': '${PRIVATE_KEY}', 'metadata': {'nested': [{'password': 'literal-credential'}]},
                  'extra_body': {'opaque': 'literal-credential'}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = self.dataset(root)
            for key, value in extras.items():
                with self.subTest(key=key):
                    model = load_profile('models', 'example')
                    model[key] = value
                    with patch('hbench.jobs.expand', side_effect=AssertionError('unsafe expansion')):
                        with self.assertRaises(ValueError) as error:
                            self.render(root, profile, model=model)
                    self.assertNotIn('literal-credential', str(error.exception))

    def test_nonsecret_fields_reject_nested_credentials_and_secret_expansion(self):
        cases = [
            ('model', ('base_url',), '${PRIVATE_KEY}'),
            ('model', ('name',), '${PRIVATE_KEY:-literal-credential}'),
            ('model', ('api_formats', 'Authorization'), 'literal-credential'),
            ('model', ('api_key_name',), '${PRIVATE_KEY}'),
            ('harness', ('kwargs', 'model_api_key_name'), '${PRIVATE_KEY}'),
            ('harness', ('kwargs', 'extra_body'), {'options': [{'password': 'literal-credential'}]}),
            ('harness', ('kwargs', 'headers'), {'Cookie': 'literal-credential'}),
            ('harness', ('kwargs', 'version'), '${PRIVATE_KEY}'),
            ('cluster', ('kwargs', 'namespace'), '${PRIVATE_KEY}'),
            ('cluster', ('kwargs', 'pod_overrides'), {'spec': {'env': {'OPAQUE': 'literal-credential'}}}),
            ('cluster', ('kwargs', 'pod_overrides'), {'metadata': {'annotations': {'auth_token': 'literal-credential'}}}),
            ('benchmark', ('metadata',), {'credentials': 'literal-credential'}),
        ]
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PRIVATE_KEY': 'expanded-credential'}):
            root = Path(temp)
            original = self.dataset(root)
            for kind, keys, value in cases:
                with self.subTest(kind=kind, keys=keys):
                    profiles = {'model': load_profile('models', 'example'), 'harness': load_profile('harnesses', 'codex'),
                                'cluster': load_profile('clusters', 'kubernetes'), 'benchmark': deepcopy(original)}
                    target = profiles[kind]
                    for key in keys[:-1]:
                        target = target.setdefault(key, {})
                    target[keys[-1]] = value
                    with patch('hbench.jobs.expand', side_effect=AssertionError('unsafe expansion')):
                        with self.assertRaises(ValueError) as error:
                            self.render(root, profiles['benchmark'], model=profiles['model'],
                                        harness=profiles['harness'], cluster=profiles['cluster'])
                    self.assertNotIn('literal-credential', str(error.exception))
                    self.assertNotIn('expanded-credential', str(error.exception))

    def test_nonsecret_references_token_limits_and_profile_inputs_preserved(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'HB_IMAGE_PULL_SECRET': 'pull-secret-name'}):
            root = Path(temp)
            profile = self.dataset(root)
            model = load_profile('models', 'example')
            harness = load_profile('harnesses', 'agentscope-java')
            harness['kwargs']['model_api_key_name'] = 'HB_MODEL_API_KEY'
            cluster = load_profile('clusters', 'ack')
            before = deepcopy((profile, model, harness, cluster))
            job, _ = self.render(root, profile, model=model, harness=harness, cluster=cluster)
            self.assertEqual(job['agents'][0]['kwargs']['max_tokens'], 16384)
            self.assertEqual(job['agents'][0]['kwargs']['model_api_key_name'], 'HB_MODEL_API_KEY')
            self.assertEqual(job['environment']['kwargs']['image_pull_secret'], 'pull-secret-name')
            self.assertFalse(job['environment']['kwargs']['pod_overrides']['spec']['automountServiceAccountToken'])
            self.assertEqual((profile, model, harness, cluster), before)

    def test_multiple_models_expand_models_by_harnesses(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
                'HB_MODEL_A_ID': 'qwen-a', 'HB_MODEL_B_ID': 'qwen-b', 'HB_BAILIAN_API_KEY': 'bailian-secret'}):
            root = Path(temp)
            profile = self.dataset(root)
            models = [load_profile('models', n) for n in ('bailian-model-a', 'bailian-model-b')]
            harnesses = [load_profile('harnesses', n) for n in ('codex', 'pi', 'agentscope-java')]
            job, plan = render_job([profile], harnesses, models, load_profile('clusters', 'docker'), root, concurrency=4)
            self.assertEqual([(a['model_name'], a['import_path'].split(':')[1]) for a in job['agents']], [
                ('qwen-a', 'FrozenCodex'), ('qwen-a', 'FrozenPi'), ('qwen-a', 'AgentScopeJava'),
                ('qwen-b', 'FrozenCodex'), ('qwen-b', 'FrozenPi'), ('qwen-b', 'AgentScopeJava')])
            self.assertEqual({a['model_api_key_name'] for a in job['agents']}, {'HB_BAILIAN_API_KEY'})
            self.assertEqual(plan['planned_trials'], 2 * 2 * 3)
            self.assertEqual(len(plan['combinations']), 6)
            self.assertEqual(plan['concurrency'], 4)
            self.assertEqual(job['n_concurrent_trials'], 4)
            self.assertNotIn('bailian-secret', yaml.safe_dump(job) + json.dumps(plan))
            other = deepcopy(models[1])
            other['environment_env']['TAU2_USER_MODEL'] = 'different'
            with self.assertRaisesRegex(ValueError, 'share environment_env'):
                render_job([profile], harnesses, [models[0], other], load_profile('clusters', 'docker'), root)
            with self.assertRaisesRegex(ValueError, 'distinct'):
                render_job([profile], harnesses, [models[0], models[0]], load_profile('clusters', 'docker'), root)
            with self.assertRaisesRegex(ValueError, 'at least one model'):
                render_job([profile], harnesses, [], load_profile('clusters', 'docker'), root)

    def test_wrong_api_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = self.dataset(Path(temp))
            model = load_profile('models', 'example')
            model['api_formats']['codex'] = 'openai_chat_completions'
            with self.assertRaisesRegex(ValueError, 'does not support'):
                render_job([profile], [load_profile('harnesses', 'codex')], model,
                           load_profile('clusters', 'ack'), Path(temp))

    def test_source_identity_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            p = self.dataset(root)
            p['version'] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'identity'):
                render_job([p], [load_profile('harnesses', 'pi')], load_profile('models', 'example'), load_profile('clusters', 'acs'), root)

    def test_expand_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(expand('${MISSING:-default}'), 'default')
            with self.assertRaises(ValueError):
                expand('${MISSING}')

    def test_summary_errors_are_not_passes(self):
        def row(n, reward, error=None):
            return {'id': n, 'agent_info': {'name': 'pi'}, 'source': 'benchmark',
                    'verifier_result': {'rewards': {'reward': reward}}, 'exception_info': error}
        result = aggregate([row('a', 1), row('b', 1, {'exception_type': 'Broken'}), row('c', None)], 4)
        self.assertEqual(result['missing_trials'], 1)
        group = result['groups'][0]
        self.assertEqual(group['passed'], 1)
        self.assertEqual(group['scored'], 1)
        self.assertEqual(group['errors'], 1)
        self.assertEqual(group['unscored'], 2)
        self.assertEqual(group['pass_rate_recorded'], 1 / 3)
        with self.assertRaises(ValueError):
            aggregate([row('a', 1), row('a', 0)])
