"""HTTP and UI tests for paired LAN management; no real TV changes."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
import kiosk_manager as manager

PEER='b'*32

class ClusterHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config=Path(self.tmp.name)/'config.json';config.write_text('{"pages": []}')
        self.lan=Mock()
        self.lan.snapshot.return_value={'enabled':True,'self':{'id':'a'*32,'name':'Local'},'peers':[{'id':PEER,'name':'Remote','online':True}],'error':''}
        self.lan.request.return_value=(200,b'{"remote":true}','application/json')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),manager.Handler)
        self.addCleanup(self.server.server_close)
        self.port=self.server.server_address[1]
        for name,value in [('PORT',self.port),('CONFIG',config),('LAN',self.lan)]:
            p=patch.object(manager,name,value,create=True);p.start();self.addCleanup(p.stop)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(self.server.shutdown)

    def request(self,path,body=None,headers=None,method=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=4)
        headers=dict(headers or {})
        if isinstance(body,dict):body=json.dumps(body).encode()
        conn.request(method or ('POST' if body is not None else 'GET'),path,body,headers)
        result=conn.getresponse();raw=result.read();conn.close()
        return result.status,json.loads(raw)

    def test_discovery_status_available_without_disclosing_pairing_key(self):
        status,data=self.request('/api/cluster')
        self.assertEqual(status,200)
        self.assertEqual(data,self.lan.snapshot.return_value)
        self.assertNotIn('key',data)

    def test_proxy_reads_and_token_protected_writes_use_selected_peer(self):
        status,data=self.request('/api/peers/'+PEER+'/status')
        self.assertEqual((status,data),(200,{'remote':True}))
        self.lan.request.assert_called_with(PEER,'GET','/api/status',b'','application/json','')
        path='/api/peers/'+PEER+'/config'
        self.assertEqual(self.request(path,{'pages':[]})[0],403)
        self.assertEqual(self.request(path,{'pages':[]},{'X-Kiosk-Token':manager.TOKEN,'Origin':'https://evil.invalid'})[0],403)
        status,data=self.request(path,{'pages':[]},{'X-Kiosk-Token':manager.TOKEN})
        self.assertEqual(status,200)
        self.assertEqual(self.lan.request.call_args.args[:3],(PEER,'POST','/api/config'))
        self.assertEqual(json.loads(self.lan.request.call_args.args[3]),{'pages':[]})
        self.lan.request.side_effect=RuntimeError('offline')
        self.assertEqual(self.request('/api/peers/'+PEER+'/status')[0],503)

    def test_signed_relay_requires_peer_auth_and_can_only_call_local_api(self):
        self.lan.verify.side_effect=PermissionError('bad signature')
        self.assertEqual(self.request('/api/cluster/relay',b'')[0],403)
        self.lan.verify.side_effect=None
        self.lan.verify.return_value=('GET','/api/status','application/json','')
        status,data=self.request('/api/cluster/relay',b'')
        self.assertEqual(status,200)
        self.assertEqual(data['config'],{'pages':[]})
        self.assertIn('audio',data)
        self.lan.verify.return_value=('GET','http://evil.invalid','application/json','')
        self.assertEqual(self.request('/api/cluster/relay',b'')[0],400)

class ClusterUITests(unittest.TestCase):
    def test_page_button_toggles_play_pause(self):
        markup=Path(__file__).with_name('index.html').read_text()
        self.assertNotIn("connect.textContent='Connexion'",markup)
        self.assertIn("paused?'Play':'Pause'",markup)
        self.assertIn("await act('kiosk')",markup)
        self.assertIn("'En pause'",markup)

    def test_same_origin_tv_selection_routes_all_mutations_to_fixed_target(self):
        markup=Path(__file__).with_name('index.html').read_text()
        self.assertIn('id="tv-selector"',markup)
        self.assertIn('id="target-banner"',markup)
        self.assertIn('const targetId=',markup)
        self.assertIn("fetch(targetPath(path)",markup)
        self.assertIn("fetch(targetPath('/api/audio/upload')",markup)
        self.assertIn("fetch(targetPath('/api/config')",markup)
        self.assertIn("api('/api/cluster')",markup)
        self.assertIn('location.assign',markup)

if __name__=='__main__':unittest.main()
