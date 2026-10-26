import tempfile
import copy
import json
import io
import urllib.error
import urllib.request
import threading
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch


def config(identity='1' * 32, address='192.168.40.10'):
    return dict(id=identity, key='a' * 64, name='test-node', address=address,
                port=80, networks=['192.168.40.0/24'])
import unittest

import lan_cluster as lc


class ConfigurationTests(unittest.TestCase):
    def test_upload_metadata_accepts_browser_encoded_names(self):
        lc._metadata('POST', '/api/audio/upload', 'application/octet-stream', 'Son%20%C3%A9t%C3%A9.wav')

    def test_absent_configuration_is_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            cluster = lc.LanCluster(root)
            cluster.start()
            self.assertEqual(cluster.snapshot(), {'enabled': False, 'self': {}, 'peers': [], 'error': ''})
            cluster.close()

    def test_valid_config_redacts_key_and_invalid_config_fails_closed(self):
        c = lc.LanCluster('.', config())
        self.assertTrue(c.snapshot()['enabled'])
        self.assertEqual(c.snapshot()['self']['id'], '1' * 32)
        self.assertNotIn('key', json.dumps(c.snapshot()))
        for field, value in [('id', 'x'), ('key', 'x'), ('address', '::1'),
                             ('address', 3232245770), ('networks', [3232245770]),
                             ('networks', []), ('networks', ['0.0.0.0/0']),
                             ('port', 0), ('name', 'bad\r\nname')]:
            bad = config()
            bad[field] = value
            with self.subTest(field=field, value=value):
                result = lc.LanCluster('.', bad).snapshot()
                self.assertFalse(result['enabled'])
                self.assertTrue(result['error'])

    def test_hex_configuration_preserves_identifier_case(self):
        cfg = config('A' * 32)
        cfg['key'] = 'A' * 64
        c = lc.LanCluster('.', cfg)
        self.assertTrue(c.snapshot()['enabled'])
        self.assertEqual(c.snapshot()['self']['id'], 'A' * 32)

    def test_route_and_size_allowlist(self):
        for path in ['/api/status', '/api/display', '/api/tv', '/api/audio',
                     '/api/audio/sounds', '/api/audio/key', '/api/audio/api']:
            self.assertTrue(lc.allowed_request('GET', path, 0))
        for path in ['/api/config', '/api/action', '/api/tv', '/api/display/settings',
                     '/api/audio/play', '/api/audio/play/bell-1.mp3', '/api/audio/stop',
                     '/api/audio/settings', '/api/audio/upload', '/api/audio/delete']:
            self.assertTrue(lc.allowed_request('POST', path, 65536))
        self.assertTrue(lc.allowed_request('POST', '/api/audio/upload', 20 * 1024 * 1024))
        for method, path, size in [('GET', '/api/status?q=x', 0), ('GET', '/api/cluster/relay', 0),
                                  ('POST', '/api/audio/play/../x', 0), ('POST', '/api/audio/play/%61', 0),
                                  ('POST', '/api/audio/play/a/b', 0), ('DELETE', '/api/tv', 0),
                                  ('POST', '/api/config', 65537), ('GET', '/api/status', -1),
                                  ('POST', '/api/audio/upload', 20 * 1024 * 1024 + 1)]:
            self.assertFalse(lc.allowed_request(method, path, size))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.a = lc.LanCluster('.', config())
        self.b = lc.LanCluster('.', config('2' * 32, '192.168.40.11'))
        self.clock = patch.object(lc.time, 'time', return_value=1000)
        self.mono = patch.object(lc.time, 'monotonic', return_value=100)
        self.clock.start()
        self.mono.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.mono.stop)

    def discover(self):
        self.assertTrue(self.a._receive_beacon(self.b._beacon(), '192.168.40.12'))
        return self.a.snapshot()['peers'][0]

    def test_authenticated_source_address_and_expiry(self):
        peer = self.discover()
        self.assertEqual(peer['address'], '192.168.40.12')
        self.assertTrue(peer['online'])
        with patch.object(lc.time, 'monotonic', return_value=116):
            self.assertFalse(self.a.snapshot()['peers'][0]['online'])
        with patch.object(lc.time, 'monotonic', return_value=161):
            self.assertEqual(self.a.snapshot()['peers'], [])

    def test_beacon_rejects_tampering_replay_network_self_and_port(self):
        beacon = self.b._beacon()
        self.assertFalse(self.a._receive_beacon(beacon, '198.51.100.1'))
        self.assertFalse(self.a._receive_beacon(beacon, 'garbage'))
        self.assertFalse(self.a._receive_beacon(b'x' * 4097, '192.168.40.12'))
        self.assertFalse(self.a._receive_beacon(b'{}', '192.168.40.12'))
        self.assertFalse(self.a._receive_beacon(self.a._beacon(), '192.168.40.12'))
        self.assertFalse(self.a._receive_beacon(beacon.replace(b'test-node', b'evil-node'), '192.168.40.12'))
        other = config('3' * 32)
        other['key'] = 'b' * 64
        self.assertFalse(self.a._receive_beacon(lc.LanCluster('.', other)._beacon(), '192.168.40.12'))
        other['key'], other['port'] = 'a' * 64, 8080
        self.assertFalse(self.a._receive_beacon(lc.LanCluster('.', other)._beacon(), '192.168.40.12'))
        with patch.object(lc.time, 'time', return_value=1031):
            self.assertFalse(self.a._receive_beacon(beacon, '192.168.40.12'))
        self.assertTrue(self.a._receive_beacon(beacon, '192.168.40.12'))
        self.assertFalse(self.a._receive_beacon(beacon, '192.168.40.12'))

    def test_untrusted_datagrams_cannot_crash_receiver(self):
        beacon = json.loads(self.b._beacon())
        beacon['time'] = 10 ** 1000
        for raw in [json.dumps(beacon).encode(), b'\xff', b'[' * 2000 + b']' * 2000,
                    b'null', b'[]', b'"text"']:
            with self.subTest(raw=raw[:30]), patch.object(lc.time, 'time', return_value=1000.0):
                self.assertFalse(self.a._receive_beacon(raw, '192.168.40.12'))

    def test_peer_cap_and_snapshot_copies(self):
        for i in range(2, 132):
            b = lc.LanCluster('.', config(format(i, '032x')))
            self.a._receive_beacon(b._beacon(), '192.168.40.12')
        peers = self.a.snapshot()['peers']
        self.assertEqual(len(peers), 128)
        peers[0]['address'] = 'bad'
        self.assertNotEqual(self.a.snapshot()['peers'][0]['address'], 'bad')


