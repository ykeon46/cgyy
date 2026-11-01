"""前端的浏览器任务；凭据只从 stdin 或本机环境读取。

serve 模式是常驻进程：保留一个已登录的浏览器，按行从 stdin 读取 JSON 命令，
按行向 stdout 写回结果。login、recheck、book 也可以作为一次性进程运行。"""
import fcntl
import json
import logging
import os
from pathlib import Path
import queue
import shlex
import secrets
import sys
import threading
from datetime import datetime, timedelta

from cgyy import CHINA, DEFAULT_CONFIG, LOG, ReservationError, SubmissionRejected, TaskStopped, VenueClient, load_config, sync_playwright, target_date
from payment import PaymentError, READ_ORDER
from login_state import LoginState, context_options
from browser_session import BrowserSession
from scheduled_booking import BookingJob, daily_config

KEEP_ALIVE_SECONDS = 240

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
            if key in {'BUAA_SSO_USER','BUAA_SSO_PASSWORD','CGYY_MAX_PAY_AMOUNT','CGYY_BROWSER_BINARY','CGYY_MINIPROGRAM_KEY','CGYY_PERSISTENT_BROWSER','CGYY_NOTIFY_URL','CGYY_WARM_TABS'}:
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


def browser_options(env):
    options={'headless':not bool(os.getenv('DISPLAY'))}
    if env.get('CGYY_BROWSER_BINARY'):options['executable_path']=env['CGYY_BROWSER_BINARY']
    return options


def captcha_solver():
    from text_captcha import TextCaptchaSolver
    fonts=[Path('/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf'),
           Path('/System/Library/Fonts/STHeiti Medium.ttc')]
    font=next((path for path in fonts if path.is_file()),fonts[0])
    return TextCaptchaSolver(ROOT/'artifacts/Text_select_captcha',font)


def login(session, request, env):
    user, password = request['username'], request['password']
    saved = user==env.get('BUAA_SSO_USER') and secrets.compare_digest(password.encode(),env.get('BUAA_SSO_PASSWORD','').encode())
    buddies = None
    if saved and session.client is not None and session.user==user and not session.stale:
        # 常驻会话仍在：读取同伴列表本身就是一次登录态核验，只需一个请求。
        buddies = session.client.buddies(user)
        if buddies is None:
            session.mark_invalid()
        else:
            session.mark_verified()
    if buddies is None:
        if saved:
            context,client=session.acquire(user,password)
        else:
            # 新账号或新密码必须真正通过统一认证，不能只凭缓存登录态。
            session.close()
            context=session.browser.new_context(**context_options(session.browser))
            try:
                page=context.new_page();page.set_default_timeout(20000)
                client=VenueClient(page,{})
                client.login(user,password,ignore_password_warning=True)
            except BaseException:
                context.close();raise
            session.adopt(user,context,client)
        buddies=client.buddies(user)
        if buddies is None:buddies=[]
    LoginState().save(session.context,user)
    save_credentials(user,password)
    return {'ok':True,'username':user,'buddies':buddies}


def recheck(session, request, env):
    if request.get('username',env.get('BUAA_SSO_USER'))!=env.get('BUAA_SSO_USER'):
        raise ValueError('登录账号已切换，请刷新后重试。')
    _,client=session.acquire(env.get('BUAA_SSO_USER',''),env.get('BUAA_SSO_PASSWORD',''))
    data=client.page.evaluate(READ_ORDER,request['trade_no']).get('data')
    if not data:raise ValueError('订单读取失败')
    session.mark_verified()
    return dict(ok=True,**{key:data.get(key) for key in ['orderStatus','payStatus','expireTime','remainSecond']})


def book(browser, request, env, *, session=None, solver=None):
    directory=Path(request['directory'])
    config=load_config(directory/'config.yaml')
    now=datetime.now(CHINA)
    if request.get('mode')=='daily':
        config=daily_config(config,now)
    else:
        # 立即预约不需要等待“开放”，只留出任务初始化的极短间隔。
        release=datetime.fromisoformat(request['run_at']) if request.get('run_at') else now+timedelta(milliseconds=100)
        if release.tzinfo is None:release=release.replace(tzinfo=CHINA)
        start=max(now,release-timedelta(minutes=2))
        config.update(start_at=start.isoformat(),release_at=release.isoformat(),stop_at=(release+timedelta(minutes=5)).isoformat())
    remember=config.get('remember_session',True)
    job=BookingJob(config,directory/'status.json',fast=config.get('fast',True),payment_qr=True,
                   login_state=LoginState() if remember else LoginState(None),
                   poll_availability=request.get('mode')!='now',
                   session=session if remember else None)
    handler=logging.FileHandler(directory/'run.log')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logging.getLogger().addHandler(handler)
    lock=(ROOT/'artifacts/booking-execution.lock').open('w')
    try:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            job.status('failed',message='已有预约任务运行，请先处理当前任务。');return
        try:
            job.captcha_solver=solver or captcha_solver()
            job.run(browser,env.get('BUAA_SSO_USER',''),env.get('BUAA_SSO_PASSWORD',''))
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
    finally:
        lock.close()
        logging.getLogger().removeHandler(handler);handler.close()


