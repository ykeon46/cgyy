"""本机预约控制台；默认只监听 127.0.0.1，小程序凭本机密钥访问。"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
from urllib.parse import urlparse
import uuid

import yaml
from cgyy import CHINA, DEFAULT_CONFIG, load_config
from payment import amount
from scheduled_booking import daily_config
from web_worker import local_environment

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'artifacts/web'
CONFIG=DATA/'config.yaml'
SESSIONS={}
PROCESSES={}
MUTEX=threading.Lock()
ACTIVE={'queued','logging_in','waiting_start','waiting_release','keeping_session','session_valid','ready','checking','read_retry','waiting_availability','submitting','locked','awaiting_payment'}


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False));temporary.chmod(0o600);temporary.replace(path)


def account_config_path(username):
    return DATA/'accounts'/hashlib.sha256(username.encode()).hexdigest()/'config.yaml'


def save_config(path, config):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(yaml.safe_dump(config,allow_unicode=True))
    temporary.chmod(0o600);temporary.replace(path)


def get_config(username=None):
    config=load_config(CONFIG if CONFIG.exists() else DEFAULT_CONFIG)
    owner=config.get('account_username') or local_environment().get('BUAA_SSO_USER')
    if username and owner!=username:
        path=account_config_path(username)
        return load_config(path if path.exists() else DEFAULT_CONFIG)
    return config


@contextmanager
def execution_lock():
    path=ROOT/'artifacts/booking-execution.lock'
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('已有预约任务运行，请先停止任务或核对订单。')
        yield


def prepare_account_switch(previous):
    if any(process.poll() is None for process in PROCESSES.values()) or any(
            state['status'] in ACTIVE|{'needs_attention','qr_needs_attention'} for state in job_states(limit=None)):
        raise ValueError('切换账号前，请先停止运行中的任务并核对未完成订单。')
    if not previous:return
    # 先停止未来触发，再更换凭据；未能关闭定时器时保留原账号。
    try:subprocess.run(['systemctl','--user','disable','--now','cgyy-daily.timer'],check=True,capture_output=True)
    except FileNotFoundError:pass  # 没有 systemd 的平台不存在需要关闭的定时器。
    except (OSError,subprocess.CalledProcessError):raise ValueError('无法关闭每日定时器，暂不能切换账号，请检查部署环境。')
    config=get_config(previous)
    config['account_username']=previous
    save_config(account_config_path(previous),config)
    # 旧版本没有账号字段，切换前将历史记录绑定到原账号。
    for path in job_paths():
        try:
            state=json.loads(path.read_text())
            if not state.get('username'):
                state['username']=previous
                write_json(path,state)
        except (OSError,ValueError):continue


def activate_account(username, previous):
    if username==previous:
        config=get_config(username)
    else:
        path=account_config_path(username)
        config=load_config(path if path.exists() else DEFAULT_CONFIG)
    config['account_username']=username
    save_config(CONFIG,config)
    save_config(account_config_path(username),config)


def validate_config(value):
    if not isinstance(value,dict):raise ValueError('配置格式不正确。')
    base=load_config(DEFAULT_CONFIG)
    allowed={'venues','reserve_date','days_ahead','reserve_time','priority','candidates','phone','daily_schedule','max_pay_amount','fast','remember_session'}
    base.update({key:item for key,item in value.items() if key in allowed})
    halls=load_config(DEFAULT_CONFIG)['venues']
    if not base['venues'] or any(venue not in halls for venue in base['venues']):raise ValueError('请选择学院路主馆或副馆。')
    base['venue']=base['venues'][0]
    hours=base['reserve_time']
    if not isinstance(hours,list) or not hours or any(type(h)!=int for h in hours) or hours!=list(range(hours[0],hours[0]+len(hours))) or hours[0]<7 or hours[-1]>21:
        raise ValueError('预约时段须为07:00–22:00内的连续整小时。')
    if type(base['days_ahead'])!=int or not 0<=base['days_ahead']<=3:raise ValueError('提前天数须在0到3天之间。')
    if base.get('reserve_date'):
        selected=datetime.fromisoformat(base['reserve_date']).date()
        if not 0<=(selected-datetime.now(CHINA).date()).days<=3:raise ValueError('预约日期须在当前可选的四天范围内。')
    for key,maximum in [('priority',24),('candidates',30)]:
        items=base[key]
        if not isinstance(items,list) or len(items)!=len(set(items)) or any(type(i)!=int or not 1<=i<=maximum for i in items):raise ValueError('场地或同伴选项不正确。')
    if not base['priority']:base['priority']=list(range(1,25))
    if not isinstance(base['phone'],str) or base['phone'] and not re.fullmatch(r'1\d{10}',base['phone']):raise ValueError('请填写正确的11位手机号。')
    base['max_pay_amount']=str(amount(base.get('max_pay_amount','70.00')))
    if amount(base['max_pay_amount'])>1000:raise ValueError('单笔上限不能超过1000元。')
    base['fast']=bool(base.get('fast',True));base['remember_session']=bool(base.get('remember_session',True))
    planned=daily_config(base)
    if not 5<=datetime.fromisoformat(planned['release_at']).hour<23:raise ValueError('每日开始预约时间须在场馆05:00–23:00开放时间内。')
    base['sso']={'user':'','passwd':''}
    return base


def job_paths():
    return list((DATA/'jobs').glob('*/status.json'))+list((ROOT/'artifacts/daily').glob('*/status.json'))


def job_states(username=None, limit=30):
    paths=job_paths()
    legacy_owner=local_environment().get('BUAA_SSO_USER')
    result=[]
    for path in paths:
        try:
            state=json.loads(path.read_text())
            if username and (state.get('username') or legacy_owner)!=username:continue
            state['id']=path.parent.name
            state['source']='daily' if 'daily' in path.parts else 'web'
            result.append(state)
        except (OSError,ValueError):continue
    ordered=sorted(result,key=lambda s:s.get('updated_at',''),reverse=True)
    if limit is None:return ordered
    # 未完成任务不能被历史条数限制隐藏，否则用户无法核对后解除阻塞。
    return ordered[:limit]+[state for state in ordered[limit:] if state['status'] in ACTIVE|{'needs_attention','qr_needs_attention'}]


def worker_command(action):
    env=local_environment()
    private_display=ROOT/'artifacts/runtime/x11/usr/bin/xvfb-run'
    display=str(private_display) if private_display.exists() else shutil.which('xvfb-run')
    command=[sys.executable,str(ROOT/'web_worker.py'),action]
    if not env.get('DISPLAY') and display:command=[display,'-a','-s','-screen 0 1440x1000x24 -nolisten tcp',*command]
    runtime=ROOT/'artifacts/runtime'
    if runtime.exists():
        env['PATH']=str(runtime/'x11/usr/bin')+os.pathsep+env.get('PATH','')
        env['LD_LIBRARY_PATH']=str(runtime/'x11/usr/lib/x86_64-linux-gnu')+':'+str(runtime/'browser-libs')+':'+env.get('LD_LIBRARY_PATH','')
    return command,env


# 进程退出时仍处于这些阶段的任务，没有提交订单。
UNSUBMITTED={'queued','logging_in','waiting_start','waiting_release','keeping_session','session_valid','ready','checking','read_retry','waiting_availability'}


class WorkerTask:
    """常驻进程中运行的预约任务，提供与 subprocess.Popen 相同的 poll()。"""
    def __init__(self,worker,task_id,directory):
        self.worker=worker;self.id=task_id;self.directory=Path(directory);self.returncode=None
    def poll(self):
        if self.returncode is None and not self.worker.alive():self.worker.task_ended(self,1)
        return self.returncode


class SessionWorker:
    """常驻浏览器进程：保留已登录的学校页面，登录、预约、核对订单都不必重新启动浏览器和恢复登录态。
    同一时间只处理一个命令；预约任务运行期间的其他请求退回一次性进程。"""
    def __init__(self):
        self.process=None;self.lock=threading.Lock();self.replies={};self.task=None

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        command,env=worker_command('serve')
        DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (DATA/'session-worker.log').open('a') as log:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,
                                     env=env,cwd=ROOT,start_new_session=True,bufsize=1)
        self.process=process
        threading.Thread(target=self.read,args=(process,),daemon=True).start()

    def read(self,process):
        for line in process.stdout:
            try:message=json.loads(line)
            except ValueError:continue
            if message.get('event')=='finished':
                task=self.task
                if task and task.id==message.get('task'):self.task_ended(task,0)
                continue
            box=self.replies.pop(message.get('id'),None)
            if box:box.put(message)
        process.wait()
        for box in list(self.replies.values()):box.put(None)
        self.replies.clear()
        if self.task:self.task_ended(self.task,process.returncode or 1)

    def task_ended(self,task,returncode):
        if task.returncode is not None:return
        task.returncode=returncode
        if self.task is task:self.task=None
        if returncode:
            # 进程意外退出：未提交的任务标记失败；已提交的要求核对，避免误判或重复下单。
            path=task.directory/'status.json'
            try:state=json.loads(path.read_text())
            except (OSError,ValueError):return
            if state.get('status') in UNSUBMITTED:
                state.update(status='failed',message='浏览器服务意外退出，未提交订单，请重新预约。')
            elif state.get('status') in ACTIVE:
                state.update(status='qr_needs_attention' if state.get('trade_no') else 'needs_attention',
                             message='浏览器服务意外退出，请到场馆网站核对订单，不自动重试。',qr_code=None)
            else:return
            state['updated_at']=datetime.now(CHINA).isoformat()
            write_json(path,state)

    def busy(self):
        return self.task is not None and self.task.poll() is None

    def send(self,message,timeout):
        """发送命令并等待回复；进程不可用时返回 None，由调用方退回一次性进程。"""
        if not self.alive():self.start()
        box=queue.Queue();self.replies[message['id']]=box
        try:
            self.process.stdin.write(json.dumps(message,ensure_ascii=False)+'\n');self.process.stdin.flush()
        except OSError:
            self.replies.pop(message['id'],None);return None
        try:return box.get(timeout=timeout)
        except queue.Empty:
            # 卡住的浏览器会阻塞后续命令，结束它；下次请求重新启动。
            self.replies.pop(message['id'],None);self.process.kill();return None

    def call(self,action,request,timeout=110):
        with self.lock:
            if self.busy():return None
            return self.send(dict(request,op=action,id=uuid.uuid4().hex),timeout)

    def book(self,request):
        with self.lock:
            if self.busy():return None
            task=WorkerTask(self,Path(request['directory']).name,request['directory'])
            self.task=task
            # 常驻进程可能正在预热场馆页面，处理完当前命令才会确认。
            answer=self.send(dict(request,op='book',task=task.id,id=uuid.uuid4().hex),120)
            if not answer or not answer.get('ok'):
                if self.task is task:self.task=None
                task.returncode=1
                return None
            return task


SESSION_WORKER=SessionWorker()


def persistent_browser():
    return local_environment().get('CGYY_PERSISTENT_BROWSER','1')!='0'


def spawn_worker(action,request):
    if persistent_browser():
        if action in {'login','recheck'}:
            answer=SESSION_WORKER.call(action,request)
            if answer is not None:
                if not answer.get('ok'):raise ValueError(answer.get('message','登录失败。'))
                return answer
        else:
            task=SESSION_WORKER.book(request)
            if task is not None:return task
    command,env=worker_command(action)
    if action in {'login','recheck'}:
        result=subprocess.run(command,input=json.dumps(request),text=True,capture_output=True,env=env,cwd=ROOT,timeout=110)
        try:answer=json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError,IndexError):raise ValueError('登录服务未完成，请检查浏览器运行环境。')
        if not answer.get('ok'):raise ValueError(answer.get('message','登录失败。'))
        return answer
    directory=Path(request['directory'])
    with (directory/'run.log').open('w') as log:
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=log,stderr=log,text=True,env=env,cwd=ROOT,start_new_session=True)
    process.stdin.write(json.dumps(request));process.stdin.close()
    return process


def apply_schedule(config,enabled):
    if not enabled:
        try:subprocess.run(['systemctl','--user','disable','--now','cgyy-daily.timer'],check=True,capture_output=True)
        except FileNotFoundError:return
        return
    units=Path.home()/'.config/systemd/user'
    units.mkdir(parents=True,exist_ok=True)
    timer=(ROOT/'deploy/cgyy-daily.timer').read_text()
    start=datetime.fromisoformat(daily_config(config)['start_at']).strftime('%H:%M:%S')
    timer=re.sub(r'^OnCalendar=.*$',f'OnCalendar=*-*-* {start} Asia/Shanghai',timer,flags=re.M)
    path=units/'cgyy-daily.timer';path.write_text(timer);path.chmod(0o600)
    service=units/'cgyy-daily.service'
    if not service.exists():
        service.write_text((ROOT/'deploy/cgyy-daily.service.in').read_text().replace('@PROJECT_ROOT@',str(ROOT)))
        service.chmod(0o600)
    try:subprocess.run(['systemctl','--user','daemon-reload'],check=True,capture_output=True)
    except FileNotFoundError:raise ValueError('当前系统不支持每日自动预约，请在 Linux 服务器配置 systemd 定时器；本机可使用立即预约或指定时间预约。') from None
    subprocess.run(['systemctl','--user',*(['enable','--now'] if enabled else ['disable','--now']),'cgyy-daily.timer'],check=True,capture_output=True)
    if enabled:subprocess.run(['systemctl','--user','restart','cgyy-daily.timer'],check=True,capture_output=True)


def local_console_request(headers):
    """Allow loopback forwarding ports; compare Origin with the browser's Host."""
    if headers.get('X-CGYY-Client')!='1':return False
    try:
        host=headers.get('Host','')
        target=urlparse('http://'+host)
        if target.hostname not in {'localhost','127.0.0.1','::1'}:return False
        if target.username is not None or target.password is not None or target.path or target.params or target.query or target.fragment:return False
        port=target.port if target.port is not None else 80
        if not 1<=port<=65535:return False
        origin=headers.get('Origin')
        if origin is None:return True
        source=urlparse(origin)
        return (source.scheme=='http' and source.netloc==target.netloc
                and not (source.path or source.params or source.query or source.fragment))
    except ValueError:return False


