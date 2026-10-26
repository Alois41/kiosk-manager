"""Opt-in authenticated LAN discovery and narrow HTTP relay (stdlib only)."""
import hashlib
import hmac
import secrets
import socket
import time
from collections import OrderedDict
import ipaddress
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote

_GET = {'/api/status', '/api/display', '/api/tv', '/api/audio',
        '/api/audio/sounds', '/api/audio/key', '/api/audio/api'}
_POST = {'/api/config', '/api/action', '/api/tv', '/api/display/settings',
         '/api/audio/play', '/api/audio/stop', '/api/audio/settings',
         '/api/audio/upload', '/api/audio/delete'}
_HEX32 = re.compile(r'[0-9a-fA-F]{32}\Z')
_HEX64 = re.compile(r'[0-9a-fA-F]{64}\Z')
_RELAY_FIELDS = ('Peer', 'Target', 'Time', 'Nonce', 'Method', 'Path', 'Type', 'Filename')
_RESPONSE_LIMIT = 32 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise RuntimeError('Peer redirects are forbidden')


def _metadata(method, path, content_type, filename):
    if (not _text(method, 16) or not _text(path, 256) or not _text(content_type) or
            not isinstance(filename, str) or len(filename) > 765 or
            (filename and not re.fullmatch(r'(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})(?:[A-Za-z0-9_. -]|%[0-9A-Fa-f]{2})*', filename))):
        raise ValueError('Malformed relay metadata')
    # The browser percent-encodes X-Filename; validate the decoded basename too.
    decoded = unquote(filename, errors='strict')
    if (len(decoded) > 255 or (decoded and decoded[0] in '. ') or
            any(c in '/\\\\' or not c.isprintable() for c in decoded)):
        raise ValueError('Malformed relay filename')



def allowed_request(method, path, body_length):
    """Return whether this exact route and raw body size may cross the relay."""
    if not isinstance(path, str) or type(body_length) is not int or body_length < 0:
        return False
    valid = (method == 'GET' and path in _GET or method == 'POST' and
             (path in _POST or re.fullmatch(r'/api/audio/play/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', path)))
    limit = 20 * 1024 * 1024 if path == '/api/audio/upload' else 65536
    return bool(valid and body_length <= limit)


def _text(value, maximum=255):
    return (isinstance(value, str) and 0 < len(value) <= maximum and
            all(32 <= ord(c) <= 126 for c in value))