def warm_tabs(session, env):
    """空闲时预先打开当前预约规则里的所有场馆，下次预约只需刷新余票。
    维护时段、账号不一致或配置异常时不预热；预热失败不影响其他命令。"""
    if env.get('CGYY_WARM_TABS','1')=='0' or session.client is None:
        return
    if not 5<=datetime.now(CHINA).hour<23:
        session.close_tabs();return
    path=ROOT/'artifacts/web/config.yaml'
    try:
        config=load_config(path if path.exists() else DEFAULT_CONFIG)
        if config.get('account_username') not in (None,'',session.user) or not config.get('venues'):
            return
        try:desired=target_date(config)
        except ReservationError:desired=target_date(dict(config,reserve_date=None))
        session.warm_up(config,desired)
    except Exception as error:
        LOG.info('预先打开场馆页面未完成（%s），预约时再加载。',type(error).__name__)
        session.close_tabs()


def failure(error):
    if isinstance(error,(ReservationError,PaymentError,ValueError)) and str(error):
        return {'ok':False,'message':str(error)}
    return {'ok':False,'message':'登录未完成，请检查学校账号、密码或统一认证的验证要求。'}


def serve():
    """常驻进程：stdin 每行一个命令；stdin 关闭后处理完当前任务再退出。"""
    protocol=sys.stdout;sys.stdout=sys.stderr  # stdout 只用于协议，其他输出进日志。
    def send(message):
        protocol.write(json.dumps(message,ensure_ascii=False)+'\n');protocol.flush()
    commands=queue.Queue()
    def read():
        for line in sys.stdin:
            try:commands.put(json.loads(line))
            except ValueError:continue
        commands.put(None)
    threading.Thread(target=read,daemon=True).start()
    with sync_playwright() as p:
        browser=p.chromium.launch(**browser_options(local_environment()))
        session=BrowserSession(browser)
        solver=None
        try:
            while True:
                try:command=commands.get(timeout=KEEP_ALIVE_SECONDS)
                except queue.Empty:
                    try:session.keep_alive()
                    except Exception:session.mark_invalid()
                    warm_tabs(session,local_environment())
                    continue
                if command is None:return
                env=local_environment();os.environ.update(env)
                ident,op=command.get('id'),command.get('op')
                try:
                    if op=='login':
                        send(dict(login(session,command,env),id=ident))
                        if solver is None:
                            # 登录后的空闲时间预先加载验证码模型，预约时不再等待。
                            try:solver=captcha_solver()
                            except Exception:LOG.warning('验证码模型预加载失败，预约时再尝试。')
                        warm_tabs(session,env)
                    elif op=='recheck':
                        send(dict(recheck(session,command,env),id=ident))
                    elif op=='book':
                        send({'id':ident,'ok':True})
                        try:
                            if solver is None:solver=captcha_solver()
                        except Exception:solver=None
                        try:book(browser,command,env,session=session,solver=solver)
                        finally:send({'event':'finished','task':command.get('task')})
                        warm_tabs(session,local_environment())
                    else:
                        send({'id':ident,'ok':False,'message':'未知命令。'})
                except Exception as error:
                    LOG.warning('命令 %s 未完成（%s）。',op,type(error).__name__)
                    send(dict(failure(error),id=ident))
        finally:
            session.close();browser.close()


def run():
    os.chdir(ROOT);os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    mode = sys.argv[1]
    if mode=='serve':
        serve();return
    request = json.load(sys.stdin)
    env = local_environment();os.environ.update(env)
    if mode=='book':
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(**browser_options(env))
                try:book(browser,request,env)
                finally:browser.close()
        except Exception:
            status=Path(request['directory'])/'status.json'
            state=json.loads(status.read_text()) if status.exists() else {}
            if state.get('status') in {None,'queued'}:
                state.update(status='failed',message='浏览器未能启动，请检查运行环境。',updated_at=datetime.now(CHINA).isoformat())
                status.write_text(json.dumps(state,ensure_ascii=False))
        return
    with sync_playwright() as p:
        browser=p.chromium.launch(**browser_options(env))
        try:
            if mode in {'login','recheck'}:
                session=BrowserSession(browser)
                try:
                    answer=(login if mode=='login' else recheck)(session,request,env)
                except Exception as error:
                    answer=failure(error)
                finally:
                    session.close()
                print(json.dumps(answer,ensure_ascii=False),flush=True)
        finally:browser.close()

if __name__=='__main__':
    try:run()
    except Exception:
        print(json.dumps({'ok':False,'message':'登录未完成，请检查学校账号、密码或统一认证的验证要求。'},ensure_ascii=False),flush=True)
        sys.exit(1)
