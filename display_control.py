"""Physical X11 output modes. No shell commands or Xwayland mode changes."""
import copy
import json
import tempfile
import re
import subprocess
import os
from pathlib import Path
import threading


def run_display(args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=8,
                          env={**os.environ, 'LC_ALL': 'C'}).stdout


class DisplayControl:
    def __init__(self, root, runner=run_display, environ=None):
        self.path = Path(root) / 'display-settings.json'
        self.runner = runner
        self.environ = os.environ if environ is None else environ
        self._operation = threading.RLock()
        self._lock = threading.Lock()
        self._state = {'backend': 'x11', 'supported': False, 'outputs': [], 'saved': {},
                       'busy': False, 'error': '', 'message': 'Détection de l’affichage…'}

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _publish(self, **values):
        with self._lock:
            self._state.update(values)

    def restore(self):
        """Best effort once at startup; never create defaults or overwrite a bad file."""
        with self._operation:
            try:
                if not self.path.exists():
                    return self.refresh()
                value = json.loads(self.path.read_text())
                if not isinstance(value, dict) or set(value) != {'outputs'} or not isinstance(value['outputs'], dict):
                    raise ValueError('Fichier de réglages d’affichage invalide.')
                for output, mode in value['outputs'].items():
                    if not isinstance(mode, dict):
                        raise ValueError('Mode enregistré invalide.')
                    validate_selection({'output': output, **mode})
                self._publish(saved=value['outputs'])
                errors = []
                for output, mode in value['outputs'].items():
                    try:
                        self.configure({'output': output, **mode}, persist=False)
                    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                        errors.append(str(error))
                if errors:
                    raise RuntimeError('Restauration de l’affichage : ' + '; '.join(errors))
                return self.snapshot()
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                self._publish(error=str(error), message='Restauration non aboutie ; autres services disponibles.', busy=False)
                return self.snapshot()

    def start(self, interval=15):
        if getattr(self, '_thread', None) and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._monitor, args=(interval,), daemon=True,
                                        name='display-monitor')
        self._thread.start()

    def close(self):
        if getattr(self, '_thread', None):
            self._stop.set()
            self._thread.join(timeout=26)

    def _monitor(self, interval):
        self.restore()
        while not self._stop.wait(interval):
            try:
                self.refresh()
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                self._publish(supported=False, error=str(error), message='Détection Xorg non aboutie.')

    def configure(self, selection, persist=True):
        with self._operation:
            self._publish(busy=True)
            try:
                self._configure(selection, persist)
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                self._publish(error=str(error), message='Réglage non enregistré. Vérifiez l’affichage et le stockage.')
                raise
            finally:
                self._publish(busy=False)
            return self.snapshot()

    def _configure(self, selection, persist=True):
        validate_selection(selection)
        with self._operation:
            view = self.refresh()
            if not view['supported']:
                raise RuntimeError(view['message'])
            output = next((o for o in view['outputs'] if o['name'] == selection['output']), None)
            mode = {'mode': selection['mode'], 'rate': selection['rate']}
            if output is None or mode not in output['modes']:
                raise ValueError('Sortie, résolution ou fréquence non disponible.')
            self.runner(['xrandr', '--output', output['name'], '--mode', mode['mode'], '--rate', mode['rate']])
            self.refresh()
            actual = next((o['current'] for o in self.snapshot()['outputs'] if o['name'] == output['name']), None)
            if actual != mode:
                self._publish(error='Le mode demandé n’est pas confirmé par Xorg.', busy=False)
                raise RuntimeError('Le mode demandé n’est pas confirmé par Xorg ; réglage non enregistré.')
            if not persist:
                self._publish(error='', message='Résolution enregistrée restaurée.')
                return self.snapshot()
            saved = self.snapshot()['saved']
            saved[output['name']] = mode
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.display-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'outputs': saved}, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self._publish(saved=saved, error='', message='Résolution appliquée et enregistrée.')
            return self.snapshot()

    def refresh(self):
        with self._operation:
            if self.environ.get('WAYLAND_DISPLAY') or self.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland':
                self._publish(backend='unsupported', supported=False, outputs=[],
                              message='Wayland non pris en charge : aucun changement physique via Xwayland.')
                return self.snapshot()
            if not self.environ.get('DISPLAY'):
                self._publish(backend='unsupported', supported=False, outputs=[],
                              message='Session X11 indisponible (DISPLAY absent).')
                return self.snapshot()
            outputs = parse_xrandr(self.runner(['xrandr', '--current']))
            self._publish(outputs=outputs, supported=bool(outputs),
                          message='Résolutions détectées.' if outputs else 'Aucune sortie physique détectée.')
            return self.snapshot()


# Exclude Xwayland/virtual outputs even if a session was misidentified as X11.
PHYSICAL_OUTPUT = re.compile(r'(?:HDMI(?:-A)?|DP|DisplayPort|DVI(?:-[ADI])?|VGA|eDP|LVDS)(?:-?[0-9]+)+\Z')
MODE = re.compile(r'[0-9]{2,5}x[0-9]{2,5}\Z')
RATE = re.compile(r'[0-9]{1,3}(?:\.[0-9]{1,6})?\Z')


def validate_selection(selection):
    if not isinstance(selection, dict) or set(selection) != {'output', 'mode', 'rate'}:
        raise ValueError('Indiquez uniquement output, mode et rate.')
    for key, pattern in (('output', PHYSICAL_OUTPUT), ('mode', MODE), ('rate', RATE)):
        if not isinstance(selection[key], str) or not pattern.fullmatch(selection[key]):
            raise ValueError('Sortie, résolution ou fréquence invalide.')


def parse_xrandr(text):
    outputs, output = [], None
    for line in text.splitlines():
        if line and not line[0].isspace():
            fields = line.split()
            output = None
            if len(fields) > 1 and fields[1] == 'connected' and PHYSICAL_OUTPUT.fullmatch(fields[0]):
                output = {'name': fields[0], 'current': None, 'modes': []}
                outputs.append(output)
        elif output:
            fields = line.split()
            if not fields or not MODE.fullmatch(fields[0]):
                continue
            for token in fields[1:]:
                rate = token.rstrip('*+')
                if not RATE.fullmatch(rate):
                    continue
                mode = {'mode': fields[0], 'rate': rate}
                if mode not in output['modes']:
                    output['modes'].append(mode)
                if '*' in token:
                    output['current'] = mode.copy()
    return outputs
