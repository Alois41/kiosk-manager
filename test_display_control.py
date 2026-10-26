"""Display regression tests; no physical display commands are executed."""
import json
import tempfile
import unittest
from pathlib import Path

XRANDR = '''Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 8192 x 8192
HDMI-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 600mm x 340mm
   1920x1080     60.00*+  59.94
   1280x720      60.00    50.00
DP-1 disconnected (normal left inverted right x axis y axis)
VIRTUAL1 connected 1024x768+0+0
   1024x768      60.00*
'''


class ParserTests(unittest.TestCase):
    def test_connected_physical_modes_include_exact_current_refresh(self):
        from display_control import parse_xrandr
        outputs = parse_xrandr(XRANDR)
        self.assertEqual([o['name'] for o in outputs], ['HDMI-1'])
        self.assertEqual(outputs[0]['current'], {'mode': '1920x1080', 'rate': '60.00'})
        self.assertEqual(outputs[0]['modes'], [
            {'mode': '1920x1080', 'rate': '60.00'},
            {'mode': '1920x1080', 'rate': '59.94'},
            {'mode': '1280x720', 'rate': '60.00'},
            {'mode': '1280x720', 'rate': '50.00'}])


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []

    def run_display(self, args):
        self.calls.append(args)
        return XRANDR

    def control(self, env=None, runner=None):
        from display_control import DisplayControl
        return DisplayControl(self.root, runner=runner or self.run_display,
                              environ={'DISPLAY': ':0'} if env is None else env)

    def test_wayland_and_missing_display_never_invoke_xrandr(self):
        for env in ({'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0'},
                    {'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'wayland'}, {}):
            with self.subTest(env=env):
                control = self.control(env=env)
                control.refresh()
                self.assertFalse(control.snapshot()['supported'])
                self.assertTrue(control.snapshot()['message'])
        self.assertEqual(self.calls, [])

    def test_apply_verifies_then_persists_selected_mode(self):
        actual = XRANDR
        def runner(args):
            nonlocal actual
            self.calls.append(args)
            if '--output' in args:
                self.assertFalse((self.root / 'display-settings.json').exists())
                actual = XRANDR.replace('60.00*+', '60.00+').replace('60.00    50.00', '60.00*   50.00')
                return ''
            return actual
        control = self.control(runner=runner)
        selected = {'output': 'HDMI-1', 'mode': '1280x720', 'rate': '60.00'}
        result = control.configure(selected)
        self.assertEqual(result['outputs'][0]['current'], {'mode': '1280x720', 'rate': '60.00'})
        self.assertEqual(json.loads((self.root / 'display-settings.json').read_text()),
                         {'outputs': {'HDMI-1': {'mode': '1280x720', 'rate': '60.00'}}})
        self.assertEqual(self.calls, [['xrandr', '--current'],
            ['xrandr', '--output', 'HDMI-1', '--mode', '1280x720', '--rate', '60.00'],
            ['xrandr', '--current']])

    def test_invalid_selections_rejected_without_mode_command(self):
        for selection in (None, [], {}, {'output': 'HDMI-1', 'mode': '1280x720', 'rate': 60},
                          {'output': 'DP-1', 'mode': '1280x720', 'rate': '60.00'},
                          {'output': 'HDMI-1', 'mode': '9999x9999', 'rate': '60.00'},
                          {'output': 'HDMI-1', 'mode': '1280x720', 'rate': '59.94'},
                          {'output': '--help', 'mode': '1280x720', 'rate': '60.00'}):
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    self.control().configure(selection)
        self.assertFalse(any('--output' in call for call in self.calls))
        with self.assertRaises(RuntimeError):
            self.control(env={'WAYLAND_DISPLAY': 'wayland-0'}).configure(
                {'output': 'HDMI-1', 'mode': '1280x720', 'rate': '60.00'})

    def test_startup_restores_only_saved_modes_and_failure_is_nonfatal(self):
        self.control().restore()
        self.assertFalse(any('--output' in call for call in self.calls))
        self.calls.clear()
        path = self.root / 'display-settings.json'
        saved = {'outputs': {'HDMI-1': {'mode': '1920x1080', 'rate': '60.00'}}}
        path.write_text(json.dumps(saved))
        control = self.control()
        control.restore()
        self.assertTrue(any('--output' in call for call in self.calls))
        self.assertEqual(control.snapshot()['saved'], saved['outputs'])
        path.write_text('{broken')
        control = self.control()
        control.restore()
        self.assertTrue(control.snapshot()['error'])
        self.assertEqual(path.read_text(), '{broken')
        path.write_text(json.dumps(saved))
        def broken(args):
            raise OSError('Xorg unavailable')
        control = self.control(runner=broken)
        control.restore()
        self.assertIn('Xorg unavailable', control.snapshot()['error'])
        self.assertEqual(json.loads(path.read_text()), saved)

    def test_background_refresh_and_command_failure_are_safe(self):
        import threading
        from unittest.mock import patch
        entered, release = threading.Event(), threading.Event()
        def slow(args):
            entered.set()
            release.wait(2)
            return XRANDR
        control = self.control(runner=slow)
        control.start(interval=.02)
        self.assertTrue(entered.wait(1))
        self.assertEqual(control.snapshot()['outputs'], [])
        release.set()
        control.close()
        self.assertFalse(control._thread.is_alive())
        def failing(args):
            if '--output' in args:
                self.assertTrue(control.snapshot()['busy'])
                raise OSError('xrandr failed')
            return XRANDR
        control = self.control(runner=failing)
        with self.assertRaises(OSError):
            control.configure({'output': 'HDMI-1', 'mode': '1280x720', 'rate': '60.00'})
        self.assertFalse(control.snapshot()['busy'])
        self.assertIn('xrandr failed', control.snapshot()['error'])
        self.assertFalse(control.path.exists())
        control = self.control()
        with patch('display_control.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                control.configure({'output': 'HDMI-1', 'mode': '1920x1080', 'rate': '60.00'})
        self.assertEqual(control.snapshot()['saved'], {})
        self.assertEqual(list(self.root.glob('.display-*')), [])

    def test_unverified_apply_never_saves(self):
        control = self.control()
        with self.assertRaisesRegex(RuntimeError, 'confirm'):
            control.configure({'output': 'HDMI-1', 'mode': '1280x720', 'rate': '60.00'})
        self.assertFalse(control.path.exists())
        self.assertTrue(control.snapshot()['error'])
        self.assertFalse(control.snapshot()['busy'])

    def test_refresh_caches_status_without_changing_unconfigured_output(self):
        control = self.control()
        control.refresh()
        self.assertTrue(control.snapshot()['supported'])
        self.assertEqual(control.snapshot()['outputs'][0]['name'], 'HDMI-1')
        for _ in range(5):
            control.snapshot()
        self.assertEqual(self.calls, [['xrandr', '--current']])
        self.assertFalse((self.root / 'display-settings.json').exists())
        view = control.snapshot()
        view['outputs'].clear()
        self.assertEqual(len(control.snapshot()['outputs']), 1)


class UITests(unittest.TestCase):
    def test_french_display_controls_explicit_apply_and_status_hook(self):
        markup = Path(__file__).with_name('index.html').read_text()
        self.assertIn('id="display-controls"', markup)
        self.assertIn('id="display-message"', markup)
        self.assertIn('function renderDisplay(', markup)
        self.assertIn("api('/api/display/settings'", markup)
        self.assertIn('Résolution', markup)
        self.assertIn('Fréquence', markup)
        self.assertIn("renderDisplay(data.display)", markup)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        import kiosk_manager as manager
        from display_control import DisplayControl
        from http.server import ThreadingHTTPServer
        import threading
        from unittest.mock import patch
        self.manager = manager
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.actual = XRANDR
        def runner(args):
            if '--output' in args:
                self.actual = XRANDR.replace('60.00*+', '60.00+').replace('60.00    50.00', '60.00*   50.00')
                return ''
            return self.actual
        self.control = DisplayControl(root, runner=runner, environ={'DISPLAY': ':0'})
        self.control.refresh()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), manager.Handler)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        config = root / 'config.json'
        config.write_text('{"pages": []}')
        for name, value in (('PORT', self.port), ('CONFIG', config), ('DISPLAY', self.control)):
            p = patch.object(manager, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, path, data=None, token=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        hdr = {} if headers is None else dict(headers)
        if token is not None:
            hdr['X-Kiosk-Token'] = token
        body = json.dumps(data) if data is not None else None
        conn.request('POST' if data is not None else 'GET', path, body, hdr)
        response = conn.getresponse()
        raw = response.read().decode()
        result = json.loads(raw) if 'json' in response.getheader('Content-Type', '') else raw
        conn.close()
        return response.status, result

    def test_display_http_apply_and_status_preserve_pages_and_audio(self):
        code, result = self.request('/api/display')
        self.assertEqual(code, 200)
        self.assertEqual(result, self.control.snapshot())
        selection = {'output': 'HDMI-1', 'mode': '1280x720', 'rate': '60.00'}
        self.assertEqual(self.request('/api/display/settings', selection)[0], 403)
        self.assertEqual(self.request('/api/display/settings', selection, 'wrong')[0], 403)
        self.assertEqual(self.request('/api/display/settings', selection, self.manager.TOKEN,
                                     {'Origin': 'https://evil.invalid'})[0], 403)
        code, result = self.request('/api/display/settings', selection, self.manager.TOKEN)
        self.assertEqual(code, 200)
        self.assertTrue(result['ok'])
        self.assertEqual(result['display']['outputs'][0]['current']['mode'], '1280x720')
        self.assertEqual(self.request('/api/display')[1], result['display'])
        status = self.request('/api/status')[1]
        self.assertEqual(status['display'], result['display'])
        self.assertIn('audio', status)
        self.assertEqual(status['config'], {'pages': []})
        self.assertEqual(self.request('/api/display/settings', {'output': '--help'}, self.manager.TOKEN)[0], 400)
        self.assertEqual(self.request('/api/config', {'pages': [], 'display': {}}, self.manager.TOKEN)[0], 400)


if __name__ == '__main__':
    unittest.main()
