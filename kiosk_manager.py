#!/usr/bin/env python3
"""Gestion locale du kiosque. Standard library only; no credential automation."""
import argparse
import collections
import hmac
import html
import getpass
import socket
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from tv_control import CecControl, wake_host_output
from browser_tabs import prepare_tab_session
from audio_control import AudioControl, MAX_UPLOAD
from display_control import DisplayControl

HOME = Path.home()
ROOT = Path(os.environ.get('KIOSK_CONFIG_DIR', HOME / '.config/kiosk-manager'))
CONFIG = ROOT / 'config.json'
PROFILE = Path(os.environ.get('KIOSK_BROWSER_PROFILE', HOME / '.config/chromium'))
PORT = int(os.environ.get('KIOSK_PORT', '8765'))
TOKEN = secrets.token_urlsafe(32)
SSH_TARGET = os.environ.get('KIOSK_SSH_TARGET', getpass.getuser() + '@' + socket.gethostname())
PUBLIC_URL = os.environ.get('KIOSK_PUBLIC_URL', 'http://' + socket.gethostname())
COMMANDS = queue.Queue(maxsize=20)
STOP = threading.Event()
CONFIG_CHANGED = threading.Event()
LOCK = threading.Lock()
STATE = {'mode': 'kiosk', 'browser_alive': False, 'page': 0, 'error': '', 'window': None, 'applied_config': None}
LOG = logging.getLogger('kiosk')
TV = CecControl()
AUDIO = AudioControl(ROOT)
DISPLAY = DisplayControl(ROOT)
LAN = None  # Initialized at startup; unconfigured kiosks remain standalone.


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {'pages'}:
        raise ValueError('La configuration doit contenir uniquement pages.')
    pages = value['pages']
    if not isinstance(pages, list) or len(pages) > 8:
        raise ValueError('Huit pages maximum. Une liste vide arrête le navigateur.')
    clean = []
    for page in pages:
        if not isinstance(page, dict) or set(page) != {'name', 'url', 'duration'}:
            raise ValueError('Chaque page doit contenir nom, adresse et durée.')
        name, url, duration = page['name'], page['url'], page['duration']
        if not isinstance(name, str) or len(name.strip()) > 100:
            raise ValueError('Le nom doit comporter au maximum 100 caractères.')
        if url == '':
            continue
        if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
            raise ValueError('Adresse web invalide.')
        try:
            parts = urlsplit(url)
            if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None or parts.password is not None:
                raise ValueError()
            _ = parts.port
        except ValueError:
            raise ValueError('Utilisez une URL HTTP(S), sans identifiant ni mot de passe.') from None
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            raise ValueError('Durée positive ou nulle en secondes ; 0 signifie changement manuel.')
        clean.append({'name': name.strip() or parts.hostname, 'url': url, 'duration': duration})
    return {'pages': clean}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.config-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_config():
    return validate_config(json.loads(CONFIG.read_text()))


def run(args, timeout=5, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check).stdout.strip()


def state(**kw):
    with LOCK:
        STATE.update(kw)


def snapshot():
    with LOCK:
        return dict(STATE)


def display_setup():
    if os.environ.get('WAYLAND_DISPLAY'):
        wake_host_output()
        return
    for args in (['xset', 's', 'off'], ['xset', 's', 'noblank'], ['xset', '-dpms'], ['xset', 'dpms', 'force', 'on']):
        run(args, check=False)
    # Force-on may enable DPMS again on some servers: disable last.
    run(['xset', '-dpms'], check=False)


def refresh_wallpaper():
    wallpaper = HOME / '.config/openbox/wallpaper.png'
    if wallpaper.exists():
        run(['feh', '--no-fehbg', '--bg-scale', str(wallpaper)], check=False)


def window_id(proc):
    result = run(['xdotool', 'search', '--onlyvisible', '--all', '--pid', str(proc.pid), '--class', 'chromium'], check=False)
    return result.splitlines()[-1] if result else None


def key(win, value):
    run(['xdotool', 'windowactivate', '--sync', win, 'key', '--clearmodifiers', value])
    # X11 delivery is asynchronous; allow Chromium on the Pi to process focus.
    time.sleep(0.15)


def navigate(win, url):
    key(win, 'ctrl+l')
    run(['xdotool', 'type', '--clearmodifiers', '--delay', '3', url], timeout=30)
    time.sleep(0.15)
    key(win, 'Return')


