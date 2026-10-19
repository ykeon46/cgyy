"""前端的独立浏览器任务；凭据只从 stdin 或本机环境读取。"""
import fcntl
import json
import logging
import os
from pathlib import Path
import shlex
import sys
from datetime import datetime, timedelta

from cgyy import CHINA, LOG, ReservationError, SubmissionRejected, TaskStopped, VenueClient, authenticated_client, load_config, sync_playwright
from payment import READ_ORDER
from login_state import LoginState
from scheduled_booking import BookingJob, daily_config

ROOT = Path(__file__).resolve().parent


def local_environment():
    result = dict(os.environ)
    path = ROOT/'.env'
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip().removeprefix('export ')
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            if key in {'BUAA_SSO_USER','BUAA_SSO_PASSWORD','CGYY_MAX_PAY_AMOUNT','CGYY_BROWSER_BINARY'}:
                values = shlex.split(value,comments=True)
                result[key] = values[0] if values else ''
    return result


def save_credentials(user, password):
    path = ROOT/'.env'
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.strip().removeprefix('export ').startswith(
        ('BUAA_SSO_USER=','BUAA_SSO_PASSWORD='))]
    lines += ['export BUAA_SSO_USER='+shlex.quote(user), 'export BUAA_SSO_PASSWORD='+shlex.quote(password)]
    temporary=ROOT/'.env.tmp'
    temporary.write_text('\n'.join(lines)+'\n');temporary.chmod(0o600);temporary.replace(path)


def run():
    os.chdir(ROOT);os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    mode = sys.argv[1]
    request = json.load(sys.stdin)
    env = local_environment();os.environ.update(env)
    options={'headless':not bool(os.getenv('DISPLAY'))}
    if env.get('CGYY_BROWSER_BINARY'):options['executable_path']=env['CGYY_BROWSER_BINARY']
    if mode=='recheck':
        with sync_playwright() as p:
            browser=p.chromium.launch(**options)
            try:
                context,client=authenticated_client(browser,{},env.get('BUAA_SSO_USER',''),lambda:env.get('BUAA_SSO_PASSWORD',''),LoginState(),ignore_password_warning=True)
                data=client.page.evaluate(READ_ORDER,request['trade_no']).get('data')
                if not data:raise ValueError('订单读取失败')
                print(json.dumps(dict(ok=True,**{key:data.get(key) for key in ['orderStatus','payStatus','expireTime','remainSecond']})),flush=True)
            finally:browser.close()
        return
    if mode == 'login':
        user, password = request['username'], request['password']
        with sync_playwright() as p:
            browser=p.chromium.launch(**options)
            try:
                context=browser.new_context(locale='zh-CN',timezone_id='Asia/Shanghai',viewport={'width':1440,'height':1000})
                page=context.new_page();page.set_default_timeout(20000)
                client=VenueClient(page,{})
                client.login(user,password,ignore_password_warning=True)
                LoginState().save(context,user)
                buddies=page.evaluate('''()=>new Promise(resolve=>{
                    const el=[...document.querySelectorAll('*')].find(e=>e.__vue__?.commonMethods?.emitAjax);
                    setTimeout(()=>resolve([]),5000);
                    el.__vue__.commonMethods.emitAjax({path:'/api/buddies',notPopUp:true,
                        success:d=>resolve((Array.isArray(d)?d:d?.content||[]).map((b,i)=>({index:i+1,name:b.name||b.buddyName||('同伴'+(i+1))}))),error:()=>resolve([])});
                })''')
                save_credentials(user,password)
                print(json.dumps({'ok':True,'username':user,'buddies':buddies},ensure_ascii=False),flush=True)
            finally:browser.close()
        return
    directory=Path(request['directory'])
    config=load_config(directory/'config.yaml')
    now=datetime.now(CHINA)
    if request.get('mode')=='daily':
        config=daily_config(config,now)
    else:
        release=datetime.fromisoformat(request['run_at']) if request.get('run_at') else now+timedelta(seconds=2)
        if release.tzinfo is None:release=release.replace(tzinfo=CHINA)
        start=max(now,release-timedelta(minutes=2))
        config.update(start_at=start.isoformat(),release_at=release.isoformat(),stop_at=(release+timedelta(minutes=5)).isoformat())
    job=BookingJob(config,directory/'status.json',fast=config.get('fast',True),payment_qr=True,
                   login_state=LoginState() if config.get('remember_session',True) else LoginState(None),
                   poll_availability=request.get('mode')!='now')
    lock=(ROOT/'artifacts/booking-execution.lock').open('w')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        job.status('failed',message='已有预约任务运行，请先处理当前任务。');return
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(**options)
            try:
                from text_captcha import TextCaptchaSolver
                job.captcha_solver=TextCaptchaSolver(ROOT/'artifacts/Text_select_captcha',Path('/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'))
                job.run(browser,env.get('BUAA_SSO_USER',''),env.get('BUAA_SSO_PASSWORD',''))
            finally:browser.close()
    except Exception as error:
        # 不把浏览器异常、认证票据或密码回传给前端。
        if isinstance(error,TaskStopped):
            job.status('stopped',message='预约任务已停止。')
        elif isinstance(error,SubmissionRejected):
            job.status('rejected',message=str(error))
        elif not job.order:
            job.status('needs_attention' if job.submission_started else 'failed',
                       message=str(error) if isinstance(error,ReservationError) else '任务未完成，请核对场馆预约记录后再操作。')
        elif not (directory/'status.json').exists():
            job.status('qr_needs_attention',message='订单已生成，请核对场馆付款页面。')
    finally:lock.close()


if __name__=='__main__':
    try:run()
    except Exception:
        print(json.dumps({'ok':False,'message':'登录未完成，请检查学校账号、密码或统一认证的验证要求。'},ensure_ascii=False),flush=True)
        sys.exit(1)
