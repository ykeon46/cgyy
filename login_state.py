"""仅保存北航登录态；不包含校园付或支付宝登录信息。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

LOG = logging.getLogger('cgyy')
DEFAULT_SESSION_STATE = Path('artifacts/auth/school-session.json')
SCHOOL_ORIGINS = {'https://cgyy.buaa.edu.cn', 'https://sso.buaa.edu.cn'}
SCHOOL_COOKIE_DOMAINS = {'buaa.edu.cn', 'cgyy.buaa.edu.cn', 'sso.buaa.edu.cn'}


def school_storage(state: dict) -> dict:
    return {
        'cookies': [cookie for cookie in state.get('cookies', [])
                    if cookie.get('domain', '').lstrip('.') in SCHOOL_COOKIE_DOMAINS],
        'origins': [origin for origin in state.get('origins', [])
                    if origin.get('origin') in SCHOOL_ORIGINS],
    }


class LoginState:
    def __init__(self, path: Path | None = DEFAULT_SESSION_STATE):
        self.path = path
        self.loaded = False

    @staticmethod
    def account_digest(user: str) -> str:
        return hashlib.sha256(user.encode('utf-8')).hexdigest()

    def new_context(self, browser, user: str, *, restore: bool = True):
        options = dict(locale='zh-CN', timezone_id='Asia/Shanghai', viewport={'width':1440,'height':1000})
        self.loaded = False
        if restore and self.path and self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                if value['version'] == 1 and value['account_digest'] == self.account_digest(user):
                    options['storage_state'] = school_storage(value['storage_state'])
                    self.loaded = True
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                LOG.info('本机登录态缓存不可用，将重新登录。')
        return browser.new_context(**options)

    def save(self, context, user: str) -> None:
        if not self.path:
            return
        value = {'version':1, 'account_digest':self.account_digest(user),
                 'storage_state':school_storage(context.storage_state())}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.school-session-', delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(value, stream, ensure_ascii=False)
            temporary.replace(self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
