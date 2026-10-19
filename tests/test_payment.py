"""官方二维码生成前核对场次、金额和时效；全部网络由本地拦截。"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import base64
import os
import unittest
from urllib.parse import urlparse
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
        self.order=deepcopy(ORDER);self.requests=[]
        self.result={'payFee':15,'schoolPayUrl':'https://cashier.cc-pay.cn/cashier?test=1','payCode':PNG}
        self.page=self.context.new_page();self.page.goto('https://cgyy.buaa.edu.cn/venue/venue-reservation/38')
        self.expected=ExpectedOrder.from_choice('test-trade',VENUE,date(2026,10,21),CHOICE,Decimal('15'))
    def tearDown(self):self.context.close()
    def route(self,route):
        req=route.request;self.requests.append((req.method,req.url));path=urlparse(req.url).path
        if path.endswith('/api/venue/finances/order/detail'):route.fulfill(json={'code':200,'data':self.order})
        elif path.endswith('/api/venue/finances/order/pay'):route.fulfill(json={'code':200,'data':self.result})
        else:route.fulfill(content_type='text/html; charset=utf-8',body=('<meta charset="utf-8">'+VENUE_HTML))
    def test_returns_official_png_without_visiting_payment_account_pages(self):
        payer=CampusQRCode(self.page,self.expected);result=payer.create()
        self.assertEqual(result['qr_code'],'data:image/png;base64,'+PNG)
        self.assertEqual(result['amount'],'15.00')
        self.assertIn('expires_at',result)
        self.assertTrue(all(urlparse(url).hostname=='cgyy.buaa.edu.cn' for _,url in self.requests))
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),1)
        with self.assertRaises(PaymentError):payer.create()
        self.assertEqual(sum(method=='POST' for method,_ in self.requests),1)
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