def apply_tabs(win, old_config, new_config):
    old_pages, new_pages = old_config['pages'], new_config['pages']
    for index, new_page in enumerate(new_pages):
        if index >= len(old_pages):
            key(win, 'ctrl+t')
            time.sleep(0.25)
            navigate(win, new_page['url'])
        elif new_page['url'] != old_pages[index]['url']:
            key(win, 'ctrl+' + str(index + 1))
            navigate(win, new_page['url'])
    # Never close the last configured tab: at least one page is mandatory.
    for number in range(len(old_pages), len(new_pages), -1):
        key(win, 'ctrl+' + str(number))
        key(win, 'ctrl+w')


def fullscreen(win, desired):
    current = '_NET_WM_STATE_FULLSCREEN' in run(['xprop', '-id', win, '_NET_WM_STATE'])
    if current != desired:
        key(win, 'F11')


def stop_browser(proc):
    if proc and proc.poll() is None:
        # Closing the UI lets Chromium flush persistent cookies before exiting.
        # SIGTERM alone can lose recent session changes.
        try:
            win = window_id(proc)
            if win:
                key(win, 'ctrl+shift+w')
                proc.wait(timeout=10)
        except (subprocess.SubprocessError, OSError):
            LOG.warning('Fermeture graphique incomplète ; arrêt du processus.')
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    state(browser_alive=False, window=None, browser_pid=None)


def launch_browser(config, mode):
    cleaned = prepare_tab_session(PROFILE, ROOT / 'previous-tabs')
    LOG.info('Ancien état des onglets écarté : %s fichiers ; ouverture de %s pages configurées.', cleaned['saved_state_files_moved'], len(config['pages']))
    args = ['/usr/lib/chromium/chromium', '--user-data-dir=' + str(PROFILE),
            '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble',
            '--disable-dev-shm-usage', '--lang=fr-FR',
            '--start-fullscreen' if mode == 'kiosk' else '--start-maximized']
    args.append('--disable-gpu' if os.environ.get('KIOSK_SOFTWARE_RENDERING') == '1' else '--use-angle=gles')
    if os.environ.get('KIOSK_PASSWORD_STORE') == 'basic':
        args.append('--password-store=basic')
    if os.environ.get('WAYLAND_DISPLAY'):
        # Retain the Wayland desktop while controlling Chromium via Xwayland.
        args.append('--ozone-platform=x11')
    args.extend(p['url'] for p in config['pages'])
    # Direct binary avoids the broken Debian --load-extension wrapper setting.
    # Do not disable TLS checks, the Chromium sandbox, or web security.
    logpath = ROOT / 'chromium.log'
    if logpath.exists() and logpath.stat().st_size > 2_000_000:
        logpath.replace(ROOT / 'chromium.log.1')
    with open(logpath, 'ab') as stream:
        proc = subprocess.Popen(args, stdout=stream, stderr=stream)
    LOG.info('Navigateur lancé, pid=%s, mode=%s', proc.pid, mode)
    return proc


def page_deadline(config, page, now=None):
    duration = config['pages'][page]['duration']
    return (time.monotonic() if now is None else now) + duration if duration > 0 else math.inf


