"""核对场馆订单并返回校园付官方二维码，由本人扫码付款。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import base64
import logging
from io import BytesIO
import re
import time
from urllib.parse import urlparse
from playwright.sync_api import Error as BrowserError

class PaymentError(ValueError):
    pass

def amount(value) -> Decimal:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number != number.quantize(Decimal('0.01')):
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError):
        raise PaymentError('金额必须为大于零、最多两位小数的人民币金额。') from None

def normalize(value: str) -> str:
    return re.sub(r'[\s\-()（）]', '', value)


@dataclass(frozen=True)
class ExpectedOrder:
    trade_no: str
    venue: str
    day: date
    slots: tuple[tuple[str, str, str], ...]
    fee: Decimal

    @classmethod
    def from_choice(cls, trade_no, venue, day, choice, limit):
        if not choice or '沙河' in venue:
            raise PaymentError('需要有效场次，且不允许沙河校区。')
        prices = []
        for slot in choice:
            match = re.fullmatch(r'\s*[￥¥]\s*(\d+(?:\.\d{1,2})?)\s*(?:元)?\s*', slot.get('text', ''))
            if not match:
                raise PaymentError('未能确定所选场次的价格，未获取付款二维码。')
            prices.append(amount(match[1]))
        total = sum(prices, Decimal('0'))
        if total > limit:
            raise PaymentError('订单价格超过单笔付款上限。')
        return cls(trade_no, venue, day,
                   tuple((s['court'], s['start'], s['end']) for s in choice), total)

    def verify(self, data: dict, *, payable=True):
        if amount(data.get('payFee')) != self.fee:
            raise PaymentError('服务器订单金额与所选场次不一致。')
        if normalize(data.get('subject') or '') != normalize(self.venue):
            raise PaymentError('服务器订单场馆与所选场馆不一致。')
        observed = []
        for slot in data.get('spaceList') or []:
            start = str(slot.get('startDate', '')).split(' ')
            end = str(slot.get('endDate', '')).split(' ')
            if not start or not end or start[0] != str(self.day) or end[0] != str(self.day):
                raise PaymentError('服务器订单日期与预约日期不一致。')
            observed.append((slot.get('venueSpaceName'), slot.get('startTime'), slot.get('endTime')))
        if sorted(observed) != sorted(self.slots):
            raise PaymentError('服务器订单场地或时段与选择不一致。')
        if str(data.get('orderStatus')) != '1':
            raise PaymentError('场馆订单已取消或失效，停止付款。')
        if payable:
            if str(data.get('payStatus')) != '1':
                raise PaymentError('场馆订单不是待付款状态，停止付款。')
            try:
                remaining = int(data['remainSecond'])
            except (TypeError, ValueError, KeyError):
                remaining = 0
            if remaining <= 5:
                raise PaymentError('场馆订单已过期或剩余时间不足，停止付款。')


READ_ORDER = r'''tradeNo => new Promise(resolve => {
    const node = Array.from(document.querySelectorAll('*')).find(e => e.__vue__ && e.__vue__.commonMethods);
    if (!node) return resolve({error:true});
    const timer = setTimeout(() => resolve({error:true}), 6000);
    node.__vue__.commonMethods.emitAjax({
        path:'/api/venue/finances/order/detail', data:{venueTradeNo:tradeNo},
        success:data=>{clearTimeout(timer);resolve({data});},
        error:()=>{clearTimeout(timer);resolve({error:true});}
    });
})'''


class CampusQRCode:
    def __init__(self, page, expected: ExpectedOrder, *, direct=True):
        self.page = page
        self.expected = expected
        self.requested = False
        self.direct = direct

    def alipay_qr(self, cashier_url, expires_at):
        """Read the official transaction QR; never open or submit account/password forms."""
        deadline = min(time.monotonic()+12, time.monotonic()+
                       (expires_at-datetime.now().astimezone()).total_seconds()-5)
        def timeout():
            remaining = int((deadline-time.monotonic())*1000)
            if remaining<=0:raise PaymentError('获取支付宝交易码超时。')
            return remaining
        cashier = self.page.context.new_page()
        try:
            cashier.goto(cashier_url,wait_until='domcontentloaded',timeout=timeout())
            if urlparse(cashier.url).scheme!='https' or urlparse(cashier.url).netloc!='cashier.cc-pay.cn':
                raise PaymentError('校园付收银台需要额外登录。')
            cashier.locator('svg').filter(has=cashier.locator('use[*|href="#iconzhifubaozhifu1"]')).click(timeout=timeout())
            cashier.wait_for_url(re.compile(r'https://(?:excashier|cashier)\.alipay\.com/'),
                                 wait_until='domcontentloaded',timeout=timeout())
            parsed=urlparse(cashier.url)
            if parsed.scheme!='https' or parsed.hostname not in {'excashier.alipay.com','cashier.alipay.com'}:
                raise PaymentError('支付宝交易页面域名异常。')
            field=cashier.locator('#J_qrCode')
            field.wait_for(state='attached',timeout=timeout())
            target=field.input_value(timeout=timeout())
            if len(target)>2048 or urlparse(target).scheme!='https' or urlparse(target).netloc!='qr.alipay.com':
                raise PaymentError('未找到支付宝原生交易付款码。')
            if not cashier.get_by_text('北京航空航天大学',exact=True).count():
                raise PaymentError('支付宝收款方不一致。')
            fee=cashier.locator('.order-amount').first.inner_text(timeout=timeout())
            if amount(re.sub(r'[¥￥\s,]','',fee))!=self.expected.fee:
                raise PaymentError('支付宝交易金额不一致。')
            # 使用官方页面给出的交易地址生成带留白的清晰二维码，不依赖网页
            # 小尺寸 canvas 的绘制时序，也不会把网页登录码当作付款交易码。
            import qrcode
            image=qrcode.make(target,box_size=8,border=4)
            output=BytesIO();image.save(output,format='PNG')
            self.expected.verify(self.read_order())
            return 'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode(),target
        finally:
            cashier.close()

    def read_order(self):
        if urlparse(self.page.url).hostname != 'cgyy.buaa.edu.cn':
            raise PaymentError('场馆页面域名异常。')
        result = self.page.evaluate(READ_ORDER, self.expected.trade_no)
        if not isinstance(result, dict) or not isinstance(result.get('data'), dict):
            raise PaymentError('读取场馆订单状态失败，请核对预约记录。')
        return result['data']

    def create(self):
        if self.requested:
            raise PaymentError('已请求本订单的二维码，不重复创建支付交易。')
        data = self.read_order()
        self.expected.verify(data)
        remaining = int(data['remainSecond'])
        expires_at = datetime.now().astimezone() + timedelta(seconds=remaining)
        if data.get('expireTime'):
            try:
                official = datetime.fromisoformat(data['expireTime'])
                if official.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    official = official.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                expires_at = min(expires_at, official)
            except ValueError:
                pass
        self.requested = True
        # 使用场馆网页自己的签名请求封装；参数与官方校园付入口一致。
        response = self.page.evaluate(r'''spec => new Promise(resolve => {
            const node=[...document.querySelectorAll('*')].find(e=>e.__vue__?.commonMethods?.emitAjax && e.__vue__?.config?.payList);
            const vm=node?.__vue__;
            if (!vm) return resolve({error:'支付配置未加载'});
            const channel=vm.config.payList.find(item=>String(item.payType)==='13' &&
                (item.isOpenRole || item.supportRoleList?.includes(vm.userInfo?.role)));
            if (!channel) return resolve({error:'当前账号未开放校园付'});
            const timer=setTimeout(()=>resolve({error:'二维码请求超时，请核对原订单'}),10000);
            vm.commonMethods.emitAjax({path:'/api/venue/finances/order/pay',type:'POST',
                data:{payType:13,venueTradeNo:spec.tradeNo,paySecret:null,cardNo:null,outTradeNo:null,
                    couponId:null,amtEndDate:spec.amtEndDate||null,isApp:0,payRemark:null,isWeChat:null,
                    openidKey:localStorage.getItem('OPID'),offlinePayType:null},
                success:data=>{clearTimeout(timer);resolve({data});},
                error:()=>{clearTimeout(timer);resolve({error:'官方付款二维码未生成，请核对原订单'});}
            });
        })''', {'tradeNo':self.expected.trade_no,'amtEndDate':data.get('amtEndDate')})
        result=response.get('data') if isinstance(response,dict) else None
        if not isinstance(result,dict):
            raise PaymentError(response.get('error','官方付款二维码未生成，请核对原订单。'))
        url = urlparse(result.get('schoolPayUrl') or '')
        if url.scheme != 'https' or url.netloc != 'cashier.cc-pay.cn' or amount(result.get('payFee')) != self.expected.fee:
            raise PaymentError('官方收银台地址或金额不一致。')
        raw = result.get('payCode') or ''
        if raw.startswith(('data:image/png;base64,','data:image/jpeg;base64,')):
            raw = raw.split(',',1)[1]
        try:
            png = base64.b64decode(raw,validate=True)
        except (ValueError,TypeError):
            raise PaymentError('官方二维码图片格式异常。') from None
        mime='image/png' if png.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if png.startswith(b'\xff\xd8\xff') else None
        if not mime or len(png) > 1024 * 1024:
            raise PaymentError('官方未返回有效的二维码图片。')
        qr={'qr_code':'data:'+mime+';base64,'+raw,'cashier_url':result['schoolPayUrl'],
            'expires_at':expires_at.isoformat(),'amount':str(self.expected.fee),
            'qr_kind':'campus_cashier','scan_tip':'校园付收银台入口，扫码后按官方页面选择支付方式，可能需要登录。'}
        if self.direct:
            try:
                qr['qr_code'],qr['alipay_url']=self.alipay_qr(result['schoolPayUrl'],expires_at)
                qr.update(qr_kind='alipay_payment',scan_tip='请使用已登录的手机支付宝扫码，在手机上确认付款。')
            except (BrowserError,PaymentError,ImportError,ValueError) as error:
                reason=str(error) if isinstance(error,PaymentError) else type(error).__name__
                logging.getLogger('cgyy').warning('未获取到已核对的支付宝交易码，返回校园付收银台入口（%s）。',reason)
                self.expected.verify(self.read_order())
        return qr

    def wait_for_payment(self, expires_at: str, update):
        deadline = datetime.fromisoformat(expires_at)
        while True:
            data = self.read_order()
            pay_status, order_status = str(data.get('payStatus')), str(data.get('orderStatus'))
            if pay_status == '2' and order_status == '1':
                self.expected.verify(data,payable=False)
                update('paid',message='场馆已确认付款成功，预约有效。',qr_code=None)
                return 'paid'
            if order_status == '2' or pay_status in {'3','4','5'}:
                update('cancelled',message='场馆订单已取消或正在退款，请核对账单。',qr_code=None)
                return 'cancelled'
            if datetime.now().astimezone() >= deadline:
                update('expired',message='付款窗口已结束，请核对场馆订单及账单。',qr_code=None)
                return 'expired'
            self.page.wait_for_timeout(3000)