class RelayTests(unittest.TestCase):
    setUp = DiscoveryTests.setUp

    def pair(self):
        self.a._receive_beacon(self.b._beacon(), '192.168.40.11')
        self.b._receive_beacon(self.a._beacon(), '192.168.40.10')

    def capture(self, method='GET', path='/api/status', body=b'', **kwargs):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok":true}'
        response.status = 200
        response.headers = {'Content-Type': 'application/json'}
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value = response
            result = self.a.request('2' * 32, method, path, body, **kwargs)
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 25)
            handlers = build.call_args.args
            self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers))
            self.assertTrue(any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in handlers))
        self.assertEqual(req.full_url, 'http://192.168.40.11:80/api/cluster/relay')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.data, body)
        self.assertEqual(result, (200, b'{"ok":true}', 'application/json'))
        headers = dict(req.header_items())
        self.assertFalse(any(h.lower() in ('authorization', 'x-kiosk-token', 'x-audio-key') for h in headers))
        return headers

    def test_request_roundtrip_upload_and_case_insensitive_headers(self):
        self.pair()
        body = b'raw\x00audio\xff'
        headers = self.capture('POST', '/api/audio/upload', body,
                               content_type='audio/mpeg', filename='bell.mp3')
        result = self.b.verify({k.swapcase(): v for k, v in headers.items()}, body)
        self.assertEqual(result, ('POST', '/api/audio/upload', 'audio/mpeg', 'bell.mp3'))
        with self.assertRaises(PermissionError):
            self.b.verify(headers, body)

    def test_signed_fields_tampering_target_and_clock(self):
        self.pair()
        headers = {k.lower(): v for k, v in self.capture().items()}
        for key, value in [('x-kiosk-peer', '3' * 32), ('x-kiosk-target', '1' * 32),
                           ('x-kiosk-time', '1001'), ('x-kiosk-nonce', 'f' * 32),
                           ('x-kiosk-method', 'POST'), ('x-kiosk-path', '/api/display'),
                           ('x-kiosk-type', 'text/plain'), ('x-kiosk-filename', 'evil.mp3'),
                           ('x-kiosk-signature', '0' * 64)]:
            with self.subTest(key=key), self.assertRaises(PermissionError):
                self.b.verify({**headers, key: value}, b'')
        with self.assertRaises(PermissionError):
            self.b.verify(headers, b'changed')
        with patch.object(lc.time, 'time', return_value=1031), self.assertRaises(PermissionError):
            self.b.verify(headers, b'')
        self.assertEqual(self.b.verify(headers, b'')[1], '/api/status')

    def test_unknown_offline_and_malformed_requests(self):
        with self.assertRaises(RuntimeError):
            self.a.request('2' * 32, 'GET', '/api/status')
        self.pair()
        headers = self.capture()
        with patch.object(lc.time, 'monotonic', return_value=116):
            with self.assertRaises(RuntimeError):
                self.a.request('2' * 32, 'GET', '/api/status')
            with self.assertRaises(PermissionError):
                self.b.verify(headers, b'')
        for path in ['/api/cluster/relay', '/api/peers', '/api/status?x=1', '/api/audio/play/../x']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.a.request('2' * 32, 'GET', path)
        for kwargs in [dict(content_type='a\r\nb'), dict(filename='../bad.mp3'), dict(filename='a\nb')]:
            with self.assertRaises(ValueError):
                self.a.request('2' * 32, 'POST', '/api/audio/upload', **kwargs)
        with self.assertRaises(ValueError):
            self.b.verify({}, b'')
        with self.assertRaises(ValueError):
            self.b.verify({**headers, 'X-Kiosk-Peer': '0' * 32}, b'')

    def test_nonce_cache_fails_closed_and_expires(self):
        self.pair()
        headers = self.capture()
        for i in range(4096):
            self.assertTrue(self.b._fresh_nonce(self.b._relay_nonces, '1' * 32, format(i, '032x')))
        with self.assertRaises(PermissionError):
            self.b.verify(headers, b'')
        self.assertEqual(len(self.b._relay_nonces), 4096)
        self.assertFalse(self.b._fresh_nonce(self.b._relay_nonces, '1' * 32, '0' * 32))
        with patch.object(lc.time, 'monotonic', return_value=162):
            self.b._receive_beacon(self.a._beacon(), '192.168.40.10')
            self.assertEqual(self.b.verify(headers, b'')[1], '/api/status')
            self.assertEqual(len(self.b._relay_nonces), 1)

    def test_validly_signed_forbidden_route_and_size_rejected(self):
        self.pair()
        headers = {k.lower(): v for k, v in self.capture().items()}
        def resign(changes, body):
            h = {**headers, **changes}
            values = [h['x-kiosk-' + k.lower()] for k in lc._RELAY_FIELDS]
            h['x-kiosk-signature'] = self.a._signature('relay-v1', [*values, lc.hashlib.sha256(body).hexdigest()])
            return h
        for path in ['/api/cluster/relay', '/api/peer/x', '/api/status#fragment',
                     '/api/audio/play/..', '/api/audio/play/a%2fb', '//api/status']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.b.verify(resign({'x-kiosk-path': path}, b''), b'')
        body = b'x' * 65537
        with self.assertRaises(ValueError):
            self.b.verify(resign({'x-kiosk-method': 'POST', 'x-kiosk-path': '/api/config'}, body), body)
        with self.assertRaises(ValueError):
            self.b.verify(headers, b'x' * (20 * 1024 * 1024 + 1))

    def test_http_errors_return_raw_status_and_network_errors_are_runtime(self):
        self.pair()
        error = urllib.error.HTTPError('http://example.invalid', 409, 'Conflict',
                                       {'Content-Type': 'text/plain'}, io.BytesIO(b'conflict'))
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.side_effect = error
            self.assertEqual(self.a.request('2' * 32, 'GET', '/api/status'), (409, b'conflict', 'text/plain'))
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.side_effect = urllib.error.URLError('unreachable')
            with self.assertRaises(RuntimeError):
                self.a.request('2' * 32, 'GET', '/api/status')

    def test_redirect_handler_never_creates_redirect_request(self):
        req = urllib.request.Request('http://192.168.40.11/api/cluster/relay', data=b'')
        for code in (301, 302, 303, 307, 308):
            response = MagicMock()
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                lc._NoRedirect().redirect_request(req, response, code, 'redirect', {}, 'http://example.invalid')
            response.close.assert_called_once()

    def test_redirect_refused_and_response_cap(self):
        self.pair()
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.side_effect = urllib.error.HTTPError(
                'http://192.168.40.11/api/cluster/relay', 302, 'Found', {}, io.BytesIO(b'redirect'))
            with self.assertRaises(RuntimeError):
                self.a.request('2' * 32, 'GET', '/api/status')
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'x' * (32 * 1024 * 1024 + 1)
        response.status = 200
        response.headers = {}
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                self.a.request('2' * 32, 'GET', '/api/status')
            response.read.assert_called_once_with(32 * 1024 * 1024 + 1)


