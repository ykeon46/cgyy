# 北航羽毛球预约控制台

本机前端支持学校统一认证登录、可视化配置预约、立即/定时预约、每日任务、订单状态，以及校园付官方付款二维码。付款由本人用微信、支付宝或云闪付扫码完成，程序不保存第三方支付账号，也不自动扣款。

默认规则：每天北京时间06:05登录，07:00预约提前两天的20:00–22:00，学院路主馆或副馆的任一连续场地，排除沙河。开放前每5分钟核验登录态，最晚08:00结束；一旦提交订单就停止寻找其他场地。

## 启动前端

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-captcha.txt
python -m playwright install chromium
git clone https://github.com/MgArcher/Text_select_captcha.git artifacts/Text_select_captcha
# Linux 服务器准备 Xvfb、xauth 和浏览器所需系统库后启动
chmod +x deploy/run_web.sh deploy/run_daily_booking.sh
deploy/run_web.sh
```

打开 **http://127.0.0.1:8765**，填写北航统一认证的学号/工号和密码。服务器运行在另一台机器时，可以用 SSH 转发8765端口后访问本机地址。服务默认仅监听127.0.0.1。

本机转发端口可与服务端不同，例如 `ssh -N -L 50293:127.0.0.1:8765 用户名@服务器` 后访问 `http://localhost:50293/`。

登录后可选择场馆、日期、提前天数、起止时间、场地偏好、同伴、联系电话、订单金额上限、定时启动时间、每日登录/开放/截止时间、登录态核验间隔、快速下单和登录态复用。

- **立即预约**：按当前配置提交一笔真实订单，随后显示官方付款码。
- **定时预约**：按指定的北京时间开始，提前准备学校登录和页面。
- **每日自动**：保存每天滚动执行的规则，开关同步到本机systemd定时器。
- **付款面板**：显示真实订单、金额及场馆付款倒计时，过期时隐藏二维码。只有学校订单返回已支付且有效，才显示预约成功。
- **任务记录**：查看前端和每日任务的最新状态。

当前实测的场馆付款窗口约1分钟，从订单创建开始计时。校园付自身可能显示更长的倒计时，这不会延长场地保留时间。服务器返回的余票、规则、价格和付款期限可能变化，以当次订单为准。

学校账号密码保存于本机被Git忽略的 `.env`，权限为0600，以供每日任务在登录态失效后重新登录；前端不会回传密码。Cookie和localStorage令牌保存在 `artifacts/auth/school-session.json`，仅包含学校域名的数据。预约配置与任务记录保存在 `artifacts/web/` 和 `artifacts/daily/`，均不提交Git。请勿共享这些文件。

## 每日任务

`deploy/run_daily_booking.sh` 读取 `.env`，优先使用前端保存的 `artifacts/web/config.yaml`，否则使用 `configs/default.yaml`。服务器通过有界面的Chromium和Xvfb运行；可选的本机浏览器路径、共享库等配置放在 `artifacts/runtime/environment.sh`。验证码使用本地ONNX模型，不跳过网站验证。

```bash
# 只校验下一次计划，不登录或下单
python scheduled_booking.py --daily --validate

# 启动一轮每日任务；会等待到配置的开始时间
./deploy/run_daily_booking.sh
```

安装每日定时器及前端后台服务：

```bash
mkdir -p ~/.config/systemd/user
python - <<'SETUP'
from pathlib import Path
project = Path.cwd()
units = Path.home() / '.config/systemd/user'
for name in ['cgyy-daily', 'cgyy-web']:
    (units / (name+'.service')).write_text(
        (project / ('deploy/'+name+'.service.in')).read_text().replace('@PROJECT_ROOT@', str(project)))
(units / 'cgyy-daily.timer').write_text((project / 'deploy/cgyy-daily.timer').read_text())
SETUP
systemctl --user daemon-reload
systemctl --user enable --now cgyy-daily.timer cgyy-web.service
systemctl --user list-timers cgyy-daily.timer
journalctl --user -u cgyy-daily.service -f
```

本机每日定时器默认06:05启动；前端修改登录时间时也会同步调整下一次启动时间。机器和网络需要在任务期间保持运行。关闭定时器不会取消已经生成的订单，需在场馆网站核对。

## 命令行

```bash
# 仅查询
python cgyy.py --config configs/default.yaml
# 创建订单并保存官方付款二维码
python cgyy.py --config configs/local.yaml --submit --payment-qr \
  --captcha-repo artifacts/Text_select_captcha --ignore-password-warning
```

无界面下单需同时指定 `--payment-qr`。`--fast` 启用10秒下单预算；`--at` 可提前登录并准备页面。已经在非高峰真实测试中实现3.28秒生成待付款订单，但不能保证7点高峰或每次验证码都成功。服务器明确拒绝下单与提交结果未知分别记录；结果未知时不自动重试，避免重复预约。

## 验证

```bash
python -m unittest discover -s tests -v
```

本地测试拦截网络，覆盖场地选择、验证码、登录态、每日日期滚动、官方二维码核对、过期处理和前端接口鉴权。二维码测试只创建校园付交易，不提交银行卡或第三方账户付款。

## License

[MIT](LICENSE)
