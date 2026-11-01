import json
from datetime import datetime,timedelta
import os
import shutil
from http.server import ThreadingHTTPServer
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError

import web_app
from playwright.sync_api import sync_playwright

# Local test traffic must bypass any system HTTP proxy, including requests
# with a forwarded Host header that differs from the connection address.
urlopen = build_opener(ProxyHandler({})).open


class WebTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        shutil.copytree(web_app.ROOT/'deploy',self.root/'deploy')
        for name,value in [('ROOT',self.root),('DATA',self.root/'artifacts/web'),('CONFIG',self.root/'artifacts/web/config.yaml')]:
            patcher=patch.object(web_app,name,value);patcher.start();self.addCleanup(patcher.stop)
        for patcher in [patch('web_app.local_environment',return_value={}),
                        patch.dict(web_app.SESSIONS,{},clear=True),patch.dict(web_app.PROCESSES,{},clear=True)]:
            patcher.start();self.addCleanup(patcher.stop)

    def request(self,path,body=None,token='test-token'):
        headers={'X-CGYY-Client':'1','Cookie':'cgyy_session='+token,'Content-Type':'application/json'}
        with urlopen(Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)) as response:
            return json.load(response)

    def job(self,task_id,status='needs_attention',username='account-a',**details):
        path=web_app.DATA/'jobs'/task_id/'status.json'
        state=dict(status=status,username=username,updated_at=datetime.now(web_app.CHINA).isoformat())
        state.update(details)
        web_app.write_json(path,state)
        return path

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
    def test_config_load_and_save_without_systemd(self):
        web_app.SESSIONS['test-token']={'username':'account-a','buddies':[]}
        with patch('web_app.subprocess.run',side_effect=FileNotFoundError('systemctl')):
            result=self.request('/api/config')
            self.assertFalse(result['daily_enabled'])
            self.assertIn('reserve_time',result['config'])
            self.assertTrue(self.request('/api/config',{'config':{},'daily_enabled':False})['ok'])
            self.assertTrue(web_app.CONFIG.is_file())

    def test_enable_daily_without_systemd_returns_clear_error(self):
        web_app.SESSIONS['test-token']={'username':'account-a','buddies':[]}
        with patch('web_app.Path.home',return_value=self.root),patch('web_app.subprocess.run',side_effect=FileNotFoundError('systemctl')):
            with self.assertRaises(HTTPError) as error:
                self.request('/api/config',{'config':{},'daily_enabled':True})
            self.assertEqual(error.exception.code,400)
            self.assertIn('当前系统不支持每日自动预约',json.load(error.exception)['error'])

    def test_account_switch_without_systemd(self):
        with patch('web_app.subprocess.run',side_effect=FileNotFoundError('systemctl')):
            web_app.prepare_account_switch('account-a')
        self.assertTrue(web_app.account_config_path('account-a').is_file())
    def test_valid_login_sets_http_only_cookie_without_password_response(self):
        with patch('web_app.spawn_worker',return_value={'username':'test-user','buddies':[]}):
            request=Request(self.url+'/api/login',data=json.dumps({'username':'test-user','password':'not-returned'}).encode(),headers={'Content-Type':'application/json','X-CGYY-Client':'1'})
            with urlopen(request) as response:
                self.assertIn('HttpOnly',response.headers.get('Set-Cookie'))
                self.assertNotIn('not-returned',response.read().decode())
    def test_miniprogram_requires_configured_access_key(self):
        headers={'Content-Type':'application/json','X-CGYY-Client':'miniprogram','X-CGYY-Key':'k'*24}
        for configured in [{},{'CGYY_MINIPROGRAM_KEY':'short'},{'CGYY_MINIPROGRAM_KEY':'x'*24}]:
            with self.subTest(configured=configured),patch('web_app.local_environment',return_value=configured),patch('web_app.spawn_worker') as worker:
                for request in [Request(self.url+'/api/session',headers=headers),
                                Request(self.url+'/api/login',data=json.dumps({'username':'u','password':'p'}).encode(),headers=headers)]:
                    with self.assertRaises(HTTPError) as error:urlopen(request)
                    self.assertEqual(error.exception.code,403)
                worker.assert_not_called()
    def test_miniprogram_login_returns_bearer_token_without_cookie(self):
        key='k'*24;headers={'Content-Type':'application/json','X-CGYY-Client':'miniprogram','X-CGYY-Key':key}
        with patch.dict(web_app.SESSIONS,{},clear=True),patch('web_app.local_environment',return_value={'CGYY_MINIPROGRAM_KEY':key}),\
             patch('web_app.spawn_worker',return_value={'username':'mp-user','buddies':[]}):
            request=Request(self.url+'/api/login',data=json.dumps({'username':'mp-user','password':'not-returned'}).encode(),headers=headers)
            with urlopen(request) as response:
                self.assertIsNone(response.headers.get('Set-Cookie'))
                data=json.load(response)
            self.assertEqual(data['session']['username'],'mp-user');self.assertNotIn('not-returned',json.dumps(data))
            with urlopen(Request(self.url+'/api/jobs',headers={**headers,'Authorization':'Bearer '+data['token']})) as response:
                self.assertIn('jobs',json.load(response))
            for changed in [{'Authorization':'Bearer wrong'},{'Authorization':'Bearer '+data['token'],'X-CGYY-Key':'w'*24}]:
                with self.subTest(changed=changed),self.assertRaises(HTTPError) as error:urlopen(Request(self.url+'/api/jobs',headers={**headers,**changed}))
                self.assertIn(error.exception.code,{401,403})
            # 浏览器Cookie不能冒充小程序的Bearer令牌。
            with self.assertRaises(HTTPError) as error:urlopen(Request(self.url+'/api/jobs',headers={**headers,'Cookie':'cgyy_session='+data['token']}))
            self.assertEqual(error.exception.code,401)

    def test_account_history_and_mutations_are_isolated(self):
        web_app.SESSIONS['test-token']={'username':'account-b','buddies':[]}
        self.job('a-task',trade_no='a-trade')
        self.job('b-task',status='paid',username='account-b')
        self.assertEqual([job['id'] for job in self.request('/api/jobs')['jobs']],['b-task'])
        with patch('web_app.spawn_worker') as worker:
            for endpoint,body in [('/api/recheck',{'id':'a-task'}),('/api/cancel',{'id':'a-task'}),('/api/resolve',{'id':'a-task','confirmed_no_order':True})]:
                with self.subTest(endpoint=endpoint),self.assertRaises(HTTPError) as error:self.request(endpoint,body)
                self.assertEqual(error.exception.code,400)
            worker.assert_not_called()

    def test_resolve_requires_explicit_confirmation_and_no_order(self):
        web_app.SESSIONS['test-token']={'username':'account-a','buddies':[]}
        path=self.job('uncertain')
        for confirmation in [None,False,'true']:
            with self.subTest(confirmation=confirmation),self.assertRaises(HTTPError):
                self.request('/api/resolve',{'id':'uncertain','confirmed_no_order':confirmation})
        self.request('/api/resolve',{'id':'uncertain','confirmed_no_order':True})
        state=json.loads(path.read_text())
        self.assertEqual(state['status'],'resolved')
        self.assertEqual(state['resolved_by'],'account-a')
        self.assertEqual(state['previous_status'],'needs_attention')
        self.job('with-order',trade_no='real-order')
        self.job('running',status='submitting')
        for task_id in ['with-order','running']:
            with self.subTest(task_id=task_id),self.assertRaises(HTTPError):
                self.request('/api/resolve',{'id':task_id,'confirmed_no_order':True})

    def test_resolve_cannot_race_with_running_daily_worker(self):
        web_app.SESSIONS['test-token']={'username':'account-a','buddies':[]}
        self.job('uncertain')
        with web_app.execution_lock(),self.assertRaises(HTTPError):
            self.request('/api/resolve',{'id':'uncertain','confirmed_no_order':True})
        self.assertEqual(web_app.job_states()[0]['status'],'needs_attention')

    def test_account_switch_archives_legacy_data_and_restores_own_config(self):
        config=web_app.load_config(web_app.DEFAULT_CONFIG)
        config['phone']='13800000001'
        web_app.save_config(web_app.CONFIG,config)
        path=self.job('legacy-paid',status='paid');state=json.loads(path.read_text());state.pop('username');web_app.write_json(path,state)
        with patch('web_app.local_environment',return_value={'BUAA_SSO_USER':'account-a'}),patch('web_app.subprocess.run') as systemctl:
            web_app.prepare_account_switch('account-a')
            web_app.activate_account('account-b','account-a')
            systemctl.assert_called_once()
        self.assertEqual(json.loads(path.read_text())['username'],'account-a')
        self.assertEqual(web_app.load_config(web_app.CONFIG)['phone'],'')
        with patch('web_app.local_environment',return_value={'BUAA_SSO_USER':'account-b'}),patch('web_app.subprocess.run'):
            self.assertEqual(web_app.job_states('account-b'),[])
            web_app.prepare_account_switch('account-b')
            web_app.activate_account('account-a','account-b')
        self.assertEqual(web_app.load_config(web_app.CONFIG)['phone'],'13800000001')
        self.assertEqual(web_app.load_config(web_app.CONFIG)['account_username'],'account-a')

    def test_account_switch_rejects_unresolved_task_before_login(self):
        self.job('uncertain')
        with patch('web_app.local_environment',return_value={'BUAA_SSO_USER':'account-a'}),patch('web_app.spawn_worker') as worker:
            with self.assertRaises(HTTPError):self.request('/api/login',{'username':'account-b','password':'dummy'})
            worker.assert_not_called()

    def test_login_switch_disables_timer_and_keeps_old_history_private(self):
        environment={'BUAA_SSO_USER':'account-a'}
        config=web_app.load_config(web_app.DEFAULT_CONFIG)
        config['phone']='13800000001'
        web_app.save_config(web_app.CONFIG,config)
        self.job('old-paid',status='paid')
        def login(action,request):
            self.assertEqual(action,'login')
            environment['BUAA_SSO_USER']=request['username']
            return {'username':request['username'],'buddies':[]}
        with patch('web_app.local_environment',side_effect=lambda:dict(environment)),patch('web_app.spawn_worker',side_effect=login),patch('web_app.subprocess.run') as timer:
            result=self.request('/api/login',{'username':'account-b','password':'dummy'})
            self.assertEqual(result['session']['username'],'account-b')
            token=next(iter(web_app.SESSIONS))
            self.assertEqual(self.request('/api/jobs',token=token)['jobs'],[])
            self.assertEqual(self.request('/api/config',token=token)['config']['phone'],'')
            self.assertIn(['systemctl','--user','disable','--now','cgyy-daily.timer'],[call.args[0] for call in timer.call_args_list])

    def test_account_switch_aborts_if_daily_timer_cannot_be_disabled(self):
        import subprocess
        with patch('web_app.local_environment',return_value={'BUAA_SSO_USER':'account-a'}),\
                patch('web_app.subprocess.run',side_effect=subprocess.CalledProcessError(1,'systemctl')),\
                patch('web_app.spawn_worker') as worker:
            with self.assertRaises(HTTPError):self.request('/api/login',{'username':'account-b','password':'dummy'})
            worker.assert_not_called()

    def test_older_unresolved_task_blocks_booking_until_manually_resolved(self):
        web_app.SESSIONS['test-token']={'username':'account-a','buddies':[]}
        self.job('old-uncertain',updated_at='2000-01-01T00:00:00+08:00')
        self.job('recent-paid',status='paid')
        run_at=(datetime.now(web_app.CHINA)+timedelta(days=1)).replace(hour=7,minute=0,second=0).isoformat()
        body={'mode':'once','run_at':run_at,'config':{}}
        with patch('web_app.spawn_worker') as worker:
            with self.assertRaises(HTTPError):self.request('/api/jobs',body)
            worker.assert_not_called()
            self.request('/api/resolve',{'id':'old-uncertain','confirmed_no_order':True})
            result=self.request('/api/jobs',body)
            self.assertIn('id',result)
            worker.assert_called_once()

    def test_account_switch_rejects_running_process_even_before_state_update(self):
        from unittest.mock import Mock
        web_app.PROCESSES['starting']=Mock();web_app.PROCESSES['starting'].poll.return_value=None
        with patch('web_app.spawn_worker') as worker:
            with self.assertRaises(HTTPError):self.request('/api/login',{'username':'account-b','password':'dummy'})
            worker.assert_not_called()

    def test_unresolved_tasks_remain_visible_beyond_history_limit(self):
        self.job('old-uncertain',updated_at='2000-01-01T00:00:00+08:00')
        for index in range(31):self.job('paid-'+str(index),status='paid')
        self.assertIn('old-uncertain',[job['id'] for job in web_app.job_states('account-a')])

    def test_config_ignores_client_account_and_is_saved_for_session_owner(self):
        web_app.SESSIONS['test-token']={'username':'account-a','buddies':[]}
        with patch('web_app.apply_schedule'):
            self.request('/api/config',{'config':{'account_username':'account-b'},'daily_enabled':False})
        self.assertEqual(web_app.load_config(web_app.CONFIG)['account_username'],'account-a')
        self.assertTrue(web_app.account_config_path('account-a').exists())
        self.assertFalse(web_app.account_config_path('account-b').exists())


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

    def test_native_payment_qr_is_distinguished_from_cashier_entry(self):
        self.jobs=[{'id':'native-qr-test','status':'awaiting_payment','trade_no':'test-trade',
                    'qr_kind':'alipay_payment','scan_tip':'请使用已登录的手机支付宝扫码，在手机上确认付款。',
                    'expires_at':(datetime.now(web_app.CHINA)+timedelta(seconds=50)).isoformat(),
                    'qr_code':'data:image/png;base64,aGVsbG8=','updated_at':datetime.now(web_app.CHINA).isoformat()}]
        self.login()
        self.page.wait_for_function("()=>document.querySelector('#payment-title').textContent==='支付宝付款码'")
        self.assertEqual(self.page.locator('#payment-title').inner_text(),'支付宝付款码')
        self.assertIn('手机支付宝',self.page.locator('#payment-subtitle').inner_text())
        self.assertEqual(self.page.locator('#payment-qr').get_attribute('alt'),'支付宝交易付款二维码')

    def test_payment_entry_updates_to_native_code_without_another_submission(self):
        self.jobs=[{'id':'progressive-qr','status':'awaiting_payment','trade_no':'test-trade',
                    'qr_kind':'campus_cashier','expires_at':(datetime.now(web_app.CHINA)+timedelta(seconds=50)).isoformat(),
                    'qr_code':'data:image/png;base64,aGVsbG8=','updated_at':datetime.now(web_app.CHINA).isoformat()}]
        self.login()
        self.page.wait_for_function("()=>document.querySelector('#payment-title').textContent==='校园付收银台入口码'")
        self.jobs[0].update(qr_kind='alipay_payment',qr_code='data:image/png;base64,bmV3',scan_tip='请使用手机支付宝扫码。')
        self.page.wait_for_function("()=>document.querySelector('#payment-title').textContent==='支付宝付款码'",timeout=2000)
        self.assertEqual(self.page.locator('#payment-qr').get_attribute('src'),'data:image/png;base64,bmV3')
        self.assertFalse(any(path=='jobs' for path,body in self.posts))

    def test_progress_marks_login_and_qr_generation_as_current_steps(self):
        now=datetime.now(web_app.CHINA).isoformat()
        self.jobs=[{'id':'progress','status':'logging_in','message':'正在恢复学校登录态。',
                    'phase_started_at':now,'updated_at':now}]
        self.login()
        self.page.locator('#step-login.current').wait_for()
        self.assertNotIn('current',self.page.locator('#step-book').get_attribute('class') or '')
        self.jobs[0].update(status='locked',trade_no='test-trade',message='正在获取官方付款入口。')
        self.page.locator('#step-pay.current').wait_for(timeout=2000)
        self.assertIn('正在获取',self.page.locator('#step-pay').inner_text())
        self.assertFalse(self.page.locator('#payment-qr').is_visible())

    def test_pending_payment_remains_visible_when_history_is_selected(self):
        now=datetime.now(web_app.CHINA)
        self.jobs=[{'id':'pending','status':'awaiting_payment','trade_no':'current-trade',
                    'qr_kind':'alipay_payment','expires_at':(now+timedelta(seconds=50)).isoformat(),
                    'qr_code':'data:image/png;base64,aGVsbG8=','updated_at':now.isoformat()},
                   {'id':'history','status':'expired','updated_at':now.isoformat()}]
        self.login();self.page.locator('#nav-history').click()
        self.page.locator('#history-list button').nth(1).click()
        self.assertTrue(self.page.locator('#payment-qr').is_visible())
        self.assertEqual(self.page.locator('#payment-title').inner_text(),'支付宝付款码')


if __name__=='__main__':unittest.main()
