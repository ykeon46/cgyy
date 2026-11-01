"""官方二维码生成前核对场次、金额和时效；全部网络由本地拦截。"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import base64
import os
import unittest
import cv2
import numpy as np
from urllib.parse import urlparse
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from payment import CampusQRCode, ExpectedOrder, PaymentError, amount

VENUE = '学院路校区-体育馆主馆-羽毛球'
CHOICE = [{'court':'1号', 'start':'07:00', 'end':'08:00', 'text':'￥15.00'}]
ORDER = {'subject':'学院路校区体育馆主馆羽毛球', 'payFee':15, 'payStatus':1,
         'orderStatus':1, 'remainSecond':55, 'spaceList':[
             {'venueSpaceName':'1号', 'startDate':'2026-10-21 07:00', 'endDate':'2026-10-21 08:00',
              'startTime':'07:00', 'endTime':'08:00'}]}

VENUE_HTML = '''<div id="native-app">场馆</div><script>
document.querySelector('#native-app').__vue__={config:{payList:[{payType:13,isOpenRole:true}]},userInfo:{role:5},commonMethods:{emitAjax:spec=>{
 fetch('/venue-server'+spec.path,{method:spec.type||'GET',body:spec.type==='POST'?JSON.stringify(spec.data):undefined}).then(r=>r.json()).then(d=>spec.success(d.data));
}}};</script>'''

PNG=base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'test-image').decode()

class QRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        options={'headless':True}
        if os.getenv('CGYY_BROWSER_BINARY'):options['executable_path']=os.environ['CGYY_BROWSER_BINARY']
        cls.browser=cls.playwright.chromium.launch(**options)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.playwright.stop()
    def setUp(self):
        self.context=self.browser.new_context();self.context.route('**/*',self.route)
        self.order=deepcopy(ORDER);self.requests=[];self.native_html=None
        self.result={'payFee':15,'schoolPayUrl':'https://cashier.cc-pay.cn/cashier?test=1','payCode':PNG}
        self.page=self.context.new_page();self.page.goto('https://cgyy.buaa.edu.cn/venue/venue-reservation/38')
        self.expected=ExpectedOrder.from_choice('test-trade',VENUE,date(2026,10,21),CHOICE,Decimal('15'))
    def tearDown(self):self.context.close()
    def route(self,route):
        req=route.request;self.requests.append((req.method,req.url));path=urlparse(req.url).path
        if path.endswith('/api/venue/finances/order/detail'):route.fulfill(json={'code':200,'data':self.order})
        elif path.endswith('/api/venue/finances/order/pay'):route.fulfill(json={'code':200,'data':self.result})
        elif urlparse(req.url).hostname=='cashier.cc-pay.cn':route.fulfill(content_type='text/html',body='''
            <svg width="40" height="40" onclick="location.href='https://excashier.alipay.com/standard/fastpayQr.htm'">
            <use href="#iconzhifubaozhifu1"></use><rect width="40" height="40"></rect></svg>''')
        elif urlparse(req.url).hostname=='excashier.alipay.com':route.fulfill(content_type='text/html; charset=utf-8',body=self.native_html or '')
        else:route.fulfill(content_type='text/html; charset=utf-8',body=('<meta charset="utf-8">'+VENUE_HTML))
    def test_returns_campus_entry_png_when_requested(self):
        payer=CampusQRCode(self.page,self.expected,direct=False);result=payer.create()
        self.assertEqual(result['qr_code'],'data:image/png;base64,'+PNG)
        self.assertEqual(result['amount'],'15.00')
        self.assertIn('expires_at',result)
        self.assertTrue(all(urlparse(url).hostname=='cgyy.buaa.edu.cn' for _,url in self.requests))
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),1)
        self.assertEqual(result['qr_kind'],'campus_cashier')
        with self.assertRaises(PaymentError):payer.create()
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),1)
    def prepare_alipay_page(self):
        target='https://qr.alipay.com/test-transaction'
        self.native_html=f'''<meta charset="utf-8"><div>北京航空航天大学</div>
            <strong class="order-amount">¥ 15.00</strong>
            <input id="J_qrCode" type="hidden" value="{target}">'''
        return target
    def test_payment_progress_is_reported_before_each_slow_request(self):
        phases=[]
        CampusQRCode(self.page,self.expected,direct=False).create(progress=lambda phase,message:phases.append(phase))
        self.assertEqual(phases,['order_read','pay_request'])
    def test_final_order_check_is_sent_before_cashier_loads(self):
        self.prepare_alipay_page()
        result=CampusQRCode(self.page,self.expected).create()
        self.assertEqual(result['qr_kind'],'alipay_payment')
        details=[i for i,(_,url) in enumerate(self.requests) if url.split('?')[0].endswith('/order/detail')]
        cashier=next(i for i,(_,url) in enumerate(self.requests) if urlparse(url).hostname=='cashier.cc-pay.cn')
        self.assertEqual(len(details),2)
        self.assertLess(details[1],cashier)
    def test_returns_native_alipay_transaction_qr_without_account_login(self):
        target=self.prepare_alipay_page()
        result=CampusQRCode(self.page,self.expected).create()
        self.assertEqual(result['qr_kind'],'alipay_payment')
        self.assertEqual(result['alipay_url'],target)
        png=base64.b64decode(result['qr_code'].split(',',1)[1])
        decoded,_,_=cv2.QRCodeDetector().detectAndDecode(cv2.imdecode(np.frombuffer(png,dtype=np.uint8),cv2.IMREAD_COLOR))
        self.assertEqual(decoded,target)
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),1)
        self.assertFalse(any('securityPost' in url or 'login' in url.lower() for _,url in self.requests))

    def test_campus_entry_is_published_before_native_qr_finishes(self):
        self.prepare_alipay_page();seen=[]
        payer=CampusQRCode(self.page,self.expected)
        original=payer.alipay_qr
        def native(*args):
            self.assertEqual(len(seen),1)
            self.assertEqual(seen[0]['qr_kind'],'campus_cashier')
            return original(*args)
        with patch.object(payer,'alipay_qr',side_effect=native):
            result=payer.create(update=seen.append)
        self.assertEqual(result['qr_kind'],'alipay_payment')
        self.assertEqual(seen[0]['qr_kind'],'campus_cashier')
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),1)

    def test_payment_via_early_entry_is_not_reported_as_qr_failure(self):
        self.prepare_alipay_page()
        result=CampusQRCode(self.page,self.expected).create(
            update=lambda entry:self.order.update(payStatus=2))
        self.assertTrue(result['payment_confirmed'])
        self.assertIsNone(result['qr_code'])
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),1)

    def test_unverified_payment_response_is_never_published(self):
        self.result['payFee']=16;seen=[]
        with self.assertRaises(PaymentError):
            CampusQRCode(self.page,self.expected).create(update=seen.append)
        self.assertEqual(seen,[])

    def test_paid_is_checked_again_after_one_second(self):
        payer=CampusQRCode(self.page,self.expected);seen=[]
        with patch.object(self.page,'wait_for_timeout',side_effect=lambda ms:self.order.update(payStatus=2)) as wait:
            result=payer.wait_for_payment('2099-01-01T00:00:00+08:00',lambda status,**kw:seen.append(status))
        wait.assert_called_once_with(1000)
        self.assertEqual(result,'paid')
        self.assertEqual(seen,['paid'])
    def test_native_qr_wrong_amount_or_target_falls_back_to_labelled_entry(self):
        for old,new in [('¥ 15.00','¥ 16.00'),('https://qr.alipay.com/','https://example.test/')]:
            with self.subTest(change=new):
                self.prepare_alipay_page();self.native_html=self.native_html.replace(old,new)
                result=CampusQRCode(self.page,self.expected).create()
                self.assertEqual(result['qr_kind'],'campus_cashier')
                self.assertIn('可能需要登录',result['scan_tip'])
    def test_changed_order_or_expiry_never_requests_payment(self):
        for change in [{'payFee':16},{'subject':'其他场馆'},{'orderStatus':2},{'payStatus':2},{'remainSecond':1}]:
            with self.subTest(change=change):
                self.order=deepcopy(ORDER);self.order.update(change)
                with self.assertRaises(PaymentError):CampusQRCode(self.page,self.expected).create()
        self.assertFalse(any(method=='POST' for method,_ in self.requests))
    def test_unavailable_school_payment_channel_never_posts(self):
        self.page.evaluate("document.querySelector('#native-app').__vue__.config.payList=[]")
        with self.assertRaises(PaymentError):CampusQRCode(self.page,self.expected).create()
        self.assertFalse(any(method=='POST' for method,_ in self.requests))
    def test_rejects_foreign_cashier_or_changed_amount(self):
        self.result['schoolPayUrl']='https://example.test/cashier'
        with self.assertRaises(PaymentError):CampusQRCode(self.page,self.expected).create()
    def test_rejects_invalid_image(self):
        self.result['payCode']=base64.b64encode(b'not-an-image').decode()
        with self.assertRaises(PaymentError):CampusQRCode(self.page,self.expected).create()
    def test_paid_requires_valid_school_order(self):
        self.order['payStatus']=2;seen=[]
        result=CampusQRCode(self.page,self.expected).wait_for_payment('2099-01-01T00:00:00+08:00',lambda status,**kw:seen.append(status))
        self.assertEqual(result,'paid');self.assertEqual(seen,['paid'])
    def test_cancelled_clears_qr(self):
        self.order['orderStatus']=2;seen=[]
        CampusQRCode(self.page,self.expected).wait_for_payment('2099-01-01T00:00:00+08:00',lambda status,**kw:seen.append((status,kw)))
        self.assertEqual(seen[0][0],'cancelled');self.assertIsNone(seen[0][1]['qr_code'])
    def test_price_cap_and_invalid_amount(self):
        with self.assertRaises(PaymentError):ExpectedOrder.from_choice('',VENUE,date(2026,10,21),CHOICE,Decimal('14'))
        for value in ['NaN','Infinity',0,-1,'1.001']:
            with self.assertRaises(PaymentError):amount(value)

if __name__=='__main__':unittest.main()