class LifecycleTests(unittest.TestCase):
    def test_start_multicast_interface_ttl_and_bounded_close(self):
        cluster = lc.LanCluster('.', config())
        sent = threading.Event()
        sock = MagicMock()
        sock.sendto.side_effect = lambda *args: sent.set()
        def receive(size):
            self.assertEqual(size, 4097)
            cluster._stop.wait(.01)
            raise socket.timeout()
        sock.recvfrom.side_effect = receive
        with patch('socket.socket', return_value=sock) as factory:
            cluster.start()
            cluster.start()
            self.assertTrue(sent.wait(2))
            cluster.close()
            cluster.close()
        self.assertFalse(cluster._thread.is_alive())
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.bind.assert_called_once_with(('', 18766))
        sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton('192.168.40.10'))
        sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                       socket.inet_aton('239.255.87.65') + socket.inet_aton('192.168.40.10'))
        self.assertEqual(sock.sendto.call_args.args[1], ('239.255.87.65', 18766))
        self.assertLessEqual(len(sock.sendto.call_args.args[0]), 4096)
        self.assertEqual(cluster.snapshot()['error'], '')

    def test_multicast_error_is_nonfatal_and_socket_closed(self):
        cluster = lc.LanCluster('.', config())
        sock = MagicMock()
        sock.bind.side_effect = OSError('sensitive details')
        with patch('socket.socket', return_value=sock):
            cluster.start()
            cluster._thread.join(2)
            cluster.close()
        self.assertTrue(cluster.snapshot()['error'])
        self.assertNotIn('sensitive', cluster.snapshot()['error'])
        sock.close.assert_called()

    def test_disabled_never_opens_network_and_loads_json(self):
        with tempfile.TemporaryDirectory() as root:
            with patch('socket.socket') as factory:
                c = lc.LanCluster(root)
                c.start()
                c.close()
                factory.assert_not_called()
            Path(root, 'cluster.json').write_text(json.dumps(config()))
            self.assertTrue(lc.LanCluster(root).snapshot()['enabled'])
            Path(root, 'cluster.json').write_text('{broken')
            self.assertFalse(lc.LanCluster(root).snapshot()['enabled'])

    def test_beacon_interval_three_seconds(self):
        cluster = lc.LanCluster('.', config())
        now = [0.0]
        sends = []
        sock = MagicMock()
        sock.sendto.side_effect = lambda *args: sends.append(now[0])
        def receive(size):
            now[0] += .5
            if now[0] > 6:
                cluster._stop.set()
            raise socket.timeout()
        sock.recvfrom.side_effect = receive
        with patch('socket.socket', return_value=sock), patch.object(lc.time, 'monotonic', side_effect=lambda: now[0]):
            cluster._run()
        self.assertEqual(sends, [0, 3, 6])


if __name__ == '__main__':
    unittest.main()
