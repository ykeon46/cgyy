"""全部请求由本地路由拦截，验证付款前核对和只提交一次，不连接真实支付。"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from payment import AlipayPayment, ExpectedOrder, PaymentError, PaymentSettings, amount, check_previous_result


VENUE = '学院路校区-体育馆主馆-羽毛球'
CHOICE = [{'court':'1号', 'start':'07:00', 'end':'08:00', 'text':'￥15.00'}]
ORDER = {'subject':'学院路校区体育馆主馆羽毛球', 'payFee':15, 'payStatus':1,
         'orderStatus':1, 'remainSecond':55, 'spaceList':[
             {'venueSpaceName':'1号', 'startDate':'2026-10-21 07:00', 'endDate':'2026-10-21 08:00',
              'startTime':'07:00', 'endTime':'08:00'}]}

VENUE_HTML = '''<div id="native-app">场馆</div><script>
document.querySelector('#native-app').__vue__={commonMethods:{emitAjax:spec=>{
 fetch('/venue-server'+spec.path).then(r=>r.json()).then(d=>spec.success(d.data));
}}};</script>'''
CASHIER_HTML = '''<div class="orderInfo-box"><span>航财通·校园付</span>
<div class="btn-group"><div class="btn" id="pay">支付 （55s）</div></div></div>
<script>
document.querySelector('.orderInfo-box').__vue__={choosePayModel:{payType:13}};
document.querySelector('#pay').onclick=async()=>{
 const d=await fetch('/venue-server/api/venue/finances/order/pay',{method:'POST'}).then(r=>r.json());
 const a=document.createElement('a');a.innerText='使用电脑支付';a.href='#';
 a.onclick=()=>window.open(d.data.schoolPayUrl);document.body.appendChild(a);
};</script>'''
CAMPUS_HTML = '''<div>应付金额:15.00元 北京航空航天大学</div>
<svg width="180" height="80" onclick="location.href='https://excashier.alipay.com/standard/auth.htm'">
<use xlink:href="#iconzhifubaozhifu1"></use><rect width="180" height="80" fill="blue"/></svg>'''
QR_HTML = '''<a id="J_changePayStyle" href="#">使用电脑支付</a>
<form id="J_changePayStyleForm" method="post" action="https://excashier.alipay.com/standard/switchToStdFront.htm"></form>'''
ACCOUNT_HTML = '''<div>收款方：北京航空航天大学</div>
<div id="J_basePriceArea"><strong>15.00</strong>元</div>
<div>请输入账户的 支付密码，不是登录密码。</div>
<form id="J_TloginForm" method="post" action="https://excashier.alipay.com/standard/securityPost.json">
<input id="J_tLoginId" name="loginId" type="email" placeholder="手机号码/邮箱">
<input id="payPasswd_rsainput" name="payPasswd_rsainput" type="password">
<a href="#" id="J_newBtn">下一步</a></form>
<script>
window.mockLogin={_accountValidated:true,submitBtnDisabled:false,ajaxValidateAccountSending:false};
window.seajs={require:()=>window.mockLogin};window.light={page:{products:{payPasswd:{ready:true}}}};
document.querySelector('#J_newBtn').onclick=()=>fetch('/standard/securityPost.json',{
 method:'POST',body:JSON.stringify({account:document.querySelector('#J_tLoginId').value,
 password:document.querySelector('#payPasswd_rsainput').value})});</script>'''


class PaymentConfigTests(unittest.TestCase):
    def test_credentials_only_from_named_environment_variables(self):
        with patch.dict(os.environ, {'ALIPAY_ACCOUNT':'fake@example.test','ALIPAY_PAY_PASSWORD':'fake-pay-secret',
                                      'CGYY_MAX_PAY_AMOUNT':'15.00'}, clear=True):
            settings = PaymentSettings.from_env(charge=True)
        self.assertEqual(settings.limit, Decimal('15.00'))
        self.assertNotIn('fake-pay-secret', repr(settings))
        self.assertNotIn('fake@example.test', repr(settings))

    def test_missing_credentials_and_invalid_limits(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(PaymentError, '单笔付款上限'):
                PaymentSettings.from_env(charge=True)
            with self.assertRaisesRegex(PaymentError, 'ALIPAY_ACCOUNT'):
                PaymentSettings.from_env('15', charge=True)
            self.assertEqual(PaymentSettings.from_env('15', charge=False).password, '')
        for value in ['NaN','Infinity','-1','0','15.001','not-money']:
            with self.subTest(value=value), self.assertRaises(PaymentError):
                amount(value)

    def test_price_cap_and_shahe_rejected_before_order(self):
        with self.assertRaisesRegex(PaymentError, '上限'):
            ExpectedOrder.from_choice('',VENUE,date(2026,10,21),CHOICE,Decimal('14.99'))
        with self.assertRaisesRegex(PaymentError, '沙河'):
            ExpectedOrder.from_choice('','沙河校区-羽毛球',date(2026,10,21),CHOICE,Decimal('15'))

    def test_uncertain_payment_blocks_new_order_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.json'
            path.write_text(json.dumps({'status':'payment_submitted_unknown','trade_no':'old-order'}))
            with self.assertRaisesRegex(PaymentError, '结果不明'):
                check_previous_result(path)


class PaymentBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        options = {'headless':True}
        if os.getenv('CGYY_BROWSER_BINARY'):
            options['executable_path']=os.environ['CGYY_BROWSER_BINARY']
        cls.browser=cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'payment-result.json'
        self.order=deepcopy(ORDER)
        self.account_html=ACCOUNT_HTML
        self.requests=[]
        self.detail_reads=0
        self.cancel_on_read=None
        self.confirm_payment=True
        self.context=self.browser.new_context()
        self.context.route('**/*',self.route)
        self.page=self.context.new_page()
        self.page.goto('https://cgyy.buaa.edu.cn/venue/venue-reservation/38')
        self.cashier=self.context.new_page()
        self.cashier.goto('https://cgyy.buaa.edu.cn/venue/pay-order?tradeNo=test-trade')
        self.plan=ExpectedOrder.from_choice('test-trade',VENUE,date(2026,10,21),CHOICE,Decimal('15'))
        self.settings=PaymentSettings(Decimal('15'),'fake@example.test','fake-pay-secret')

    def tearDown(self):
        self.context.close()
        self.temp.cleanup()

    def route(self,route):
        request=route.request
        path=urlparse(request.url).path
        self.requests.append((request.method,request.url))
        if path.endswith('/api/venue/finances/order/detail'):
            self.detail_reads+=1
            if self.cancel_on_read==self.detail_reads:
                self.order.update(orderStatus=2,remainSecond=None)
            route.fulfill(json={'code':200,'data':self.order})
            return
        if path.endswith('/api/venue/finances/order/pay'):
            route.fulfill(json={'code':200,'data':{'payFee':15,'schoolPayUrl':'https://cashier.cc-pay.cn/cashier?test=1'}})
            return
        if path=='/standard/securityPost.json':
            if self.confirm_payment:
                self.order['payStatus']=2
            route.fulfill(json={'code':200})
            return
        if path=='/standard/switchToStdFront.htm':
            html=self.account_html
        elif path=='/standard/auth.htm':
            html=QR_HTML
        elif path=='/cashier':
            html=CAMPUS_HTML
        elif path=='/venue/pay-order':
            html=CASHIER_HTML
        else:
            html=VENUE_HTML
        route.fulfill(content_type='text/html; charset=utf-8',body='<meta charset="utf-8">'+html)

    def payer(self,charge=True):
        return AlipayPayment(self.page,self.plan,self.settings,self.path,charge=charge)

    def payer_posts(self):
        return sum(method=='POST' and urlparse(url).path=='/standard/securityPost.json' for method,url in self.requests)

    def final_cashier(self, fee='15.00', account='fake@example.test'):
        page = self.context.new_page()
        html = f'''支付宝账户：{account}<div>订单名称: 场馆预约-羽毛球</div>
        <div>订单金额: {fee}</div><div>支付{fee}元</div>
        <iframe src="https://securitycore.alipay.com/validate.htm"></iframe>
        <script>addEventListener('message',e=>{{if(e.origin==='https://securitycore.alipay.com' && e.data==='confirmed')fetch('/business/api/pay.json',{{method:'POST'}});}});</script>'''
        page.route('**/business/cashiermain.htm', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=html))
        page.route('https://securitycore.alipay.com/validate.htm', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body='''<input id="payPassword_rsainput" type="password">
            <input type="button" id="validateButton" value="确定"
             onclick="parent.postMessage('confirmed','https://cashier.alipay.com')">'''))
        def charge(route):
            self.requests.append((route.request.method, route.request.url))
            self.order['payStatus'] = 2
            route.fulfill(json={'success': True})
        page.route('**/business/api/pay.json', charge)
        page.goto('https://cashier.alipay.com/business/cashiermain.htm')
        return page

    def test_logged_in_final_cashier_charges_once_and_confirms_server(self):
        page = self.final_cashier()
        payer = self.payer()
        with patch.object(payer, 'open_alipay', return_value=page):
            self.assertEqual(payer.run(), 'paid')
        self.assertEqual(sum(urlparse(url).path == '/business/api/pay.json' for _, url in self.requests), 1)
        self.assertTrue(json.loads(self.path.read_text())['final_charge_submitted'])
        with self.assertRaisesRegex(PaymentError, '重复'):
            payer.confirm_final_cashier(page)

    def test_final_cashier_rejects_changed_account_amount_and_expiry(self):
        for fee, account in [('16.00', 'fake@example.test'), ('15.00', 'other@example.test')]:
            with self.subTest(fee=fee, account=account):
                page = self.final_cashier(fee, account)
                with self.assertRaises(PaymentError):
                    self.payer().confirm_final_cashier(page)
                page.close()
        page = self.final_cashier()
        self.order['remainSecond'] = 0
        with self.assertRaisesRegex(PaymentError, '过期'):
            self.payer().confirm_final_cashier(page)
        self.assertFalse(any(urlparse(url).path == '/business/api/pay.json' for _, url in self.requests))

    def test_preview_cannot_activate_final_charge(self):
        page = self.final_cashier()
        with self.assertRaisesRegex(PaymentError, '预览'):
            self.payer(charge=False).confirm_final_cashier(page)
        self.assertFalse(any(urlparse(url).path == '/business/api/pay.json' for _, url in self.requests))

    def test_preview_walks_full_flow_without_credentials_or_payment(self):
        self.assertEqual(self.payer(charge=False).run(),'payment_preview_ready')
        alipay=self.context.pages[-1]
        self.assertEqual(alipay.locator('#J_tLoginId').input_value(),'')
        self.assertEqual(alipay.locator('#payPasswd_rsainput').input_value(),'')
        self.assertEqual(self.payer_posts(),0)

    def test_success_requires_server_confirmation_and_submits_once(self):
        payer=self.payer()
        self.assertEqual(payer.run(),'paid')
        self.assertEqual(self.payer_posts(),1)
        saved=self.path.read_text()
        self.assertNotIn('fake-pay-secret',saved)
        self.assertNotIn('fake@example.test',saved)
        self.assertEqual(json.loads(saved)['status'],'paid')
        self.assertTrue(json.loads(saved)['payment_request_observed'])
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        with self.assertRaisesRegex(PaymentError,'重复'):
            payer.run()
        with self.assertRaisesRegex(PaymentError,'重复'):
            self.payer().run()
        self.assertEqual(self.payer_posts(),1)

    def test_legacy_key_handlers_do_not_erase_payment_inputs(self):
        self.account_html=ACCOUNT_HTML+'''<script>
        document.querySelectorAll('input').forEach(e=>e.addEventListener('keyup',()=>e.value=''));
        </script>'''
        self.assertEqual(self.payer().run(),'paid')
        self.assertEqual(self.payer_posts(),1)

    def test_account_validation_completes_before_password_is_filled(self):
        self.account_html=ACCOUNT_HTML+'''<script>
        document.querySelector('#J_tLoginId').addEventListener('input',()=>{
          mockLogin._accountValidated=false;mockLogin.submitBtnDisabled=true;
          setTimeout(()=>{document.querySelector('#payPasswd_rsainput').value='';
            mockLogin._accountValidated=true;mockLogin.submitBtnDisabled=false;},500);
        });
        const post=document.querySelector('#J_newBtn').onclick;
        document.querySelector('#J_newBtn').onclick=()=>{if(!mockLogin.submitBtnDisabled)post();};
        </script>'''
        self.assertEqual(self.payer().run(),'paid')
        self.assertEqual(self.payer_posts(),1)

    def test_click_without_payment_request_is_not_reported_as_payment(self):
        self.account_html=ACCOUNT_HTML+'''<script>
        document.querySelector('#J_newBtn').onclick=()=>false;
        </script>'''
        with self.assertRaisesRegex(PaymentError,'未观察到'):
            self.payer().run()
        result=json.loads(self.path.read_text())
        self.assertFalse(result['payment_request_observed'])
        self.assertEqual(result['status'],'payment_submitted_unknown')
        self.assertEqual(self.payer_posts(),0)

    def test_changed_date_court_amount_or_venue_never_initiates_payment(self):
        for name,update in [('fee',{'payFee':16}),('venue',{'subject':'沙河校区羽毛球'}),
                            ('date',{'spaceList':[dict(ORDER['spaceList'][0],startDate='2026-10-22 07:00')]}),
                            ('court',{'spaceList':[dict(ORDER['spaceList'][0],venueSpaceName='2号')]}),
                            ('expired',{'remainSecond':0}),('cancelled',{'orderStatus':2})]:
            with self.subTest(name=name):
                self.order=deepcopy(ORDER);self.order.update(update)
                with self.assertRaises(PaymentError):
                    self.payer().run()
        self.assertFalse(any(urlparse(url).path.endswith('/order/pay') for _,url in self.requests))
        self.assertEqual(self.payer_posts(),0)

    def test_expiry_after_credentials_stops_before_submit(self):
        self.cancel_on_read=2
        with self.assertRaisesRegex(PaymentError,'已取消'):
            self.payer().run()
        self.assertEqual(self.payer_posts(),0)
        self.assertFalse(json.loads(self.path.read_text())['payer_form_submitted'])

    def test_reinitialized_form_stops_before_payment_click(self):
        payer=self.payer()
        real_read=payer.read_order
        calls=0
        def read():
            nonlocal calls
            calls+=1
            if calls==2:
                self.context.pages[-1].locator('#J_tLoginId').fill('')
            return real_read()
        with patch.object(payer,'read_order',side_effect=read):
            with self.assertRaisesRegex(PaymentError,'输入内容变化'):
                payer.run()
        self.assertEqual(self.payer_posts(),0)
        self.assertFalse(json.loads(self.path.read_text())['payer_form_submitted'])

    def test_tampered_merchant_amount_or_form_destination_never_submits(self):
        for old,new in [('北京航空航天大学','其他商家'),('15.00','16.00'),
                        ('https://excashier.alipay.com/standard/securityPost.json','https://other.test/standard/securityPost.json')]:
            with self.subTest(replacement=new):
                self.account_html=ACCOUNT_HTML.replace(old,new)
                with self.assertRaises(PaymentError):
                    self.payer().run()
                self.assertEqual(self.payer_posts(),0)

    def test_unknown_result_keeps_record_and_prevents_second_attempt(self):
        payer=self.payer()
        self.confirm_payment=False
        real_read=payer.read_order
        calls=0
        def read():
            nonlocal calls
            calls+=1
            if calls==4:
                raise PaymentError('结果尚未确认')
            return real_read()
        with patch.object(payer,'read_order',side_effect=read):
            with self.assertRaisesRegex(PaymentError,'未确认'):
                payer.run()
        self.assertEqual(json.loads(self.path.read_text())['status'],'payment_submitted_unknown')
        with self.assertRaisesRegex(PaymentError,'结果不明'):
            self.payer().run()
        self.assertEqual(self.payer_posts(),1)

    def test_concurrent_payment_is_blocked_before_any_second_submission(self):
        self.path.with_name(self.path.name+'.lock').touch()
        with self.assertRaisesRegex(PaymentError,'不并发付款'):
            self.payer().run()
        self.assertEqual(self.payer_posts(),0)


if __name__=='__main__':
    unittest.main()
