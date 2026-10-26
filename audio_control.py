"""HDMI soundboard: generated cues, private uploads and bounded local playback."""
import array
import glob
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import wave

LOG = logging.getLogger('kiosk.audio')
MAX_UPLOAD = 20 * 1024 * 1024
MAX_SOUND_SECONDS = 600
BUILTINS = {
    'bips': ('Trois bips', [((440,), .3, .15), ((660,), .3, .15), ((880,), .3, 0)]),
    'notification': ('Notification', [((660, 880), .35, 0)]),
    'carillon': ('Carillon', [((523,), .22, .06), ((659,), .22, .06), ((784,), .45, 0)]),
    'alerte': ('Alerte douce', [((660, 880), .25, .15), ((660, 880), .25, .15), ((660, 880), .25, 0)]),
}


def write_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.audio-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def volume_value(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError('Le volume doit être compris entre 0 et 100 %.')
    return float(value)


def hdmi_outputs():
    cards, connected = {}, []
    for path in Path('/proc/asound').glob('card*/id'):
        try:
            card = path.read_text().strip()
            if not re.fullmatch(r'vc4hdmi[0-9]+', card):
                continue
            label = 'HDMI-' + str(int(card.removeprefix('vc4hdmi')) + 1)
            item = {'id': 'hdmi:CARD=' + card + ',DEV=0', 'name': label, 'card': card}
            cards[card] = item
            for eld in path.parent.glob('eld*'):
                text = eld.read_text()
                if re.search(r'monitor_present\s+1\b', text) and re.search(r'eld_valid\s+1\b', text):
                    connected.append(item)
                    break
        except OSError:
            continue
    if not connected:
        for status in Path('/sys/class/drm').glob('card*-HDMI-A-*/status'):
            try:
                if status.read_text().strip() == 'connected':
                    number = int(re.search(r'HDMI-A-([0-9]+)', str(status)).group(1)) - 1
                    card = 'vc4hdmi' + str(number)
                    if card in cards:
                        connected.append(cards[card])
            except (OSError, ValueError, AttributeError):
                continue
    return list({item['id']: item for item in connected}.values())


class AudioControl:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / 'soundboard'
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._monitor, name='soundboard', daemon=True)
        self._player = self._converter = None
        self._key = ''
        self._catalog = {}
        self._settings = {'volume': 40.0, 'device': 'auto'}
        self._state = {'playing': False, 'current_sound': None, 'message': 'Initialisation audio…',
                       'error': None, 'last_playback_id': None, 'last_result': None, 'device': None}

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        keyfile = self.root / 'audio-api.key'
        if not keyfile.exists():
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_urlsafe(32) + '\n')
        keyfile.chmod(0o600)
        self._key = keyfile.read_text().strip()
        settings = self.directory / 'settings.json'
        if settings.exists():
            data = json.loads(settings.read_text())
            self._settings['volume'] = volume_value(data.get('volume', 40))
            self._settings['device'] = data.get('device', 'auto')
        catalog = self.directory / 'catalog.json'
        if catalog.exists():
            entries = json.loads(catalog.read_text())
            for ident, item in entries.items():
                if re.fullmatch(r'u_[a-f0-9]{16}', ident) and (self.directory / (ident + '.wav')).is_file():
                    self._catalog[ident] = item
        for ident, (name, sequence) in BUILTINS.items():
            path = self.directory / (ident + '.wav')
            if not path.exists():
                self._generate(path, sequence)
            with wave.open(str(path)) as stream:
                duration = stream.getnframes() / stream.getframerate()
            self._catalog[ident] = {'id': ident, 'name': name, 'duration': duration, 'builtin': True}
        self._save_catalog()
        self._state['message'] = 'Prêt à jouer sur la TV.'
        self._thread.start()

    @staticmethod
    def _generate(path, sequence):
        rate = 48000
        samples = array.array('h')
        for frequencies, duration, pause in sequence:
            count = int(rate * duration)
            for index in range(count):
                fade = max(0, min(1, index / (rate * .01), (count - 1 - index) / (rate * .01)))
                value = int(32767 * .2 * fade * sum(math.sin(2 * math.pi * f * index / rate) for f in frequencies) / len(frequencies))
                samples.extend((value, value))
            samples.extend([0] * (2 * int(rate * pause)))
        if __import__('sys').byteorder != 'little':
            samples.byteswap()
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(samples.tobytes())
        path.chmod(0o600)

    def _save_catalog(self):
        write_json(self.directory / 'catalog.json', self._catalog)

    def authorized(self, supplied):
        return bool(self._key) and hmac.compare_digest(supplied.encode(), self._key.encode())

    def api_key(self):
        return self._key

    def snapshot(self):
        outputs = hdmi_outputs()
        with self._lock:
            value = dict(self._state)
            selected = next((o for o in outputs if o['id'] == self._settings['device']), None)
            if self._settings['device'] == 'auto' and outputs:
                selected = outputs[0]
            value.update(volume=self._settings['volume'], selected_device=self._settings['device'],
                         device=selected['id'] if selected else None, outputs=outputs,
                         available=bool(selected and shutil.which('ffmpeg') and shutil.which('aplay')),
                         sounds=[dict(item) for item in self._catalog.values()],
                         upload_max_bytes=MAX_UPLOAD, upload_max_seconds=MAX_SOUND_SECONDS)
            return value

    def configure(self, data):
        if not isinstance(data, dict) or not data or set(data) - {'volume', 'device'}:
            raise ValueError('Réglage audio invalide.')
        with self._lock:
            updated = dict(self._settings)
            if 'volume' in data:
                updated['volume'] = volume_value(data['volume'])
            if 'device' in data:
                options = {item['id'] for item in hdmi_outputs()} | {'auto'}
                if data['device'] not in options:
                    raise ValueError('Cette sortie HDMI n’est pas disponible.')
                updated['device'] = data['device']
            write_json(self.directory / 'settings.json', updated)
            self._settings = updated
        return self.snapshot()

    def _stop_locked(self):
        for process in (self._player, self._converter):
            if process and process.poll() is None:
                process.terminate()
        for process in (self._player, self._converter):
            if process:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        self._player = self._converter = None
        self._state.update(playing=False, current_sound=None)

    def stop(self):
        with self._lock:
            self._stop_locked()
            self._state.update(message='Lecture arrêtée.', error=None, last_result='stopped')
        return self.snapshot()

    def play(self, ident, volume=None):
        with self._lock:
            if ident not in self._catalog:
                raise KeyError('Son inconnu.')
            current = self.snapshot()
            if not current['available']:
                raise RuntimeError('Sortie audio HDMI indisponible. Vérifiez le câble HDMI et les outils ffmpeg / aplay.')
            gain = volume_value(volume) if volume is not None else self._settings['volume']
            self._stop_locked()
            source = self.directory / (ident + '.wav')
            logpath = self.root / 'audio.log'
            if logpath.exists() and logpath.stat().st_size > 2_000_000:
                logpath.replace(self.root / 'audio.log.1')
            try:
                with open(logpath, 'ab') as log:
                    self._converter = subprocess.Popen(['ffmpeg', '-v', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe',
                        '-i', str(source), '-vn', '-ac', '2', '-ar', '48000', '-af', 'volume=' + str(gain / 100),
                        '-c:a', 'pcm_s16le', '-f', 'wav', 'pipe:1'], stdout=subprocess.PIPE, stderr=log)
                    self._player = subprocess.Popen(['aplay', '-q', '-D', current['device']], stdin=self._converter.stdout,
                                                    stdout=subprocess.DEVNULL, stderr=log)
                    self._converter.stdout.close()
            except OSError:
                self._stop_locked()
                raise RuntimeError('Impossible de démarrer le son. Consultez ~/.config/kiosk-manager/audio.log.') from None
            self._state.update(playing=True, current_sound=ident, error=None, last_result='playing',
                               last_playback_id=secrets.token_hex(8),
                               message='Lecture : ' + self._catalog[ident]['name'])
            LOG.info('Lecture HDMI du son %s, volume=%s', ident, gain)
        return self.snapshot()

    def upload(self, source, length, filename):
        if not 0 < length <= MAX_UPLOAD:
            raise ValueError('Le fichier audio doit peser au maximum 20 Mo.')
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise RuntimeError('L’import audio nécessite ffmpeg et ffprobe.')
        name = Path(filename.replace('\\', '/')).stem.strip()[:100] or 'Son importé'
        ident = 'u_' + secrets.token_hex(8)
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.upload-')
        destination = self.directory / (ident + '.wav')
        try:
            with os.fdopen(fd, 'wb') as stream:
                remaining = length
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError('Transfert audio interrompu. Réessayez.')
                    stream.write(chunk)
                    remaining -= len(chunk)
            probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries',
                'format=duration:stream=codec_type', '-of', 'json', temporary], capture_output=True, text=True, timeout=20)
            if probe.returncode:
                raise ValueError('Fichier audio illisible. Utilisez WAV, MP3, OGG, FLAC ou M4A.')
            info = json.loads(probe.stdout)
            if not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])):
                raise ValueError('Ce fichier ne contient pas de piste audio.')
            duration = float(info.get('format', {}).get('duration', 0))
            if not math.isfinite(duration) or not 0 < duration <= MAX_SOUND_SECONDS:
                raise ValueError('Un son importé doit durer entre plus de 0 et 600 secondes.')
            convert = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe', '-i', temporary,
                '-map', '0:a:0', '-vn', '-t', str(MAX_SOUND_SECONDS), '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(destination)],
                capture_output=True, text=True, timeout=45)
            if convert.returncode:
                raise ValueError('La conversion du son a échoué. Essayez un autre fichier.')
            destination.chmod(0o600)
            with wave.open(str(destination)) as stream:
                duration = stream.getnframes() / stream.getframerate()
            item = {'id': ident, 'name': name, 'duration': duration, 'builtin': False}
            with self._lock:
                self._catalog[ident] = item
                self._save_catalog()
            return item
        except (subprocess.TimeoutExpired, ValueError):
            destination.unlink(missing_ok=True)
            raise
        finally:
            Path(temporary).unlink(missing_ok=True)

    def delete(self, ident):
        with self._lock:
            if ident not in self._catalog:
                raise KeyError('Son inconnu.')
            if self._catalog[ident]['builtin']:
                raise ValueError('Les sons intégrés ne peuvent pas être supprimés.')
            if self._state['current_sound'] == ident:
                self._stop_locked()
            self._catalog.pop(ident)
            self._save_catalog()
            (self.directory / (ident + '.wav')).unlink(missing_ok=True)
        return self.snapshot()

    def _monitor(self):
        while not self._stop_event.wait(.15):
            with self._lock:
                if self._player and self._player.poll() is not None:
                    code = self._player.returncode
                    converter = self._converter.poll() if self._converter else 0
                    success = code == 0 and converter in (None, 0)
                    ident = self._state['current_sound']
                    self._stop_locked()
                    self._state.update(last_result='finished' if success else 'error',
                        message='Son terminé.' if success else 'Lecture HDMI impossible. Vérifiez la sortie TV ; détails dans audio.log.',
                        error=None if success else 'Erreur audio HDMI.')
                    LOG.info('Fin de lecture %s : %s', ident, 'ok' if success else 'erreur')

    def close(self):
        self._stop_event.set()
        with self._lock:
            self._stop_locked()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