def miniprogram_request(headers):
    """小程序请求不带Origin和Cookie，改用.env中的访问密钥；未配置密钥时拒绝。"""
    if headers.get('X-CGYY-Client')!='miniprogram':return False
    key=local_environment().get('CGYY_MINIPROGRAM_KEY','')
    return len(key)>=16 and secrets.compare_digest(headers.get('X-CGYY-Key','').encode(),key.encode())


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def json(self,value,status=200,cookie=None):
        raw=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(raw)

    def token(self):
        if self.headers.get('X-CGYY-Client')=='miniprogram':
            value=self.headers.get('Authorization','')
            return value.removeprefix('Bearer ') if value.startswith('Bearer ') and miniprogram_request(self.headers) else None
        try:return SimpleCookie(self.headers.get('Cookie',''))['cgyy_session'].value
        except (KeyError,ValueError):return None

    def session(self):
        return SESSIONS.get(self.token())

    def do_GET(self):
        path=urlparse(self.path).path
        files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path in files:
            target=ROOT/'web'/files[path]
            self.send_response(200);self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[target.suffix[1:]])
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers();self.wfile.write(target.read_bytes());return
        if self.headers.get('X-CGYY-Client')=='miniprogram' and not miniprogram_request(self.headers):
            self.json({'error':'小程序访问密钥不正确，或服务器未配置 CGYY_MINIPROGRAM_KEY。'},403);return
        if path=='/api/session':
            self.json({'session':self.session(),'server_now':datetime.now(CHINA).isoformat()});return
        if not self.session():self.json({'error':'请先登录学校账号。'},401);return
        if path=='/api/config':
            state=get_config(self.session()['username']);state.pop('sso',None)
            try:enabled=subprocess.run(['systemctl','--user','is-enabled','cgyy-daily.timer'],capture_output=True,text=True).returncode==0
            except FileNotFoundError:enabled=False
            self.json({'config':state,'daily_enabled':enabled});return
        if path=='/api/jobs':self.json({'jobs':job_states(self.session()['username']),'server_now':datetime.now(CHINA).isoformat()});return
        self.json({'error':'未找到接口。'},404)

    def do_POST(self):
        miniprogram=miniprogram_request(self.headers)
        if not (miniprogram or local_console_request(self.headers)):
            self.json({'error':'请求来源校验失败，请刷新当前控制台页面后重试。'},403);return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32768:raise ValueError('请求大小不正确。')
            body=json.loads(self.rfile.read(length))
            path=urlparse(self.path).path
            if path=='/api/login':
                user,password=body.get('username','').strip(),body.get('password','')
                if not isinstance(password,str) or not user or not password or len(user)>100 or len(password)>500:raise ValueError('请填写学校统一认证账号和密码。')
                with MUTEX:
                    previous=local_environment().get('BUAA_SSO_USER','')
                    if previous!=user:
                        with execution_lock():
                            prepare_account_switch(previous)
                            answer=spawn_worker('login',{'username':user,'password':password})
                            activate_account(user,previous)
                    else:
                        answer=spawn_worker('login',{'username':user,'password':password})
                    token=secrets.token_urlsafe(32)
                    SESSIONS.clear()
                    SESSIONS[token]={key:answer[key] for key in ['username','buddies']}
                if miniprogram:self.json({'session':SESSIONS[token],'token':token});return
                self.json({'session':SESSIONS[token]},cookie=f'cgyy_session={token}; HttpOnly; SameSite=Strict; Path=/');return
            if not self.session():self.json({'error':'请先登录学校账号。'},401);return
            if path=='/api/logout':
                SESSIONS.pop(self.token(),None)
                self.json({'ok':True},cookie='cgyy_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/');return
            if path=='/api/config':
                config=validate_config(body['config'])
                with MUTEX:
                    session=self.session()
                    if not session:self.json({'error':'请重新登录学校账号。'},401);return
                    config['account_username']=session['username']
                    save_config(CONFIG,config)
                    save_config(account_config_path(session['username']),config)
                    enabled=body.get('daily_enabled',False)
                    try:apply_schedule(config,enabled)
                    except (OSError,subprocess.CalledProcessError):raise ValueError('配置已保存，但系统定时器未更新，请按README检查部署环境。')
                self.json({'ok':True});return
            if path=='/api/jobs':
                config=validate_config(body['config'])
                mode=body.get('mode','now')
                if mode not in {'now','once'}:raise ValueError('请选择立即预约或指定时间。')
                if mode=='now' and not 5<=datetime.now(CHINA).hour<23:raise ValueError('场馆每日23点至次日5点维护，请选择定时预约。')
                run_at=body.get('run_at')
                if mode=='once':
                    when=datetime.fromisoformat(run_at)
                    if when.tzinfo is None:when=when.replace(tzinfo=CHINA)
                    if when<=datetime.now(CHINA):raise ValueError('启动时间须晚于当前时间。')
                    if not 5<=when.hour<23:raise ValueError('场馆每日23点至次日5点维护，请选择其他启动时间。')
                    if config.get('reserve_date') and when.date()>datetime.fromisoformat(config['reserve_date']).date():raise ValueError('开始预约的时间不能晚于预约日期。')
                with MUTEX:
                    session=self.session()
                    if not session:self.json({'error':'请重新登录学校账号。'},401);return
                    config['account_username']=session['username']
                    for process in PROCESSES.values():
                        if process.poll() is None:raise ValueError('已有任务正在运行，请先处理当前任务。')
                    if any(state['status'] in ACTIVE|{'needs_attention','qr_needs_attention'} for state in job_states(limit=None)):
                        raise ValueError('上一笔预约尚待核对，请先确认场馆订单状态，避免重复下单。')
                    task_id=uuid.uuid4().hex
                    directory=DATA/'jobs'/task_id;directory.mkdir(parents=True,mode=0o700)
                    (directory/'config.yaml').write_text(yaml.safe_dump(config,allow_unicode=True))
                    queued_at=datetime.now(CHINA).isoformat()
                    write_json(directory/'status.json',{'status':'queued','username':session['username'],'updated_at':queued_at,'started_at':queued_at,'date':config.get('reserve_date'),'hours':config['reserve_time']})
                    PROCESSES[task_id]=spawn_worker('book',{'directory':str(directory),'mode':mode,'run_at':run_at})
                self.json({'id':task_id});return
            if path=='/api/cancel':
                task_id=body.get('id','');state=next((state for state in job_states(self.session()['username'],limit=None) if state['id']==task_id),None)
                if not state or state['status'] not in ACTIVE or state['status'] in {'submitting','locked','awaiting_payment'}:raise ValueError('当前阶段不能停止，请核对已生成的订单。')
                target=(ROOT/'artifacts/daily' if state['source']=='daily' else DATA/'jobs')/task_id
                (target/'cancel.request').write_text('stop')
                self.json({'ok':True});return
            if path=='/api/resolve':
                with MUTEX,execution_lock():
                    session=self.session()
                    if not session:self.json({'error':'请重新登录学校账号。'},401);return
                    task_id=body.get('id','')
                    state=next((item for item in job_states(session['username'],limit=None) if item['id']==task_id),None)
                    if not state or state['status']!='needs_attention' or state.get('trade_no') or body.get('confirmed_no_order') is not True:
                        raise ValueError('仅可在官网确认没有有效或待付款订单后，结束无订单编号的异常任务。')
                    if any(process.poll() is None for process in PROCESSES.values()):raise ValueError('请等待当前任务结束后再核对。')
                    state.update(status='resolved',previous_status=state['status'],qr_code=None,
                                 resolved_by=session['username'],updated_at=datetime.now(CHINA).isoformat(),
                                 message='用户已在官网核对无有效或待付款订单，结束异常任务；未取消任何官方订单。')
                    target=(ROOT/'artifacts/daily' if state['source']=='daily' else DATA/'jobs')/task_id/'status.json'
                    write_json(target,state)
                self.json({'ok':True});return
            if path=='/api/recheck':
                task_id=body.get('id','')
                state=next((item for item in job_states(self.session()['username'],limit=None) if item['id']==task_id),None)
                if not state or not state.get('trade_no'):raise ValueError('此任务还没有已确认的订单编号，请到场馆网站核对。')
                result=spawn_worker('recheck',{'trade_no':state['trade_no'],'username':self.session()['username']})
                paid=str(result['payStatus'])=='2' and str(result['orderStatus'])=='1'
                cancelled=str(result['orderStatus'])=='2' or str(result['payStatus']) in {'3','4','5'}
                expired=bool(result.get('expireTime') and datetime.fromisoformat(result['expireTime']).replace(tzinfo=CHINA)<=datetime.now(CHINA))
                if paid:state.update(status='paid',message='场馆已确认付款成功，预约有效。',qr_code=None)
                elif cancelled:state.update(status='cancelled',message='场馆订单已取消或退款，请核对账单。',qr_code=None)
                elif expired:state.update(status='expired',message='付款窗口已结束，请核对账单。',qr_code=None)
                else:state.update(message='场馆订单仍待付款，请在倒计时内扫码。')
                state['updated_at']=datetime.now(CHINA).isoformat()
                target=(ROOT/'artifacts/daily' if state['source']=='daily' else DATA/'jobs')/task_id/'status.json'
                write_json(target,state);self.json({'ok':True});return
            self.json({'error':'未找到接口。'},404)
        except (ValueError,KeyError,TypeError) as error:self.json({'error':str(error)},400)
        except Exception:self.json({'error':'操作未完成，请检查任务状态；如已提交订单，请先核对预约记录。'},500)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1',help='小程序直连时可改为局域网地址；公网请经HTTPS反向代理')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    os.chdir(ROOT);os.umask(0o077);DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    server=ThreadingHTTPServer((args.host,args.port),Handler)
    print(f'预约控制台：http://127.0.0.1:{args.port}',flush=True)
    if args.host not in {'127.0.0.1','localhost','::1'}:print(f'小程序接口监听 {args.host}:{args.port}，需在.env配置CGYY_MINIPROGRAM_KEY。',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
