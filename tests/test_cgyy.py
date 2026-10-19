"""浏览器回归：场地重排、跨页连续时段、日期接口和下单响应。"""
from datetime import date, datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import yaml
from playwright.sync_api import sync_playwright

from cgyy import DEFAULT_CONFIG, ReservationError, SubmissionRejected, VenueClient, available_choice, load_config, target_date

# 使用当前网页实际的类名和接口前缀，场地顺序故意与编号相反。
HTML = '''<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="date_box"><div class="active"><span>10月19日</span></div><div><span>10月20日</span></div></div>
<div id="scrollTable"></div>
<div class="xieyi"><input type="checkbox"></div>
<div class="submit_order_box"><div class="action"><div class="btn">提交</div></div></div>
<script>
let current = 0, selected = new Set();
function render() {
 const hours = current === 0 ? [10,11] : [12,13];
 document.querySelector('#scrollTable').innerHTML = '<table><thead><tr><td>场地</td>' + hours.map(h=>'<td>'+h+':00-'+(h+1)+':00</td>').join('') + '</tr></thead><tbody>' + [2,1].map(c=>'<tr><td>'+c+'号</td>'+hours.map(h=>'<td><div class="reserveBlock position '+(c===2&&h===12?'reserved':'free')+(selected.has(c+"_"+h)?' selected':'')+'" data-key="'+c+'_'+h+'">￥15.00</div></td>').join('')+'</tr>').join('')+'</tbody></table>' + '<i class="ivu-icon-ios-arrow-'+(current===0?'forward':'back')+'">翻页</i>';
 document.querySelector('#scrollTable i').onclick=()=>{ current=1-current; render(); };
 document.querySelectorAll('.reserveBlock').forEach(b=>b.onclick=()=>{if(b.classList.contains('free')){selected.add(b.dataset.key);b.classList.add('selected');}});
}
render();
document.querySelectorAll('.date_box > div').forEach(d=>d.onclick=async()=>{
 await fetch('/venue-server/api/reservation/day/info');
 document.querySelectorAll('.date_box > div').forEach(x=>x.classList.remove('active'));d.classList.add('active');current=0;selected.clear();render();
});
document.querySelector('.btn').onclick=()=>fetch('/venue-server/api/reservation/order/submit',{method:'POST'});
</script></body></html>'''


