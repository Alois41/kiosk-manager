"""Reset saved tabs only, after Chromium exits; never touch login databases."""
import json
import os
from pathlib import Path
import re
import shutil
import socket

SESSION_FILE = re.compile(r'(?:Session|Tabs)_[0-9]+\Z')
LEGACY_FILES = ('Current Session', 'Current Tabs', 'Last Session', 'Last Tabs')


def ensure_profile_closed(profile):
    lock = Path(profile) / 'SingletonLock'
    if not lock.is_symlink():
        if lock.exists():
            raise RuntimeError('Le profil Chromium présente un verrou inattendu. Fermez le navigateur avant de relancer le kiosque.')
        return
    owner = os.readlink(lock)
    try:
        hostname, raw_pid = owner.rsplit('-', 1)
        pid = int(raw_pid)
    except (ValueError, TypeError):
        raise RuntimeError('Le verrou du profil Chromium est illisible. Vérifiez qu’aucun autre navigateur n’utilise ce profil.') from None
    if hostname != socket.gethostname():
        raise RuntimeError('Le profil Chromium est verrouillé par une autre machine. Ne le partagez pas entre deux cartes.')
    proc = Path('/proc') / str(pid)
    try:
        if proc.exists() and proc.stat().st_uid == os.getuid():
            status = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[0]
            if status != 'Z':
                raise RuntimeError('Un autre Chromium utilise déjà ce profil. Fermez-le avant de relancer le kiosque ; aucun onglet supplémentaire n’a été ouvert.')
    except (FileNotFoundError, ProcessLookupError):
        pass


def prepare_tab_session(profile, backup):
    """Archive the previous tab-state generation; retain cookies and storage."""
    profile, backup = Path(profile), Path(backup)
    ensure_profile_closed(profile)
    if not profile.exists():
        return {'saved_state_files_moved': 0, 'profiles': []}
    profiles = [p for p in profile.iterdir() if p.is_dir() and
                (p.name == 'Default' or re.fullmatch(r'Profile [0-9]+', p.name))]
    candidates = []
    for browser_profile in profiles:
        session_dir = browser_profile / 'Sessions'
        if session_dir.is_dir():
            candidates.extend((browser_profile.name, p) for p in session_dir.iterdir()
                              if p.is_file() and not p.is_symlink() and SESSION_FILE.fullmatch(p.name))
        candidates.extend((browser_profile.name, browser_profile / name) for name in LEGACY_FILES
                          if (browser_profile / name).is_file() and not (browser_profile / name).is_symlink())
    if not candidates:
        return {'saved_state_files_moved': 0, 'profiles': []}
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup.chmod(0o700)
    # Retain one private recovery generation, rather than accumulating old tabs.
    for previous in backup.iterdir():
        if previous.is_file() and not previous.is_symlink() and (previous.name.startswith('tabstate__') or previous.name == 'manifest.json'):
            previous.unlink()
    moved = []
    for profile_name, source in candidates:
        destination = backup / ('tabstate__' + profile_name + '__' + source.name)
        shutil.move(str(source), str(destination))
        destination.chmod(0o600)
        moved.append({'profile': profile_name, 'original': str(source), 'backup': destination.name})
    report = {'saved_state_files_moved': len(moved), 'profiles': sorted({item['profile'] for item in moved}), 'files': moved}
    manifest = backup / 'manifest.json'
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    manifest.chmod(0o600)
    return report
