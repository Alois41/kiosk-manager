"""Regression tests for URL updates in Chromium fullscreen mode."""
import unittest
from unittest.mock import patch
import kiosk_manager as manager

class NavigationTests(unittest.TestCase):
    def exercise(self, fullscreen=True, fail=False):
        events=[]
        def run(args, **kwargs):
            if args[0]=='xprop':return '_NET_WM_STATE_FULLSCREEN' if fullscreen else ''
            events.append(('type',args[-1]))
            if fail:raise OSError('typing failed')
            return ''
        with patch.object(manager,'run',side_effect=run),patch.object(manager,'key',side_effect=lambda win,value:events.append(('key',value))),patch.object(manager,'fullscreen',side_effect=lambda win,value:events.append(('fullscreen',value))),patch.object(manager.time,'sleep'):
            if fail:
                with self.assertRaises(OSError):manager.navigate('123','https://example.org/new')
            else:manager.navigate('123','https://example.org/new')
        return events

    def test_fullscreen_url_update_exposes_omnibox_before_typing_then_restores(self):
        self.assertEqual(self.exercise(),[('fullscreen',False),('key','ctrl+l'),('type','https://example.org/new'),('key','Return'),('fullscreen',True)])

    def test_windowed_url_update_does_not_toggle_fullscreen(self):
        self.assertEqual(self.exercise(False),[('key','ctrl+l'),('type','https://example.org/new'),('key','Return')])

    def test_typing_failure_restores_original_fullscreen(self):
        self.assertEqual(self.exercise(fail=True)[-1],('fullscreen',True))

if __name__=='__main__':unittest.main()