class ConfigTests(unittest.TestCase):
    def test_training_hall_numbers_outside_main_hall_priority(self):
        slots = [{'court':f'{court}号','start':f'{hour}:00','end':f'{hour+1}:00','classes':['free']}
                 for court in [17,24] for hour in [20,21]]
        choice = available_choice(slots, dict(load_config(DEFAULT_CONFIG), reserve_time=[20,21]))
        self.assertEqual([s['court'] for s in choice], ['17号','17号'])

    def test_priority_precedes_other_visible_courts(self):
        slots = [{'court':f'{court}号','start':f'{hour}:00','end':f'{hour+1}:00','classes':['free']}
                 for court in [1,17] for hour in [20,21]]
        choice = available_choice(slots, dict(load_config(DEFAULT_CONFIG), reserve_time=[20,21], priority=[17]))
        self.assertEqual([s['court'] for s in choice], ['17号','17号'])

    def test_invalid_hours_and_priority(self):
        base = load_config(DEFAULT_CONFIG)
        for field, values in {'reserve_time': [[14,16], [14,14], [True], []], 'priority': [[1,1], [0], [True]]}.items():
            for value in values:
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'config.yaml'
                    path.write_text(yaml.safe_dump(dict(base, **{field:value})), encoding='utf-8')
                    with self.assertRaises(ReservationError):
                        load_config(path)

    def test_explicit_date_and_year_boundary(self):
        self.assertEqual(target_date({'days_ahead': 2}, date(2026,12,31)), date(2027,1,2))
        self.assertEqual(target_date({'reserve_date': '2027-01-01'}, date(2026,12,31)), date(2027,1,1))
        with self.assertRaises(ReservationError):
            target_date({'reserve_date': '2026-10-18'}, date(2026,10,19))


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        options = {'headless': True}
        if os.getenv('CGYY_BROWSER_BINARY'):
            options['executable_path'] = os.environ['CGYY_BROWSER_BINARY']
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.requests = []
        self.reply = {'code': 200, 'data': {'tradeNo': 'test-order'}}
        self.page.route('**/*', self.handle)
        self.page.goto('https://cgyy.buaa.edu.cn/test')
        self.config = dict(load_config(DEFAULT_CONFIG), reserve_time=[11,12], priority=[2,1], candidates=[])
        self.client = VenueClient(self.page, self.config)

    def tearDown(self):
        self.context.close()

    def handle(self, route):
        self.requests.append((route.request.method, route.request.url))
        if '/api/' in route.request.url:
            route.fulfill(json=self.reply)
        else:
            route.fulfill(content_type='text/html', body=HTML)

    def test_inspect_and_cross_page_selection(self):
        slots = self.client.inspect()
        self.assertEqual(len(slots), 8)
        self.assertEqual(self.page.locator('.selected').count(), 0)
        self.assertFalse(any(method == 'POST' for method, _ in self.requests))
        choice = available_choice(slots, self.config)
        self.assertEqual([s['court'] for s in choice], ['1号','1号'])
        self.assertEqual([s['page'] for s in choice], [0,1])
        self.client.select(choice)
        self.assertEqual(self.page.evaluate('Array.from(selected).sort()'), ['1_11','1_12'])

    def test_fast_choice_stops_on_first_available_page(self):
        self.config.update(reserve_time=[10,11])
        choice = self.client.find_choice()
        self.assertEqual([s['court'] for s in choice], ['2号','2号'])
        self.assertEqual(self.page.evaluate('current'), 0)
        self.assertEqual(self.page.locator('.selected').count(), 0)

    def test_fast_choice_keeps_continuous_hours_across_pages(self):
        choice = self.client.find_choice()
        self.assertEqual([s['court'] for s in choice], ['1号','1号'])
        self.assertEqual([s['page'] for s in choice], [0,1])

    def test_refresh_uses_site_method_and_waits_for_response(self):
        self.page.locator('body').evaluate('''body => {
            const root=document.createElement('div');root.className='reservationStep1';body.append(root);
            root.__vue__={getReserveDate(){fetch('/venue-server/api/reservation/day/info')}};
        }''')
        self.client.refresh_availability()
        self.assertTrue(any('/api/reservation/day/info' in url for _,url in self.requests))
        self.assertFalse(any(method == 'POST' for method,_ in self.requests))

    def test_expired_budget_does_not_click_submit(self):
        choice = available_choice(self.client.inspect(), self.config)
        with patch('cgyy.datetime') as clock, patch('cgyy.time', SimpleNamespace(monotonic=iter([0,2]).__next__)):
            clock.now.return_value = datetime(2026,10,19,12)
            with self.assertRaisesRegex(ReservationError,'未点击提交'):
                self.client.submit(choice, timeout=1000)
        self.assertFalse(any(method == 'POST' for method,_ in self.requests))

    def test_venue_list_uses_in_page_navigation(self):
        self.page.locator('body').evaluate('''body => {
            const tab=document.createElement('div'); tab.className='tabItem';
            tab.innerText='场地预约'; body.appendChild(tab);
            tab.onclick=()=>{history.pushState({},'', '/venue/venue-introduce');
                body.insertAdjacentHTML('beforeend','<div class="place-item"><h5>测试场馆</h5></div>');};
        }''')
        requests_before=len(self.requests)
        self.assertEqual(self.client.list_venues(), ['测试场馆'])
        self.assertEqual(len(self.requests), requests_before)

    def test_date_change_waits_for_actual_api_prefix(self):
        self.client.choose_date(date(2026,10,20))
        self.assertIn('10月20日', self.page.locator('.date_box .active').inner_text())
        self.assertTrue(any('/venue-server/api/reservation/day/info' in url for _, url in self.requests))
        with self.assertRaises(ReservationError):
            self.client.choose_date(date(2026,10,21))

    def test_changed_availability_refuses_selection(self):
        choice = available_choice(self.client.inspect(), self.config)
        self.client.first_page()
        self.page.locator('[data-key="1_11"]').evaluate('(e)=>e.classList.replace("free","reserved")')
        with self.assertRaises(ReservationError):
            self.client.select(choice)
        self.assertFalse(any(method == 'POST' for method, _ in self.requests))

    def test_submit_checks_server_business_status(self):
        choice = available_choice(self.client.inspect(), self.config)
        self.reply = {'code': 400, 'message': '同伴存在时间冲突', 'data': {'tradeNo':'not-success'}}
        with patch('cgyy.datetime') as clock:
            clock.now.return_value = datetime(2026,10,19,12)
            with self.assertRaisesRegex(ReservationError, '同伴存在时间冲突'):
                self.client.submit(choice)
        self.assertEqual(sum(method == 'POST' for method, _ in self.requests), 1)

    def test_submit_confirmed_by_trade_number(self):
        choice = available_choice(self.client.inspect(), self.config)
        with patch('cgyy.datetime') as clock:
            clock.now.return_value = datetime(2026,10,19,12)
            self.client.submit(choice)
        self.assertEqual(sum(method == 'POST' for method, _ in self.requests), 1)

    def test_captcha_callback_completes_one_order_without_resubmitting(self):
        self.page.locator('.submit_order_box .btn').evaluate('''button => {
            button.onclick=()=>document.body.insertAdjacentHTML('beforeend',
                '<div class="verifybox">请完成安全验证</div>');
        }''')
        callbacks = []
        def solve(page, *, timeout):
            callbacks.append(timeout)
            page.evaluate('''() => {
                document.querySelector('.verifybox').remove();
                fetch('/venue-server/api/reservation/order/submit', {method:'POST'});
            }''')
        choice = available_choice(self.client.inspect(), self.config)
        with patch('cgyy.datetime') as clock:
            clock.now.return_value = datetime(2026,10,19,12)
            result = self.client.submit(choice, captcha_solver=solve)
        self.assertEqual(result['tradeNo'], 'test-order')
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(sum(method == 'POST' for method, _ in self.requests), 1)

    def captcha_submission_fixture(self, replies, *, status=200):
        self.page.locator('.submit_order_box .btn').evaluate('''button => {
            button.onclick=()=>document.body.insertAdjacentHTML('beforeend',
                '<div class="verifybox">请完成安全验证</div>');
        }''')
        def respond(route):
            self.requests.append((route.request.method,route.request.url))
            route.fulfill(status=status,json=replies.pop(0))
        self.page.route('**/api/reservation/order/submit',respond)
        callbacks=[]
        def solve(page,*,timeout):
            callbacks.append(timeout)
            page.evaluate('''async()=>{
                document.querySelector('.verifybox').remove();
                const response=await fetch('/venue-server/api/reservation/order/submit',{method:'POST'});
                const data=await response.json();
                if(data.code!==200) {
                    const modal=document.createElement('div');modal.className='ivu-modal-wrap';
                    modal.textContent=data.message;
                    const button=document.createElement('button');button.textContent='确定';
                    button.onclick=()=>modal.remove();modal.append(button);document.body.append(modal);
                }
            }''')
        return solve,callbacks

    def test_explicit_captcha_rejection_gets_one_fresh_challenge(self):
        solve,callbacks=self.captcha_submission_fixture([
            {'code':400,'message':'验证码非法校验'},
            {'code':200,'data':{'tradeNo':'second-challenge-order'}},
        ])
        choice=available_choice(self.client.inspect(),self.config)
        with patch('cgyy.datetime') as clock:
            clock.now.return_value=datetime(2026,10,19,12)
            result=self.client.submit(choice,timeout=5000,captcha_solver=solve)
        self.assertEqual(result['tradeNo'],'second-challenge-order')
        self.assertEqual(len(callbacks),2)
        self.assertLess(callbacks[1],callbacks[0])
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),2)

    def test_captcha_rejection_never_retries_more_than_once(self):
        solve,callbacks=self.captcha_submission_fixture([
            {'code':400,'message':'验证码非法校验'},
            {'code':400,'message':'验证码非法校验'},
        ])
        choice=available_choice(self.client.inspect(),self.config)
        with patch('cgyy.datetime') as clock:
            clock.now.return_value=datetime(2026,10,19,12)
            with self.assertRaises(SubmissionRejected):
                self.client.submit(choice,timeout=5000,captcha_solver=solve)
        self.assertEqual(len(callbacks),2)
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),2)

    def test_ambiguous_submit_responses_are_never_retried(self):
        cases=[
            (200,{'code':200,'data':{}}),
            (200,{'code':400,'message':'验证码非法校验','data':{'tradeNo':'ambiguous-order'}}),
            (503,{'code':400,'message':'验证码非法校验'}),
        ]
        for status,reply in cases:
            with self.subTest(status=status,reply=reply):
                self.page.goto('https://cgyy.buaa.edu.cn/test')
                self.requests.clear()
                solve,callbacks=self.captcha_submission_fixture([reply],status=status)
                choice=available_choice(self.client.inspect(),self.config)
                with patch('cgyy.datetime') as clock:
                    clock.now.return_value=datetime(2026,10,19,12)
                    with self.assertRaises(ReservationError) as error:
                        self.client.submit(choice,timeout=5000,captcha_solver=solve)
                self.assertNotIsInstance(error.exception,SubmissionRejected)
                self.assertEqual(len(callbacks),1)
                self.assertEqual(sum(method=='POST' for method,_ in self.requests),1)

    def test_captcha_rejection_with_insufficient_budget_does_not_retry(self):
        solve,callbacks=self.captcha_submission_fixture([{'code':400,'message':'验证码非法校验'}])
        choice=available_choice(self.client.inspect(),self.config)
        with patch('cgyy.datetime') as clock:
            clock.now.return_value=datetime(2026,10,19,12)
            with self.assertRaises(SubmissionRejected):
                self.client.submit(choice,timeout=1500,captcha_solver=solve)
        self.assertEqual(len(callbacks),1)

    def test_submit_fills_phone_and_current_buddy_buttons(self):
        self.page.locator('body').evaluate('''body => body.insertAdjacentHTML('beforeend',
            '<input placeholder="请输入手机号"><div style="display:none"><input placeholder="请输入手机号"></div><div class="companion_box">'
            + '<div class="buddy-row" onclick="this.classList.add(\\'active\\')">测试同伴一</div>'
            + '<div class="buddy-row" onclick="this.classList.add(\\'active\\')">测试同伴二</div></div>')''')
        self.config.update(phone='13800000000', candidates=[2])
        choice = available_choice(self.client.inspect(), self.config)
        with patch('cgyy.datetime') as clock:
            clock.now.return_value = datetime(2026,10,19,12)
            self.client.submit(choice)
        self.assertEqual(self.page.locator('input[placeholder="请输入手机号"]:visible').input_value(), '13800000000')
        self.assertEqual(self.page.locator('.buddy-row.active').inner_text(), '测试同伴二')

    def test_maintenance_does_not_submit(self):
        with patch('cgyy.datetime') as clock:
            clock.now.return_value = datetime(2026,10,19,0)
            with self.assertRaisesRegex(ReservationError, '维护'):
                self.client.submit([])
        self.assertFalse(any(method == 'POST' for method, _ in self.requests))

    def test_maintenance_notice_waits_for_closing_animation(self):
        self.page.set_content('''<meta charset="utf-8"><div class="ivu-modal-wrap">系统每日运行维护，不支持预约支付
            <button onclick="setTimeout(()=>this.parentElement.style.display='none',300)">确定</button></div>''')
        with self.assertLogs('cgyy', level='WARNING') as messages:
            self.client.dismiss_notices()
        self.assertEqual(len(messages.output), 1)
        self.assertFalse(self.page.locator('.ivu-modal-wrap').is_visible())

    def auth_route(self, route):
        url = route.request.url
        if url.endswith('/venue/Login'):
            body = '<a href="https://sso.buaa.edu.cn/login">统一身份认证登录</a>'
        elif url.endswith('/cas/login-normal.html'):
            body = '''<input id="unPassword"><input id="pwPassword" type="password">
                <input id="cptPassword" style="display:none"><div id="errPassword"></div>
                <button class="submit-btn" onclick="parent.location.href='/warning'">登录</button>'''
        elif url.endswith('/warning'):
            body = '''账号存在安全风险<input id="btnSkip" type="button" disabled value="本次忽略"
                onclick="location.href='https://cgyy.buaa.edu.cn/venue/home'">
                <script>setTimeout(()=>document.querySelector('#btnSkip').disabled=false,600)</script>'''
        elif url.endswith('/venue/home'):
            body = '<a>退出</a>'
        else:
            body = '<iframe id="loginIframe" src="/cas/login-normal.html"></iframe>'
        route.fulfill(content_type='text/html; charset=utf-8', body='<meta charset="utf-8">' + body)

    def test_password_warning_waits_for_enabled_skip(self):
        self.page.route('**/*', self.auth_route)
        self.client.login('test-student', 'test-password', True)
        self.assertTrue(self.page.url.endswith('/venue/home'))

    def test_password_warning_requires_explicit_option(self):
        self.page.route('**/*', self.auth_route)
        with self.assertRaisesRegex(ReservationError, '密码不安全'):
            self.client.login('test-student', 'test-password')
        self.assertTrue(self.page.url.endswith('/warning'))


if __name__ == '__main__':
    unittest.main()