class LanCluster:
    def __init__(self, root, config=None):
        self.enabled = False
        self._self = {}
        self._error = ''
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._socket = None
        self._peers = {}
        self._beacon_nonces = OrderedDict()
        self._relay_nonces = OrderedDict()
        try:
            if config is None:
                path = Path(root) / 'cluster.json'
                if not path.exists():
                    return
                if path.stat().st_size > 16384:
                    raise ValueError('oversize configuration')
                config = json.loads(path.read_text())
            if not isinstance(config, dict):
                raise ValueError('invalid configuration')
            if not _HEX32.fullmatch(config.get('id', '')) or not _HEX64.fullmatch(config.get('key', '')):
                raise ValueError('invalid identity or key')
            if not isinstance(config['address'], str):
                raise ValueError('IPv4 address must be text')
            address = ipaddress.IPv4Address(config['address'])
            networks = config['networks']
            if not isinstance(networks, list) or not networks or not all(isinstance(n, str) for n in networks):
                raise ValueError('explicit networks required')
            self._networks = [ipaddress.IPv4Network(n) for n in networks]
            if any(n.prefixlen == 0 for n in self._networks) or not any(address in n for n in self._networks):
                raise ValueError('invalid local network')
            if address.is_multicast or address.is_unspecified or address.is_loopback:
                raise ValueError('invalid local address')
            if type(config['port']) is not int or not 1 <= config['port'] <= 65535 or not _text(config['name']):
                raise ValueError('invalid name or port')
            self._self = {k: config[k] for k in ('id', 'name', 'address', 'port')}
            self._key = bytes.fromhex(config['key'])
            self.enabled = True
        except (ValueError, TypeError, KeyError, OSError):
            self._error = 'Invalid cluster configuration'

    def start(self):
        """Start once in a daemon thread; multicast failures never abort the app."""
        with self._lock:
            if not self.enabled or (self._thread is not None and self._thread.is_alive()):
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='kiosk-lan-cluster', daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._error = 'Could not start LAN discovery'
                self._thread = None

    def close(self):
        """Interrupt receives and bound shutdown, even on a broken network stack."""
        with self._lock:
            self._stop.set()
            sock, thread = self._socket, self._thread
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.5)

    def _run(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('', 18766))
            interface = socket.inet_aton(self._self['address'])
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, interface)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton('239.255.87.65') + interface)
            sock.settimeout(.5)
            with self._lock:
                if self._stop.is_set():
                    return
                self._socket = sock
                self._error = ''
            next_send = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_send:
                    sock.sendto(self._beacon(), ('239.255.87.65', 18766))
                    next_send = now + 3
                try:
                    data, source = sock.recvfrom(4097)
                except socket.timeout:
                    continue
                self._receive_beacon(data, source[0])
        except OSError:
            with self._lock:
                if not self._stop.is_set():
                    self._error = 'LAN discovery unavailable'
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._lock:
                self._socket = None

    def _signature(self, domain, values):
        # Domain separation and canonical arrays avoid ambiguous concatenation.
        data = json.dumps([domain, *values], ensure_ascii=True, separators=(',', ':')).encode('ascii')
        return hmac.new(self._key, data, hashlib.sha256).hexdigest()

    def _beacon(self):
        beacon = {**self._self, 'time': int(time.time()), 'nonce': secrets.token_hex(16)}
        beacon.pop('address')  # Never advertise or trust a destination in the payload.
        beacon['signature'] = self._signature('beacon-v1', list(beacon.values()))
        return json.dumps(beacon, separators=(',', ':')).encode('ascii')

    def _source_allowed(self, source):
        try:
            address = ipaddress.IPv4Address(source)
            return (not address.is_multicast and not address.is_unspecified and
                    not address.is_loopback and any(address in n for n in self._networks))
        except (ValueError, TypeError):
            return False

    def _fresh_nonce(self, cache, identity, nonce):
        now = time.monotonic()
        for key, expiry in list(cache.items()):
            if expiry <= now:
                del cache[key]
        key = (identity, nonce)
        # Never evict a live nonce: fail closed when full, preserving replay protection.
        if key in cache or len(cache) >= 4096:
            return False
        cache[key] = now + 61
        return True

    def _prune(self):
        now = time.monotonic()
        for identity, peer in list(self._peers.items()):
            if now - peer['seen'] > 60:
                del self._peers[identity]

    def _receive_beacon(self, data, source):
        if not self.enabled or len(data) > 4096 or not self._source_allowed(source):
            return False
        try:
            b = json.loads(data)
            if not isinstance(b, dict) or set(b) != {'id', 'name', 'port', 'time', 'nonce', 'signature'}:
                return False
            if (not isinstance(b['id'], str) or not _HEX32.fullmatch(b['id']) or
                    b['id'] == self._self['id'] or not _text(b['name']) or
                    type(b['port']) is not int or b['port'] != self._self['port'] or
                    type(b['time']) is not int or not time.time() - 30 <= b['time'] <= time.time() + 30 or
                    not isinstance(b['nonce'], str) or not _HEX32.fullmatch(b['nonce']) or
                    not isinstance(b['signature'], str) or not _HEX64.fullmatch(b['signature'])):
                return False
            expected = self._signature('beacon-v1', [b[k] for k in ('id', 'name', 'port', 'time', 'nonce')])
            if not hmac.compare_digest(expected, b['signature']):
                return False
            with self._lock:
                self._prune()
                if b['id'] not in self._peers and len(self._peers) >= 128:
                    return False
                if not self._fresh_nonce(self._beacon_nonces, b['id'], b['nonce']):
                    return False
                self._peers[b['id']] = dict(id=b['id'], name=b['name'], address=source,
                                           port=b['port'], seen=time.monotonic())
            return True
        except (ValueError, TypeError, KeyError, RecursionError):
            return False

    def _live_peer(self, identity):
        peer = self._peers.get(identity)
        if (not self.enabled or peer is None or time.monotonic() - peer['seen'] > 15 or
                not self._source_allowed(peer['address']) or peer['port'] != self._self['port']):
            raise RuntimeError('Peer is offline or unknown')
        return dict(peer)

    def request(self, peer_id, method, path, body=b'', content_type='application/json', filename=''):
        """Relay raw bytes to one live authenticated peer; never forward UI credentials."""
        if not isinstance(body, bytes) or not allowed_request(method, path, len(body)):
            raise ValueError('Forbidden relay route or body size')
        _metadata(method, path, content_type, filename)
        with self._lock:
            peer = self._live_peer(peer_id)
        values = [self._self['id'], peer_id, str(int(time.time())), secrets.token_hex(16),
                  method, path, content_type, filename]
        headers = {'X-Kiosk-' + k: v for k, v in zip(_RELAY_FIELDS, values)}
        headers['X-Kiosk-Signature'] = self._signature('relay-v1', [*values, hashlib.sha256(body).hexdigest()])
        headers['Content-Type'] = 'application/octet-stream'
        req = urllib.request.Request(
            f"http://{peer['address']}:{peer['port']}/api/cluster/relay",
            data=body, headers=headers, method='POST')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            try:
                response = opener.open(req, timeout=25)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status = response.status
                if 300 <= status < 400:
                    raise RuntimeError('Peer redirects are forbidden')
                raw = response.read(_RESPONSE_LIMIT + 1)
                if len(raw) > _RESPONSE_LIMIT:
                    raise ValueError('Peer response too large')
                response_type = response.headers.get('Content-Type', 'application/octet-stream')
                if not _text(response_type):
                    raise ValueError('Malformed peer content type')
                return status, raw, response_type
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError('Peer request failed') from exc

    def verify(self, headers, body):
        """Authenticate relay metadata before the caller dispatches locally.

        Authentication failures raise PermissionError; malformed/forbidden requests
        raise ValueError. No browser credentials are accepted or returned.
        """
        if not isinstance(body, bytes) or len(body) > 20 * 1024 * 1024:
            raise ValueError('Invalid relay body')
        normalized = {}
        try:
            for name, value in headers.items():
                name = name.lower()
                if not name.startswith('x-kiosk-'):
                    continue
                if name in normalized or not isinstance(value, str):
                    raise ValueError('Duplicate or invalid relay header')
                normalized[name] = value
            values = [normalized['x-kiosk-' + k.lower()] for k in _RELAY_FIELDS]
            signature = normalized['x-kiosk-signature']
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError('Missing relay headers') from exc
        source, target, stamp, nonce, method, path, content_type, filename = values
        if (not _HEX32.fullmatch(source) or not _HEX32.fullmatch(target) or
                not re.fullmatch(r'[0-9]{1,12}', stamp) or not _HEX32.fullmatch(nonce) or
                not _HEX64.fullmatch(signature)):
            raise ValueError('Malformed relay identity, time, nonce or signature')
        _metadata(method, path, content_type, filename)
        with self._lock:
            if not self.enabled or target != self._self['id'] or abs(time.time() - int(stamp)) > 30:
                raise PermissionError('Invalid relay target or time')
            try:
                self._live_peer(source)
            except RuntimeError as exc:
                raise PermissionError('Unknown or offline relay source') from exc
            expected = self._signature('relay-v1', [*values, hashlib.sha256(body).hexdigest()])
            if not hmac.compare_digest(expected, signature):
                raise PermissionError('Invalid relay signature')
            if not allowed_request(method, path, len(body)):
                raise ValueError('Forbidden relay route or body size')
            if not self._fresh_nonce(self._relay_nonces, source, nonce):
                raise PermissionError('Replayed relay or nonce cache full')
        return method, path, content_type, filename

    def snapshot(self):
        with self._lock:
            self._prune()
            peers = [{**{k: p[k] for k in ('id', 'name', 'address', 'port')},
                      'online': time.monotonic() - p['seen'] <= 15}
                     for p in self._peers.values()]
            return {'enabled': self.enabled, 'self': dict(self._self),
                    'peers': sorted(peers, key=lambda p: p['id']), 'error': self._error}