def controller():
    proc = None
    config = read_config()
    state(applied_config=config, browser_pid=None, empty=not config['pages'])
    mode, page, win = 'kiosk', 0, None
    deadline, launched, retry_at, last_display, last_geometry = math.inf, 0, 0, 0, None
    try:
        while not STOP.is_set():
            try:
                command = COMMANDS.get(timeout=.1)
            except queue.Empty:
                command = None
            actions = []
            if CONFIG_CHANGED.is_set():
                CONFIG_CHANGED.clear()
                actions.append({'action': 'apply'})
            if command:
                actions.append(command)
            try:
                now = time.monotonic()
                for action in actions:
                    cmd = action['action']
                    if cmd == 'apply':
                        updated = read_config()
                        if not updated['pages']:
                            stop_browser(proc)
                            proc, win, page, retry_at, deadline = None, None, 0, 0, math.inf
                            config = updated
                            state(applied_config=config, page=0, empty=True, error='')
                        elif proc is not None and proc.poll() is None and win:
                            apply_tabs(win, config, updated)
                            config = updated
                            page = min(page, len(config['pages']) - 1)
                            key(win, 'ctrl+' + str(page + 1))
                            deadline = page_deadline(config, page)
                            state(applied_config=config, page=page, empty=False, error='')
                        else:
                            stop_browser(proc)
                            config = updated
                            proc, win, page, retry_at = None, None, 0, 0
                            state(applied_config=config, empty=False, error='')
                    elif cmd == 'restart':
                        stop_browser(proc)
                        config = read_config()
                        proc, win, page, retry_at = None, None, 0, 0
                        state(applied_config=config, empty=not config['pages'], page=0, error='')
                    elif cmd == 'login' and config['pages']:
                        mode = 'login'
                        page = min(action.get('page', 0), len(config['pages']) - 1)
                        if win:
                            fullscreen(win, False)
                            key(win, 'ctrl+' + str(page + 1))
                        state(mode=mode, page=page)
                    elif cmd == 'kiosk' and config['pages']:
                        returning = mode == 'login'
                        mode = 'kiosk'
                        if win:
                            key(win, 'ctrl+' + str(page + 1))
                            if returning:
                                navigate(win, config['pages'][page]['url'])
                            fullscreen(win, True)
                        deadline = page_deadline(config, page)
                        state(mode=mode)
                    elif cmd == 'next' and win and config['pages']:
                        page = (page + 1) % len(config['pages'])
                        key(win, 'ctrl+' + str(page + 1))
                        deadline = page_deadline(config, page)
                        state(page=page)
                    elif cmd == 'reload' and win:
                        key(win, 'ctrl+r')
                if now - last_display > 15:
                    display_setup()
                    geometry = run(['xrandr', '--current'])
                    geometry = '\n'.join(line for line in geometry.splitlines() if ' connected' in line or line.startswith('Screen '))
                    if geometry != last_geometry:
                        refresh_wallpaper()
                        last_geometry = geometry
                    last_display = now
                if not config['pages']:
                    state(browser_alive=False, browser_pid=None, window=None, empty=True, error='')
                    continue
                if proc is not None and proc.poll() is not None:
                    LOG.warning('Navigateur arrêté, code=%s ; reprise dans 10 secondes.', proc.returncode)
                    state(browser_alive=False, browser_pid=None, window=None, error='Navigateur arrêté ; reprise dans 10 secondes.')
                    proc, win, retry_at = None, None, time.monotonic() + 10
                if proc is None and time.monotonic() >= retry_at:
                    proc = launch_browser(config, mode)
                    launched, page, win = time.monotonic(), 0, None
                if proc is not None and proc.poll() is None:
                    if not win:
                        win = window_id(proc)
                        if win:
                            fullscreen(win, mode == 'kiosk')
                            key(win, 'ctrl+1')
                            deadline = page_deadline(config, page)
                            state(browser_alive=True, browser_pid=proc.pid, window=win, page=page, empty=False, error='')
                        elif time.monotonic() - launched > 30:
                            state(error='Aucune fenêtre Chromium visible. Consultez chromium.log.')
                    elif mode == 'kiosk' and len(config['pages']) > 1 and time.monotonic() >= deadline:
                        page = (page + 1) % len(config['pages'])
                        key(win, 'ctrl+' + str(page + 1))
                        deadline = page_deadline(config, page)
                        state(page=page)
            except Exception as error:
                LOG.exception('Erreur de contrôle')
                state(error=str(error) if isinstance(error, RuntimeError) else 'Commande non aboutie. Consultez manager.log.')
                STOP.wait(1)
    finally:
        stop_browser(proc)

