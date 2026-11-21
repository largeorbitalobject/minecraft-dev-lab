import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from mcdevlab.bridges import PROTOCOL, inspect_bridge, invoke_bridge


PROVIDER = r'''
import json, pathlib, subprocess, sys, time
q = json.loads(sys.stdin.buffer.readline())
p = pathlib.Path('calls.txt')
with p.open('a') as f: f.write(q['request_id'] + '\n')
mode = sys.argv[1]
if mode == 'timeout': time.sleep(3)
if mode == 'child':
    subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(10)'])
    raise SystemExit(0)
if mode == 'stdout_overflow':
    sys.stdout.write('x' * 100000);sys.stdout.flush();time.sleep(3)
if mode == 'stderr_overflow':
    sys.stderr.write('private-token-' * 2000);sys.stderr.flush();time.sleep(3)
if mode == 'exit': raise SystemExit(2)
if mode == 'malformed': print('SECRET-DO-NOT-RETURN');raise SystemExit(0)
now = time.time_ns() // 1000000
frames = pathlib.Path('frames.txt')
frame = int(frames.read_text()) if frames.exists() else 10
w = dict(session_id='session-A', world_id='world-A', frame_id=frame, observed_at_ms=now)
after = dict(w, frame_id=frame + 1)
frames.write_text(str(frame + 1))
r = dict(protocol=q['protocol'], provider_id=q['provider_id'], request_id=q['request_id'],
         operation=q['operation'], status='ok', code='OBSERVED', before=w, after=after,
         result={'map_open': q['operation'] == 'open_map'})
if mode == 'identity': r['request_id'] = 'wrong'
if mode == 'world': r['after']['world_id'] = 'world-B'
if mode == 'session': r['after']['session_id'] = 'session-B'
if mode == 'frame': r['after']['frame_id'] = r['before']['frame_id']
if mode == 'stale': r['after']['observed_at_ms'] = now - 100000
if mode == 'no_witness': r['before'] = None
if mode == 'menu': r['before']['world_id'] = r['after']['world_id'] = None
if mode == 'rejected': r['status'] = 'rejected';r['code'] = 'PRECONDITION_REFUSED'
if mode == 'changed_pin': pathlib.Path(__file__).write_text('# changed during dispatch')
if mode == 'duplicate':
    print(json.dumps(r)[:-1] + ',"status":"ok"}');raise SystemExit(0)
if mode == 'nonfinite':
    r['result']['value'] = float('nan')
print(json.dumps(r))
'''


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = self.root / 'provider.py'
        self.provider.write_text(PROVIDER)
        executable = str(Path(sys.executable).resolve())
        self.config = {
            'kind': 'json-stdio', 'argv': [executable, str(self.provider), 'normal'],
            'cwd': str(self.root), 'platforms': ['linux', 'macos', 'windows'],
            'pins': [{'path': p, 'sha256': hashlib.sha256(Path(p).read_bytes()).hexdigest()}
                     for p in (executable, str(self.provider))],
            'manifest': {'protocol': PROTOCOL, 'provider_id': 'fixture/1',
                         'operations': {'status': {'effect': 'observe'}, 'open_map': {'effect': 'action'}}},
            'timeout_seconds': 1, 'output_limit_bytes': 4096,
        }

    def request(self, operation='open_map'):
        value = {'request_id': str(uuid.uuid4()), 'operation': operation, 'arguments': {}}
        if operation != 'status':
            value['expected'] = {'session_id': 'session-A', 'world_id': 'world-A', 'frame_id': 9,
                                 'observed_at_ms': time.time_ns() // 1000000}
        return value

    def run_mode(self, mode, operation='open_map'):
        self.config['argv'][2] = mode
        return invoke_bridge(self.config, self.request(operation))

    def test_inspect_never_executes(self):
        with patch('mcdevlab.bridges.subprocess.Popen', side_effect=AssertionError('must not spawn')):
            result = inspect_bridge(self.config)
        self.assertTrue(result['available'])
        self.assertFalse(result['live_verified'])
        self.assertFalse((self.root / 'calls.txt').exists())

    def test_pin_mismatch_and_unpinned_script_refuse(self):
        self.config['pins'][1]['sha256'] = '0' * 64
        self.assertEqual(inspect_bridge(self.config)['reason'], 'PROVIDER_PIN_MISMATCH')
        self.config['pins'].pop()
        self.assertEqual(inspect_bridge(self.config)['reason'], 'ARGUMENT_FILE_PIN_REQUIRED')

    def test_explicit_unsupported_atlas_and_platform(self):
        self.assertEqual(inspect_bridge({'kind': 'atlas-controller'})['reason'], 'ATLAS_CONTROLLER_ADAPTER_NOT_IMPLEMENTED')
        self.config['platforms'] = ['windows'] if sys.platform != 'win32' else ['linux']
        self.assertEqual(inspect_bridge(self.config)['reason'], 'UNSUPPORTED_PLATFORM')

    def test_invalid_config_bounds(self):
        for field, value, code in [('timeout_seconds', 100, 'INVALID_PROVIDER_TIMEOUT'),
                                   ('timeout_seconds', float('nan'), 'INVALID_PROVIDER_TIMEOUT'),
                                   ('output_limit_bytes', 999999, 'INVALID_OUTPUT_LIMIT'),
                                   ('freshness_ms', True, 'INVALID_FRESHNESS')]:
            with self.subTest(field=field, value=value):
                c = copy.deepcopy(self.config);c[field] = value
                self.assertEqual(inspect_bridge(c)['reason'], code)

    def test_request_validation_never_dispatches(self):
        cases = [({'operation': 'shell'}, 'UNSUPPORTED_OPERATION'),
                 ({'request_id': 'not-a-uuid'}, 'INVALID_BRIDGE_REQUEST'),
                 ({'argv': ['/bin/sh']}, 'INVALID_REQUEST_FIELDS'),
                 ({'arguments': []}, 'ARGUMENTS_OBJECT_REQUIRED'),
                 ({'expected': None}, 'WITNESS_REQUIRED')]
        for change, code in cases:
            with self.subTest(code=code):
                request = self.request();request.update(change)
                result = invoke_bridge(self.config, request)
                self.assertFalse(result['dispatched'])
                self.assertEqual(result['code'], code)
        self.assertFalse((self.root / 'calls.txt').exists())

    def test_stale_expected_refuses_before_dispatch(self):
        request = self.request();request['expected']['observed_at_ms'] -= 60000
        result = invoke_bridge(self.config, request)
        self.assertFalse(result['dispatched'])
        self.assertEqual(result['code'], 'STALE_WITNESS')

    def test_real_provider_status_then_action(self):
        status = invoke_bridge(self.config, self.request('status'))
        self.assertEqual(status['outcome'], 'observed')
        self.assertEqual(status['after']['world_id'], 'world-A')
        request = self.request();request['expected'] = dict(status['after'])
        result = invoke_bridge(self.config, request)
        self.assertEqual(result['outcome'], 'verified')
        self.assertTrue(result['result']['map_open'])
        self.assertEqual((self.root / 'calls.txt').read_text().splitlines(), [status['request_id'], request['request_id']])

    def test_failed_receipt_is_unknown_without_replay(self):
        modes = {'identity': 'RESPONSE_IDENTITY_MISMATCH', 'world': 'SESSION_OR_WORLD_CHANGED',
                 'session': 'SESSION_OR_WORLD_CHANGED', 'frame': 'COMPLETED_FRAME_REQUIRED',
                 'stale': 'STALE_WITNESS', 'no_witness': 'WITNESS_REQUIRED',
                 'malformed': 'MALFORMED_PROVIDER_RESPONSE', 'duplicate': 'DUPLICATE_JSON_KEY',
                 'nonfinite': 'NONFINITE_JSON', 'exit': 'PROVIDER_EXIT_FAILED'}
        for mode, code in modes.items():
            with self.subTest(mode=mode):
                result = self.run_mode(mode)
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['code'], code)
                self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(len((self.root / 'calls.txt').read_text().splitlines()), len(modes))

    def test_refused_action_has_explicit_witnesses(self):
        result = self.run_mode('rejected')
        self.assertEqual(result['outcome'], 'refused')
        self.assertEqual(result['status'], 'rejected')

    def test_menu_observation_possible_but_world_action_refused(self):
        self.assertEqual(self.run_mode('menu', 'status')['outcome'], 'observed')
        self.assertEqual(self.run_mode('menu')['code'], 'WORLD_WITNESS_REQUIRED')

    def test_execution_timeout_output_bounds_and_child_cleanup(self):
        self.config['timeout_seconds'] = 0.5
        for mode, code in [('timeout', 'PROVIDER_TIMEOUT'), ('child', 'PROVIDER_TIMEOUT'),
                           ('stdout_overflow', 'PROVIDER_OUTPUT_LIMIT'), ('stderr_overflow', 'PROVIDER_OUTPUT_LIMIT')]:
            with self.subTest(mode=mode):
                started = time.monotonic()
                result = self.run_mode(mode)
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['code'], code)
                self.assertLess(time.monotonic() - started, 3)
                self.assertNotIn('private-token', json.dumps(result))

    def test_provider_replacement_during_dispatch_is_unknown(self):
        result = self.run_mode('changed_pin')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['code'], 'PROVIDER_PIN_MISMATCH')

    def test_expected_session_and_frame_are_concrete(self):
        for key, value, code in [('session_id', '', 'SESSION_WITNESS_REQUIRED'),
                                 ('world_id', None, 'WORLD_WITNESS_REQUIRED'),
                                 ('frame_id', True, 'FRAME_WITNESS_REQUIRED')]:
            with self.subTest(key=key):
                request = self.request();request['expected'][key] = value
                result = invoke_bridge(self.config, request)
                self.assertFalse(result['dispatched'])
                self.assertEqual(result['code'], code)


if __name__ == '__main__':
    unittest.main()
