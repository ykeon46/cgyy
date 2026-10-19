"""本机预约控制台；默认只监听 127.0.0.1。"""
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
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


def get_config():
    return load_config(CONFIG if CONFIG.exists() else DEFAULT_CONFIG)


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


def job_states():
    paths=list((DATA/'jobs').glob('*/status.json'))+list((ROOT/'artifacts/daily').glob('*/status.json'))
    result=[]
    for path in paths:
        try:
            state=json.loads(path.read_text())
            state['id']=path.parent.name
            state['source']='daily' if 'daily' in path.parts else 'web'
            result.append(state)
        except (OSError,ValueError):continue
    return sorted(result,key=lambda s:s.get('updated_at',''),reverse=True)[:30]


def spawn_worker(action,request):
    env=local_environment()
    private_display=ROOT/'artifacts/runtime/x11/usr/bin/xvfb-run'
    display=str(private_display) if private_display.exists() else shutil.which('xvfb-run')
    command=[sys.executable,str(ROOT/'web_worker.py'),action]
    if not env.get('DISPLAY') and display:command=[display,'-a','-s','-screen 0 1440x1000x24 -nolisten tcp',*command]
    runtime=ROOT/'artifacts/runtime'
    if runtime.exists():
        env['PATH']=str(runtime/'x11/usr/bin')+os.pathsep+env.get('PATH','')
        env['LD_LIBRARY_PATH']=str(runtime/'x11/usr/lib/x86_64-linux-gnu')+':'+str(runtime/'browser-libs')+':'+env.get('LD_LIBRARY_PATH','')
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
    subprocess.run(['systemctl','--user','daemon-reload'],check=True,capture_output=True)
    subprocess.run(['systemctl','--user',*(['enable','--now'] if enabled else ['disable','--now']),'cgyy-daily.timer'],check=True,capture_output=True)
    if enabled:subprocess.run(['systemctl','--user','restart','cgyy-daily.timer'],check=True,capture_output=True)


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

    def session(self):
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''))
            return SESSIONS.get(cookie['cgyy_session'].value)
        except (KeyError,ValueError):return None

    def do_GET(self):
        path=urlparse(self.path).path
        files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path in files:
            target=ROOT/'web'/files[path]
            self.send_response(200);self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[target.suffix[1:]])
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers();self.wfile.write(target.read_bytes());return
        if path=='/api/session':
            self.json({'session':self.session(),'server_now':datetime.now(CHINA).isoformat()});return
        if not self.session():self.json({'error':'请先登录学校账号。'},401);return
        if path=='/api/config':
            state=get_config();state.pop('sso',None)
            enabled=subprocess.run(['systemctl','--user','is-enabled','cgyy-daily.timer'],capture_output=True,text=True).returncode==0
            self.json({'config':state,'daily_enabled':enabled});return
        if path=='/api/jobs':self.json({'jobs':job_states(),'server_now':datetime.now(CHINA).isoformat()});return
        self.json({'error':'未找到接口。'},404)

    def do_POST(self):
        host=self.headers.get('Host','')
        origin=self.headers.get('Origin')
        if host not in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'} or self.headers.get('X-CGYY-Client')!='1' or origin and origin not in {'http://'+host}:
            self.json({'error':'请从本机控制台操作。'},403);return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32768:raise ValueError('请求大小不正确。')
            body=json.loads(self.rfile.read(length))
            path=urlparse(self.path).path
            if path=='/api/login':
                user,password=body.get('username','').strip(),body.get('password','')
                if not isinstance(password,str) or not user or not password or len(user)>100 or len(password)>500:raise ValueError('请填写学校统一认证账号和密码。')
                with MUTEX:
                    answer=spawn_worker('login',{'username':user,'password':password})
                token=secrets.token_urlsafe(32)
                SESSIONS.clear()
                SESSIONS[token]={key:answer[key] for key in ['username','buddies']}
                self.json({'session':SESSIONS[token]},cookie=f'cgyy_session={token}; HttpOnly; SameSite=Strict; Path=/');return
            if not self.session():self.json({'error':'请先登录学校账号。'},401);return
            if path=='/api/logout':
                cookie=SimpleCookie(self.headers.get('Cookie',''));SESSIONS.pop(cookie['cgyy_session'].value,None)
                self.json({'ok':True},cookie='cgyy_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/');return
            if path=='/api/config':
                config=validate_config(body['config'])
                DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
                temporary=CONFIG.with_suffix('.tmp');temporary.write_text(yaml.safe_dump(config,allow_unicode=True));temporary.chmod(0o600);temporary.replace(CONFIG)
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
                    for process in PROCESSES.values():
                        if process.poll() is None:raise ValueError('已有任务正在运行，请先处理当前任务。')
                    if any(state['status'] in {'needs_attention','qr_needs_attention','submitting','awaiting_payment'} for state in job_states()[:1]):
                        raise ValueError('上一笔预约尚待核对，请先确认场馆订单状态，避免重复下单。')
                    task_id=uuid.uuid4().hex
                    directory=DATA/'jobs'/task_id;directory.mkdir(parents=True,mode=0o700)
                    (directory/'config.yaml').write_text(yaml.safe_dump(config,allow_unicode=True))
                    write_json(directory/'status.json',{'status':'queued','updated_at':datetime.now(CHINA).isoformat(),'date':config.get('reserve_date'),'hours':config['reserve_time']})
                    PROCESSES[task_id]=spawn_worker('book',{'directory':str(directory),'mode':mode,'run_at':run_at})
                self.json({'id':task_id});return
            if path=='/api/cancel':
                task_id=body.get('id','');state=next((state for state in job_states() if state['id']==task_id),None)
                if not state or state['status'] not in ACTIVE or state['status'] in {'submitting','locked','awaiting_payment'}:raise ValueError('当前阶段不能停止，请核对已生成的订单。')
                target=(ROOT/'artifacts/daily' if state['source']=='daily' else DATA/'jobs')/task_id
                (target/'cancel.request').write_text('stop')
                self.json({'ok':True});return
            if path=='/api/recheck':
                task_id=body.get('id','')
                state=next((item for item in job_states() if item['id']==task_id),None)
                if not state or not state.get('trade_no'):raise ValueError('此任务还没有已确认的订单编号，请到场馆网站核对。')
                result=spawn_worker('recheck',{'trade_no':state['trade_no']})
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
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    os.chdir(ROOT);os.umask(0o077);DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'预约控制台：http://127.0.0.1:{args.port}',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