class Handler(BaseHTTPRequestHandler):
    server_version = 'KioskManager/2'

    def log_message(self, fmt, *args):
        LOG.info('%s %s', self.command, self.path.split('?')[0])

    def reply(self, code, body, content_type='application/json; charset=utf-8'):
        raw = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'nonce-" + TOKEN + "'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)

    def respond(self, code, data):
        return self.reply(code, json.dumps(data, ensure_ascii=False, allow_nan=False))

    def allowed_host(self):
        return self.headers.get('Host', '') in {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}

    def proxy_peer(self, method, path):
        """Same-origin UI gateway. Never accepts a caller-supplied destination URL."""
        match = re.fullmatch(r'/api/peers/([0-9a-f]{32})(/[^?]*)', path)
        if not match or self.path != path or LAN is None:
            return self.respond(400, {'error': 'TV ou route inconnue.'})
        ident, suffix = match.groups()
        remote_path = '/api' + suffix
        try:
            size = int(self.headers.get('Content-Length', '0'))
            limit = MAX_UPLOAD if method == 'POST' and remote_path == '/api/audio/upload' else 65536
            if not 0 <= size <= limit or (method == 'GET' and size):
                raise ValueError('Requête trop grande ou invalide.')
            body = self.rfile.read(size) if size else b''
            if len(body) != size:
                raise ValueError('Requête incomplète.')
            code, raw, _ = LAN.request(ident, method, remote_path, body,
                                      self.headers.get('Content-Type', 'application/json'),
                                      self.headers.get('X-Filename', ''))
            return self.reply(code, raw)
        except (ValueError, TypeError):
            return self.respond(400, {'error': 'Requête distante invalide.'})
        except (OSError, RuntimeError):
            return self.respond(503, {'error': 'TV distante indisponible. Aucun basculement vers la TV locale.'})

    def do_GET(self):
        if not self.allowed_host():
            return self.respond(403, {'error': 'Accès local uniquement.'})
        path = urlsplit(self.path).path
        if path == '/':
            markup = Path(__file__).with_name('index.html').read_text().replace('__TOKEN__', TOKEN)
            markup = markup.replace('__SSH_TARGET__', html.escape(SSH_TARGET, quote=True)).replace('__PUBLIC_URL__', html.escape(PUBLIC_URL, quote=True))
            return self.reply(200, markup, 'text/html; charset=utf-8')
        if path.startswith('/api/peers/'):
            return self.proxy_peer('GET', path)
        if path == '/api/cluster':
            return self.respond(200, LAN.snapshot() if LAN else {'enabled': False, 'self': {}, 'peers': [], 'error': ''})
        if path == '/api/status':
            return self.respond(200, {'state': snapshot(), 'config': read_config(), 'tv': TV.snapshot(), 'audio': AUDIO.snapshot(), 'display': DISPLAY.snapshot()})
        if path == '/api/display':
            return self.respond(200, DISPLAY.snapshot())
        if path == '/api/tv':
            return self.respond(200, TV.snapshot())
        if path == '/api/audio':
            return self.respond(200, AUDIO.snapshot())
        if path == '/api/audio/sounds':
            return self.respond(200, {'sounds': AUDIO.snapshot()['sounds']})
        if path == '/api/audio/key':
            return self.respond(200, {'key': AUDIO.api_key(), 'header': 'X-API-Key'})
        if path == '/api/audio/api':
            return self.respond(200, {'authentication': 'Authentification HTTP du gestionnaire + X-API-Key, ou jeton X-Kiosk-Token de l’interface.',
                'routes': {'GET /api/audio/sounds': 'Catalogue et identifiants', 'POST /api/audio/play': {'id': 'bips', 'volume': 40},
                           'POST /api/audio/play/{id}': 'Lecture avec corps JSON facultatif', 'POST /api/audio/stop': 'Arrêt immédiat',
                           'POST /api/audio/settings': {'volume': 40, 'device': 'auto'}, 'POST /api/audio/upload': 'Corps binaire ; nom dans X-Filename (encodé URL)',
                           'POST /api/audio/delete': {'id': 'identifiant du son importé'}}})
        return self.respond(404, {'error': 'Introuvable.'})

    def relay_peer(self):
        """Machine-to-machine entry point: paired HMAC authentication, not UI auth."""
        if not self.allowed_host() or self.path != '/api/cluster/relay' or LAN is None:
            return self.respond(403, {'error': 'Pairage requis.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= MAX_UPLOAD:
                raise ValueError('Requête trop grande.')
            body = self.rfile.read(size) if size else b''
            if len(body) != size:
                raise ValueError('Requête incomplète.')
            method, path, content_type, filename = LAN.verify(self.headers, body)
            # Defense in depth: the relay can never become a URL fetcher or recurse.
            if method not in ('GET', 'POST') or not re.fullmatch(
                    r'/api/(?:status|display(?:/settings)?|tv|config|action|audio(?:/(?:sounds|key|api|play(?:/[A-Za-z0-9_-]+)?|stop|settings|upload|delete))?)', path):
                raise ValueError('Route interdite.')
            headers = {'X-Kiosk-Token': TOKEN, 'Content-Type': content_type}
            if filename:
                headers['X-Filename'] = filename
            request = Request(f'http://127.0.0.1:{PORT}' + path,
                              data=body if method == 'POST' else None, headers=headers, method=method)
            try:
                response = build_opener(ProxyHandler({})).open(request, timeout=30)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ValueError('Réponse trop grande.')
                return self.reply(response.status, raw)
        except PermissionError:
            return self.respond(403, {'error': 'Pairage ou signature invalide.'})
        except (ValueError, TypeError):
            return self.respond(400, {'error': 'Requête signée invalide.'})
        except (OSError, RuntimeError):
            return self.respond(503, {'error': 'Relais indisponible.'})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == '/api/cluster/relay':
            return self.relay_peer()
        ui_token = hmac.compare_digest(self.headers.get('X-Kiosk-Token', '').encode(), TOKEN.encode())
        audio_key = path.startswith('/api/audio/') and AUDIO.authorized(self.headers.get('X-API-Key', ''))
        if not self.allowed_host() or not (ui_token or audio_key):
            return self.respond(403, {'error': 'Accès refusé. Rechargez l’interface ou fournissez la clé API audio.'})
        if self.headers.get('Origin') not in (None, f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'):
            return self.respond(403, {'error': 'Origine refusée.'})
        if path.startswith('/api/peers/'):
            return self.proxy_peer('POST', path)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if path == '/api/audio/upload':
                if not 0 < size <= MAX_UPLOAD:
                    raise ValueError('Choisissez un fichier audio de 20 Mo maximum.')
                item = AUDIO.upload(self.rfile, size, unquote(self.headers.get('X-Filename', 'Son.wav')))
                return self.respond(201, {'ok': True, 'sound': item, 'audio': AUDIO.snapshot()})
            if not 0 <= size <= 65536:
                raise ValueError('Requête trop grande.')
            if not size and not path.startswith('/api/audio/'):
                raise ValueError('Requête vide.')
            data = json.loads(self.rfile.read(size)) if size else {}
            if not isinstance(data, dict):
                raise ValueError('Requête invalide.')
            if path == '/api/audio/play' or path.startswith('/api/audio/play/'):
                ident = path.removeprefix('/api/audio/play/') if path.startswith('/api/audio/play/') else data.get('id')
                if not isinstance(ident, str):
                    raise ValueError('Choisissez un identifiant de son.')
                return self.respond(202, {'ok': True, 'audio': AUDIO.play(ident, data.get('volume'))})
            if path == '/api/audio/stop':
                return self.respond(200, {'ok': True, 'audio': AUDIO.stop()})
            if path == '/api/display/settings':
                return self.respond(200, {'ok': True, 'display': DISPLAY.configure(data)})
            if path == '/api/audio/settings':
                return self.respond(200, {'ok': True, 'audio': AUDIO.configure(data)})
            if path == '/api/audio/delete':
                return self.respond(200, {'ok': True, 'audio': AUDIO.delete(data.get('id'))})
            if path == '/api/tv':
                if set(data) != {'action'} or data['action'] not in ('wake', 'source', 'check'):
                    raise ValueError('Commande TV inconnue.')
                try:
                    TV.submit(data['action'])
                except RuntimeError as error:
                    return self.respond(409, {'error': str(error)})
                return self.respond(202, {'ok': True, 'tv': TV.snapshot()})
            if path == '/api/config':
                clean = validate_config(data)
                atomic_json(CONFIG, clean)
                CONFIG_CHANGED.set()
                return self.respond(200, {'ok': True, 'config': clean, 'message': 'Modifications enregistrées ; application automatique.'})
            if path != '/api/action' or data.get('action') not in ('apply', 'restart', 'login', 'kiosk', 'next', 'reload'):
                raise ValueError('Commande inconnue.')
            current = read_config()
            if not current['pages'] and data['action'] not in ('apply', 'restart'):
                return self.respond(409, {'error': 'Ajoutez une adresse pour ouvrir le navigateur.'})
            if data['action'] == 'login':
                if snapshot().get('applied_config') != current:
                    return self.respond(409, {'error': 'Les modifications sont encore en cours d’application. Réessayez dans un instant.'})
                idx = data.get('page', 0)
                if type(idx) is not int or not 0 <= idx < len(current['pages']):
                    raise ValueError('Page inconnue.')
            COMMANDS.put_nowait(data)
            return self.respond(202, {'ok': True, 'message': 'Commande transmise.'})
        except KeyError:
            return self.respond(404, {'error': 'Son inconnu.'})
        except (ValueError, TypeError, OverflowError, queue.Full) as error:
            return self.respond(400, {'error': 'Trop de commandes, patientez.' if isinstance(error, queue.Full) else str(error)})
        except RuntimeError as error:
            return self.respond(409, {'error': str(error)})
        except (OSError, subprocess.SubprocessError):
            LOG.exception('Opération non aboutie')
            return self.respond(503, {'error': 'Opération non aboutie. Vérifiez Xorg, les outils audio, le stockage et manager.log.'})

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

def main():
    from lan_cluster import LanCluster
    global LAN
    os.umask(0o077)
    LAN = LanCluster(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), RotatingFileHandler(ROOT / 'manager.log', maxBytes=2_000_000, backupCount=2)])
    read_config()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    AUDIO.start()
    TV.start()
    DISPLAY.start()
    LAN.start()
    thread = threading.Thread(target=controller, daemon=False)
    thread.start()
    server.timeout = 0.5
    try:
        while not STOP.is_set():
            server.handle_request()
    finally:
        STOP.set()
        AUDIO.close()
        DISPLAY.close()
        LAN.close()
        server.server_close()
        thread.join(timeout=25)
        TV.close()


if __name__ == '__main__':
    main()
