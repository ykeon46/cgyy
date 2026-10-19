import json
from datetime import datetime,timedelta
import os
from http.server import ThreadingHTTPServer
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import web_app
from playwright.sync_api import sync_playwright


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),web_app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close()
    def test_login_is_required_for_job_and_configuration_data(self):
        for endpoint in ['/api/jobs','/api/config']:
            with self.subTest(endpoint=endpoint),self.assertRaises(HTTPError) as error:urlopen(self.url+endpoint)
            self.assertEqual(error.exception.code,401)
    def test_mutations_reject_cross_origin_requests(self):
        cases=[
            {'Host':'localhost:50293','Origin':'https://example.test','X-CGYY-Client':'1'},
            {'Host':'localhost:50293','Origin':'http://localhost:8765','X-CGYY-Client':'1'},
            {'Host':'example.test:50293','Origin':'http://example.test:50293','X-CGYY-Client':'1'},
            {'Host':'localhost:50293','Origin':'null','X-CGYY-Client':'1'},
            {'Host':'localhost:50293','Origin':'http://localhost:50293'},
        ]
        with patch('web_app.spawn_worker') as worker:
            for headers in cases:
                request=Request(self.url+'/api/login',data=b'{}',headers={'Content-Type':'application/json',**headers})
                with self.subTest(headers=headers),self.assertRaises(HTTPError) as error:urlopen(request)
                self.assertEqual(error.exception.code,403)
            worker.assert_not_called()
    def test_login_accepts_same_origin_on_a_forwarded_local_port(self):
        with patch.dict(web_app.SESSIONS,{},clear=True),patch('web_app.spawn_worker',return_value={'username':'forward-test','buddies':[]}) as worker:
            request=Request(self.url+'/api/login',data=json.dumps({'username':'forward-test','password':'test-password'}).encode(),headers={
                'Content-Type':'application/json','X-CGYY-Client':'1',
                'Host':'localhost:50293','Origin':'http://localhost:50293',
            })
            with urlopen(request) as response:
                self.assertEqual(json.load(response)['session']['username'],'forward-test')
                self.assertIn('HttpOnly',response.headers.get('Set-Cookie'))
            worker.assert_called_once()
    def test_configuration_excludes_credentials_and_rejects_shahe(self):
        config=web_app.validate_config({'password':'must-not-save','venues':web_app.load_config(web_app.DEFAULT_CONFIG)['venues']})
        self.assertNotIn('password',config)
        self.assertEqual(config['sso'],{'user':'','passwd':''})
        with self.assertRaises(ValueError):web_app.validate_config({'venues':['沙河校区-羽毛球']})
    def test_invalid_slots_and_amount_never_start_a_job(self):
        for change in [{'reserve_time':[20,22]},{'reserve_time':[True]},{'max_pay_amount':'NaN'},{'priority':[0]}]:
            with self.subTest(change=change),self.assertRaises(ValueError):web_app.validate_config(change)
    def test_public_session_does_not_return_school_password(self):
        data=json.load(urlopen(self.url+'/api/session'))
        self.assertIsNone(data['session']);self.assertNotIn('password',data)
    def test_saved_login_time_updates_daily_timer(self):
        config=web_app.load_config(web_app.DEFAULT_CONFIG)
        config['daily_schedule']['login_at']='06:25:00'
        with tempfile.TemporaryDirectory() as directory,patch('web_app.Path.home',return_value=Path(directory)),patch('web_app.subprocess.run'):
            web_app.apply_schedule(config,True)
            timer=(Path(directory)/'.config/systemd/user/cgyy-daily.timer').read_text()
            self.assertIn('06:25:00 Asia/Shanghai',timer)
    def test_valid_login_sets_http_only_cookie_without_password_response(self):
        with patch('web_app.spawn_worker',return_value={'username':'test-user','buddies':[]}):
            request=Request(self.url+'/api/login',data=json.dumps({'username':'test-user','password':'not-returned'}).encode(),headers={'Content-Type':'application/json','X-CGYY-Client':'1'})
            with urlopen(request) as response:
                self.assertIn('HttpOnly',response.headers.get('Set-Cookie'))
                self.assertNotIn('not-returned',response.read().decode())


class BrowserUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),web_app.Handler)
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
        cls.playwright=sync_playwright().start();options={'headless':True}
        if os.getenv('CGYY_BROWSER_BINARY'):options['executable_path']=os.environ['CGYY_BROWSER_BINARY']
        cls.browser=cls.playwright.chromium.launch(**options)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop();cls.server.shutdown();cls.server.server_close()
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':1360,'height':1100})
        self.page=self.context.new_page();self.logged=False;self.jobs=[];self.posts=[];self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.route('**/api/**',self.route)
    def tearDown(self):self.context.close()
    def route(self,route):
        path=route.request.url.split('/api/')[-1]
        now=datetime.now(web_app.CHINA).isoformat()
        session={'username':'test-school-user','buddies':[{'index':1,'name':'测试同伴'}]}
        if route.request.method=='POST':
            self.posts.append((path,route.request.post_data_json))
            if path=='login':self.logged=True;result={'session':session}
            else:result={'ok':True,'id':'test-job'}
        elif path=='session':result={'session':session if self.logged else None,'server_now':now}
        elif path=='config':result={'config':web_app.load_config(web_app.DEFAULT_CONFIG),'daily_enabled':True}
        elif path=='jobs':result={'jobs':self.jobs,'server_now':now}
        else:result={}
        route.fulfill(json=result)
    def login(self):
        self.page.goto(self.url)
        self.page.locator('#username').fill('test-school-user');self.page.locator('#password').fill('test-school-password')
        self.page.locator('#login-button').click();self.page.locator('#dashboard').wait_for(state='visible')
    def test_login_and_config_controls_are_connected(self):
        self.login()
        self.page.locator('#end-hour').select_option('21')
        self.page.locator('[name=venue][value=main]').uncheck()
        self.page.locator('#save-config').click()
        self.page.wait_for_function("()=>document.querySelector('#toast').textContent.includes('设置已保存')")
        request=next(value for key,value in self.posts if key=='config')
        self.assertEqual(request['config']['reserve_time'],[20])
        self.assertEqual(request['config']['venues'],[web_app.load_config(web_app.DEFAULT_CONFIG)['venues'][1]])
        self.assertEqual(self.errors,[])
    def test_expired_qr_is_hidden_and_mobile_does_not_overflow(self):
        self.jobs=[{'id':'expired-test','status':'awaiting_payment','trade_no':'test-trade','expires_at':(datetime.now(web_app.CHINA)-timedelta(seconds=1)).isoformat(),'qr_code':'data:image/png;base64,aGVsbG8=','updated_at':datetime.now(web_app.CHINA).isoformat()}]
        self.page.set_viewport_size({'width':390,'height':844})
        self.login();self.page.wait_for_timeout(300)
        self.assertFalse(self.page.locator('#payment-qr').is_visible())
        self.assertEqual(self.page.locator('#countdown').inner_text(),'00:00')
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
