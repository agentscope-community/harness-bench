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
"""Real native Harness CLI against a loopback streaming provider; no external model."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.native_events import convert


@unittest.skipUnless(os.environ.get("HARNESS_EVAL_CLASSPATH"), "Build the Java adapter first")
class NativeRunnerTest(unittest.TestCase):
    def test_two_inputs_keep_one_real_harness_session_and_export_native_events(self):
        requests = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                chunks = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "fake answer"}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
                ]
                data = "".join("data: " + json.dumps({"id": "fake", "object": "chat.completion.chunk", "created": 1,
                    "model": "fake", **chunk}) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                body = data.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                request = folder / "request.json"
                request.write_text(json.dumps({"events": [{"type": "user.message", "content": [{"type": "text", "text": marker}]}
                    for marker in ("first input marker", "second input marker")]}))
                env = {k: v for k, v in os.environ.items() if k not in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY")}
                env.update(DASHSCOPE_API_KEY="fake-local-only", MODEL_ID="fake", MODEL_BASE_URL=f"http://127.0.0.1:{provider.server_port}/v1")
                result = subprocess.run(["java", "-cp", os.environ["HARNESS_EVAL_CLASSPATH"],
                    "io.agentscope.examples.eval.HarnessEvalCli", str(request), str(folder / "native.jsonl"), str(folder / "workspace")],
                    env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                # Production defaults flush memory after each turn. These calls are
                # part of the real Harness cost and must remain visible in evidence.
                self.assertEqual(len(requests), 4)
                main_requests = [r for r in requests if r.get("tools")]
                flush_requests = [r for r in requests if not r.get("tools")]
                self.assertEqual(len(main_requests), 2)
                self.assertEqual(len(flush_requests), 2)
                for flush in flush_requests:
                    self.assertIn("Extract NEW memories", json.dumps(flush["messages"]))
                for marker in ("first input marker", "second input marker", "fake answer"):
                    self.assertIn(marker, json.dumps(main_requests[1]["messages"]))
                self.assertTrue(requests[0]["tools"], "Native workspace tools must be available")
                binding = convert("agentscope", [folder / "native.jsonl"], folder / "protocol")
                self.assertTrue(binding["native_completed"])
                self.assertEqual(binding["summary"]["event_counts"]["user.message"], 2)
                self.assertEqual(binding["summary"]["event_counts"]["agent.message"], 2)
                native = [json.loads(line) for line in (folder / "native.jsonl").read_text().splitlines()]
                self.assertEqual(sum(e["type"] == "eval.model_start" for e in native), 4)
                self.assertEqual(sum(e["type"] == "eval.model_end" for e in native), 4)
        finally:
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=5)

    def test_native_file_edit_changes_the_actual_harbor_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            project = folder / 'workspace'
            project.mkdir()
            target = project / 'data.txt'
            target.write_text('original\n')
            main_calls = []

            class Provider(BaseHTTPRequestHandler):
                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if request.get('tools'):
                        main_calls.append(request)
                    if request.get('tools') and len(main_calls) == 1:
                        delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'edit-fixture', 'type': 'function',
                            'function': {'name': 'edit_file', 'arguments': json.dumps({'path': str(target),
                                'old_string': 'original', 'new_string': 'changed'})}}]}
                        reason = 'tool_calls'
                    else:
                        delta, reason = {'role': 'assistant', 'content': 'done'}, 'stop'
                    chunks = [{'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                              {'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}],
                               'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}]
                    body = (''.join('data: ' + json.dumps({'id': 'fake-edit', 'object': 'chat.completion.chunk',
                        'created': 1, 'model': 'fake', **chunk}) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
            thread = threading.Thread(target=provider.serve_forever, daemon=True)
            thread.start()
            try:
                request = folder / 'request.json'
                request.write_text(json.dumps({'events': [{'type': 'user.message', 'content': [
                    {'type': 'text', 'text': 'Edit data.txt: replace original with changed.'}]}]}))
                env = {k: v for k, v in os.environ.items() if k not in ('DASHSCOPE_API_KEY', 'OPENAI_API_KEY')}
                env.update(DASHSCOPE_API_KEY='fake-local-only', MODEL_ID='fake', MODEL_BASE_URL=f'http://127.0.0.1:{provider.server_port}/v1')
                result = subprocess.run(['java', '-cp', os.environ['HARNESS_EVAL_CLASSPATH'],
                    'io.agentscope.examples.eval.HarnessEvalCli', str(request), str(folder / 'native.jsonl'), str(project)],
                    env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertEqual(target.read_text(), 'changed\n', 'Changes must reach the actual verifier project, not only a session overlay')
                self.assertEqual(len(main_calls), 2)
                binding = convert('agentscope', [folder / 'native.jsonl'], folder / 'protocol')
                self.assertEqual(binding['summary']['event_counts']['agent.tool_result'], 1)
                self.assertTrue(binding['native_completed'])
                self.assertTrue((folder / 'workspace-state').is_dir())
            finally:
                provider.shutdown()
                provider.server_close()
                thread.join(timeout=5)

    def test_mcp_clients_close_before_jvm_exit_on_setup_failures(self):
        deleted = []
        initialized = []

        class McpServer(BaseHTTPRequestHandler):
            def reply(self, status, body=b'', session=None):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                if session is not None:
                    self.send_header('Mcp-Session-Id', session)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.reply(405)

            def do_DELETE(self):
                deleted.append(self.headers['Mcp-Session-Id'])
                self.reply(200)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/v1/chat/completions':
                    body = ('data: ' + json.dumps({'id': 'cleanup', 'object': 'chat.completion.chunk',
                        'created': 1, 'model': 'fake', 'choices': [{'index': 0,
                            'delta': {'role': 'assistant', 'content': 'done'}, 'finish_reason': 'stop'}]})
                        + '\n\ndata: [DONE]\n\n').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if 'id' not in request:
                    self.reply(202)
                    return
                response = {'jsonrpc': '2.0', 'id': request['id']}
                if request['method'] == 'initialize':
                    initialized.append(self.path)
                    response['result'] = {
                        'protocolVersion': request['params']['protocolVersion'],
                        'capabilities': {'tools': {}},
                        'serverInfo': {'name': 'loopback-only', 'version': '1'},
                    }
                elif self.path == '/bad':
                    response['error'] = {'code': -32603, 'message': 'fixture registration failure'}
                else:
                    response['result'] = {'tools': []}
                self.reply(200, json.dumps(response).encode(), self.path)

            def log_message(self, *args):
                pass

        # Catch the CLI failure in Java and keep that JVM alive. A subprocess exit alone
        # would close sockets too, hiding the ownership regression.
        probe_source = '''
package io.agentscope.examples.eval;
public class CleanupProbe {
    public static void close(io.agentscope.harness.agent.HarnessAgent agent) {
        try {
            agent.close();
        } finally {
            throw new IllegalStateException("fixture close failure");
        }
    }
    public static void main(String[] args) throws Exception {
        String outcome = "ok";
        try {
            if (args[0].equals("toolkit")) {
                HarnessEvalCli.mcpToolkit(System.getenv("EVAL_MCP_SERVERS")).closeMcpClients();
            } else {
                HarnessEvalCli.main(java.util.Arrays.copyOfRange(args, 1, args.length));
            }
        } catch (Throwable error) {
            boolean inBuild = java.util.Arrays.stream(error.getStackTrace()).anyMatch(
                frame -> frame.getClassName().equals("io.agentscope.harness.agent.HarnessAgent$Builder")
                    && frame.getMethodName().equals("build"));
            outcome = error.getClass().getSimpleName() + ":" + inBuild;
        }
        System.out.println(outcome);
        System.out.flush();
        System.in.read();
    }
}
'''
        server = ThreadingHTTPServer(('127.0.0.1', 0), McpServer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'CleanupProbe.java'
                source.write_text(probe_source)
                classpath = os.environ['HARNESS_EVAL_CLASSPATH']
                compiled = subprocess.run(['javac', '-cp', classpath, '-d', str(root), str(source)],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                # Inject an abrupt close at the adapter's call site in a temporary copy,
                # without production test hooks or changes to the cached native agent.
                fault_dir = root / 'close-fault'
                fault_dir.mkdir()
                cli_source = ROOT / ('adapters/agentscope-java/src/main/java/'
                    'io/agentscope/examples/eval/HarnessEvalCli.java')
                cli_text = cli_source.read_text()
                self.assertEqual(cli_text.count('agent.close();'), 1)
                fault_source = fault_dir / 'HarnessEvalCli.java'
                fault_source.write_text(cli_text.replace('agent.close();', 'CleanupProbe.close(agent);'))
                compiled = subprocess.run(['javac', '-cp', str(root) + os.pathsep + classpath,
                    '-d', str(fault_dir), str(fault_source)], capture_output=True, text=True, timeout=30)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                good = {'name': 'good', 'transport': 'streamable-http',
                    'url': f'http://127.0.0.1:{server.server_port}/good'}
                bad = {**good, 'name': 'bad', 'url': f'http://127.0.0.1:{server.server_port}/bad'}
                cases = [
                    ('writer-open', [good], 'FileSystemException:false', ['/good']),
                    ('builder-build', [good], 'IllegalArgumentException:true', ['/good']),
                    ('agent-close', [good], 'IllegalStateException:false', ['/good']),
                    ('later-parse', [good, {'name': 'bad', 'transport': 'stdio',
                        'command': 'unused', 'args': {}}], 'IllegalArgumentException:false', ['/good']),
                    ('later-build', [good, {**bad, 'url': 'http://['}],
                        'IllegalArgumentException:false', ['/good']),
                    ('later-register', [good, bad], 'McpError:false', ['/good', '/bad']),
                    ('malformed-json', '[', 'JsonEOFException:false', []),
                    ('success', [good], 'ok', ['/good']),
                ]
                for case, config, expected, sessions in cases:
                    with self.subTest(case=case):
                        deleted.clear()
                        initialized.clear()
                        folder = root / case
                        folder.mkdir()
                        request = folder / 'request.json'
                        request.write_text(json.dumps({'events': [{'type': 'user.message',
                            'content': [{'type': 'text', 'text': 'unused'}]}]}))
                        output = folder if case == 'writer-open' else folder / 'native.jsonl'
                        env = {key: value for key, value in os.environ.items()
                            if not key.startswith('EVAL_') and key not in
                            ('DASHSCOPE_API_KEY', 'OPENAI_API_KEY', 'MODEL_API_KEY', 'MAX_TURNS')}
                        env.update(MODEL_API_KEY='fake-local-only', MODEL_ID='fake',
                            MODEL_BASE_URL=f'http://127.0.0.1:{server.server_port}/v1',
                            EVAL_MCP_SERVERS=config if isinstance(config, str) else json.dumps(config),
                            EVAL_STATE_DIR=str(folder / 'state'))
                        if case == 'builder-build':
                            env['MAX_TURNS'] = '0'
                        mode = 'main' if case in ('writer-open', 'builder-build', 'agent-close') else 'toolkit'
                        probe_classpath = str(root) + os.pathsep + classpath
                        if case == 'agent-close':
                            probe_classpath = str(fault_dir) + os.pathsep + probe_classpath
                        process = subprocess.Popen(['java', '-cp', probe_classpath,
                            'io.agentscope.examples.eval.CleanupProbe', mode, str(request),
                            str(output), str(folder / 'workspace')], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
                        outcomes = queue.Queue()
                        reader = threading.Thread(target=lambda: outcomes.put(process.stdout.readline()),
                            daemon=True)
                        reader.start()
                        try:
                            self.assertEqual(outcomes.get(timeout=60).strip(), expected)
                            self.assertIsNone(process.poll(), 'JVM must still be alive during assertions')
                            self.assertCountEqual(initialized, sessions)
                            self.assertCountEqual(deleted, sessions,
                                'Every initialized client must close, including unregistered clients')
                            if case == 'agent-close':
                                native = [json.loads(line) for line in output.read_text().splitlines()]
                                self.assertTrue(any(event['type'] == 'eval.completed' for event in native))
                                self.assertNotIn('fixture close failure', output.read_text())
                        finally:
                            try:
                                process.communicate(input='\n', timeout=15)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.communicate(timeout=5)
                                raise
                            finally:
                                reader.join(timeout=5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
