"""HDMI-CEC TV controls. Bounded calls, no shell, no browser interaction."""
import datetime
import glob
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading

LOG = logging.getLogger('kiosk.cec')
POWER_LABELS = {'on': 'Allumée', 'standby': 'En veille', 'to-on': 'Allumage en cours',
                'to-standby': 'Mise en veille en cours', 'unknown': 'État non confirmé'}


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def wake_host_output():
    if os.environ.get('WAYLAND_DISPLAY') and shutil.which('wlopm'):
        try:
            query = subprocess.run(['wlopm', '--json'], capture_output=True, text=True, timeout=3, check=True)
            outputs = json.loads(query.stdout)
            for item in outputs:
                name = item.get('output')
                if name:
                    subprocess.run(['wlopm', '--on', name], capture_output=True, timeout=3, check=True)
            return
        except (OSError, ValueError, subprocess.SubprocessError):
            LOG.warning('Le réveil de la sortie Wayland n’a pas abouti.', exc_info=True)
            return
    for args in (['xset', 'dpms', 'force', 'on'], ['xset', 's', 'reset'], ['xset', '-dpms']):
        try:
            subprocess.run(args, capture_output=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            LOG.warning('Le réveil X11 n’a pas abouti ; tentative CEC conservée.')


class CecControl:
    def __init__(self):
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._state = {'available': False, 'busy': False, 'device': None,
                       'physical_address': None, 'power': 'unknown',
                       'power_label': POWER_LABELS['unknown'], 'severity': 'info',
                       'message': 'Détection HDMI-CEC en cours…', 'source': 'not_requested',
                       'last_action': None, 'last_checked': None, 'last_error': None}
        self._thread = threading.Thread(target=self._worker, name='hdmi-cec', daemon=True)

    def start(self):
        self._thread.start()
        self.submit('check')

    def close(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=8)

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **values):
        if 'power' in values:
            values['power_label'] = POWER_LABELS[values['power']]
        with self._lock:
            self._state.update(values)

    def submit(self, action):
        if action not in ('wake', 'source', 'check'):
            raise ValueError('Commande TV inconnue.')
        with self._lock:
            if self._state['busy']:
                raise RuntimeError('Une commande TV est déjà en cours. Patientez quelques secondes.')
            self._state.update(busy=True, last_action=action, last_error=None,
                               severity='info', message='Commande TV en cours…')
        self._queue.put_nowait(action)

    def _run(self, args, timeout=5):
        if self._stop.is_set():
            raise RuntimeError('Arrêt du contrôle TV.')
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Le contrôle HDMI-CEC ne répond pas dans le délai prévu.') from None
        except PermissionError:
            raise RuntimeError('Accès HDMI-CEC refusé. Vérifiez les droits du compte du gestionnaire sur /dev/cec*.') from None
        except FileNotFoundError:
            raise RuntimeError('Outil cec-ctl absent. Installez le paquet v4l-utils sur le Pi.') from None
        text = (result.stdout + '\n' + result.stderr).strip()
        if result.returncode:
            if 'Permission denied' in text:
                raise RuntimeError('Accès HDMI-CEC refusé. Vérifiez l’appartenance du compte au groupe video.')
            raise RuntimeError('Commande HDMI-CEC refusée : ' + (text.splitlines()[-1][:250] if text else 'erreur sans détail'))
        return text

    def _discover(self):
        if not shutil.which('cec-ctl'):
            raise RuntimeError('Outil cec-ctl absent. Installez le paquet v4l-utils sur le Pi.')
        candidates = []
        errors = []
        for device in sorted(glob.glob('/dev/cec[0-9]*')):
            if not re.fullmatch(r'/dev/cec[0-9]+', device):
                continue
            try:
                info = self._run(['cec-ctl', '-d', device])
                match = re.search(r'^\s*Physical Address\s*:\s*([0-9a-fA-F](?:\.[0-9a-fA-F]){3})\s*$', info, re.M)
                if match and match.group(1).lower() != 'f.f.f.f':
                    candidates.append((device, match.group(1).lower(), info))
            except RuntimeError as error:
                errors.append(str(error))
        if not candidates:
            self._set(available=False, device=None, physical_address=None, power='unknown')
            raise RuntimeError(errors[0] if errors else 'Aucune TV détectée sur HDMI-CEC. Vérifiez le câble HDMI et activez le CEC dans les réglages de la TV.')
        # Retain the previously discovered attached adapter when there are several.
        previous = self.snapshot()['device']
        selected = next((item for item in candidates if item[0] == previous), candidates[0])
        device, address, info = selected
        self._set(available=True, device=device, physical_address=address)
        if re.search(r'Logical Address Mask\s*:\s*0x0+\b', info):
            self._run(['cec-ctl', '-d', device, '--playback', '-o', 'Kiosk'])
        return device, address

    def _send(self, device, *args):
        result = self._run(['cec-ctl', '-s', '-d', device, *args])
        if re.search(r'not acknowledged|transmit failed|tx,\s*(?:error|timeout)|nack|arbitration lost', result, re.I):
            raise RuntimeError('La TV n’accuse pas réception de la commande. Vérifiez que le HDMI-CEC est activé.')
        return result

    def _power(self, device):
        self._set(power='unknown', last_checked=timestamp())
        try:
            result = self._run(['cec-ctl', '-s', '-d', device, '--timeout', '1000', '-t', '0', '--give-device-power-status'])
        except RuntimeError as error:
            self._set(last_error=str(error))
            return 'unknown'
        found = re.search(r'pwr-state:\s*(to-standby|to-on|standby|on)\b', result, re.I)
        power = found.group(1).lower() if found else 'unknown'
        self._set(power=power, last_checked=timestamp())
        return power

    def _execute(self, action):
        device, address = self._discover()
        wake_sent = False
        if action in ('wake', 'source'):
            # Wake the host output as well as requesting TV power over CEC.
            wake_host_output()
            self._send(device, '-t', '0', '--image-view-on')
            wake_sent = True
            if self._stop.wait(2):
                return
        if action == 'source':
            self._send(device, '--active-source', 'phys-addr=' + address)
            self._send(device, '--set-stream-path', 'phys-addr=' + address)
            if self._stop.wait(1):
                return
            self._send(device, '--active-source', 'phys-addr=' + address)
            self._set(source='requested')
        power = self._power(device)
        if action == 'source':
            self._set(severity='warning', message=('La TV confirme qu’elle est allumée. ' if power == 'on' else '') +
                      'Sélection de l’entrée du kiosque demandée, non confirmée. Vérifiez l’image sur la TV.')
        elif power == 'on':
            self._set(severity='success', message='La TV confirme qu’elle est allumée.')
        elif power == 'standby':
            self._set(severity='warning', message='La TV est encore en veille. Réessayez le réveil ou vérifiez ses réglages HDMI-CEC.')
        elif power == 'to-on':
            self._set(severity='warning', message='La TV confirme un allumage en cours. Vérifiez à nouveau son état dans quelques secondes.')
        elif power == 'to-standby':
            self._set(severity='warning', message='La TV indique une mise en veille en cours.')
        else:
            self._set(severity='warning', message=('Commande de réveil transmise. ' if wake_sent else '') +
                      'La TV ne confirme pas son état. Vérifiez son écran et l’activation du HDMI-CEC.')

    def _worker(self):
        while not self._stop.is_set():
            try:
                action = self._queue.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                self._execute(action)
                LOG.info('Commande TV %s terminée : %s', action, self.snapshot()['message'])
            except Exception as error:
                LOG.exception('Commande TV %s non aboutie', action)
                self._set(power='unknown', severity='error', message=str(error), last_error=str(error), last_checked=timestamp())
            finally:
                self._set(busy=False)
                self._queue.task_done()
