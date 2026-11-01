"""手机推送：订单生成、付款码就绪、付款结果。地址由 .env 中的 CGYY_NOTIFY_URL 配置。

支持 Bark（https://api.day.app/<key>）、Server酱（https://sctapi.ftqq.com/<key>.send）、
ntfy（https://ntfy.sh/<topic>），其他地址按 JSON {title, body, url} 发送 POST。
自建的 Bark 或 ntfy 服务可加前缀：bark+https://...、ntfy+https://...。
推送在后台线程中发送，失败只记录日志，不影响预约和付款流程。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from urllib.parse import urlencode, urlparse
import urllib.request

LOG = logging.getLogger('cgyy')
TIMEOUT_SECONDS = 5


def build_request(endpoint: str, title: str, body: str, url: str | None = None,
                  urgent: bool = False) -> urllib.request.Request:
    kind = ''
    if '+' in endpoint.split('://', 1)[0]:
        kind, endpoint = endpoint.split('+', 1)
    parsed = urlparse(endpoint)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname:
        raise ValueError('推送地址必须是 http(s) 链接。')
    host = parsed.hostname
    kind = kind or ('bark' if host == 'api.day.app' else 'serverchan' if host.endswith('ftqq.com')
                    else 'ntfy' if host == 'ntfy.sh' else 'json')
    if kind == 'bark':
        payload = {'title': title, 'body': body, 'group': 'cgyy'}
        if url:
            payload['url'] = url
        if urgent:
            payload['level'] = 'timeSensitive'
        return json_request(endpoint, payload)
    if kind == 'serverchan':
        text = body + (f'\n\n[打开付款链接]({url})' if url else '')
        return urllib.request.Request(endpoint, data=urlencode({'title': title, 'desp': text}).encode(),
                                      headers={'Content-Type': 'application/x-www-form-urlencoded'})
    if kind == 'ntfy':
        # JSON 发布接口支持中文标题；主题取地址路径。
        payload = {'topic': parsed.path.strip('/'), 'title': title, 'message': body,
                   'priority': 5 if urgent else 3}
        if url:
            payload['click'] = url
        return json_request(f'{parsed.scheme}://{parsed.netloc}/', payload)
    return json_request(endpoint, {'title': title, 'body': body, 'url': url, 'urgent': urgent})


def json_request(endpoint: str, payload: dict) -> urllib.request.Request:
    return urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode(),
                                  headers={'Content-Type': 'application/json; charset=utf-8'})


def deliver(request: urllib.request.Request) -> None:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            response.read(1024)
    except Exception as error:  # 推送失败绝不影响预约流程。
        LOG.warning('手机推送失败（%s）。', type(error).__name__)


def send(title: str, body: str, url: str | None = None, *, urgent: bool = False,
         endpoint: str | None = None):
    endpoint = os.getenv('CGYY_NOTIFY_URL', '') if endpoint is None else endpoint
    if not endpoint:
        return None
    try:
        request = build_request(endpoint, title, body, url, urgent)
    except ValueError as error:
        LOG.warning('%s', error)
        return None
    # 非守护线程：进程在任务结束后退出时，也会等这条推送发完（最多几秒）。
    thread = threading.Thread(target=deliver, args=(request,), name='cgyy-notify')
    thread.start()
    return thread


def clock(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value).strftime('%H:%M:%S') if value else ''
    except ValueError:
        return ''


def booking_message(state: dict):
    """把任务状态变成推送；不需要推送的状态返回 None。返回 (去重键, 标题, 正文, 链接, 是否紧急)。"""
    status = state.get('status')
    slots = '、'.join(f'{court} {start}-{end}' for court, start, end in state.get('slots') or [])
    order = ' '.join(part for part in [state.get('venue', ''), state.get('date', ''), slots] if part)
    deadline = clock(state.get('expires_at'))
    if status == 'locked':
        return ('locked', '已抢到场地，正在生成付款码', f'{order}\n订单约1分钟内需付款，付款码马上推送。',
                state.get('payment_url'), True)
    if status == 'awaiting_payment' and state.get('phase') == 'payment_ready':
        if state.get('qr_kind') == 'alipay_payment' and state.get('alipay_url'):
            return ('pay', '点开立即用支付宝付款', f'{order}\n金额 {state.get("amount")} 元，'
                    f'请在 {deadline} 前完成付款。', state['alipay_url'], True)
        return ('pay', '请打开校园付收银台付款', f'{order}\n金额 {state.get("amount")} 元，请在 {deadline} 前付款，'
                '收银台可能要求登录。', state.get('cashier_url'), True)
    titles = {'paid': ('预约成功', '场馆已确认付款，预约有效。'),
              'expired': ('付款窗口已结束', '订单未在时限内付款，请到场馆网站核对。'),
              'cancelled': ('订单已取消', '场馆订单已取消或正在退款，请核对账单。'),
              'needs_attention': ('预约结果需要核对', '提交结果未确认，请到场馆网站个人中心核对，程序不会自动重试。'),
              'qr_needs_attention': ('付款码获取失败', '订单已生成，请尽快到场馆网站付款页面完成付款。'),
              'no_availability': ('本次没有抢到场地', '所选时段没有连续可预约的场地，未下单。'),
              'failed': ('预约任务未完成', '程序未完成查询或下单，请查看任务记录。')}
    if status in titles:
        title, text = titles[status]
        link = state.get('payment_url') if status == 'qr_needs_attention' else None
        return (status, title, f'{order}\n{state.get("message") or text}' if order else (state.get('message') or text),
                link, status in {'needs_attention', 'qr_needs_attention'})
    return None
