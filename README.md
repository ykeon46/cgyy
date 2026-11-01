# 北航羽毛球预约控制台

本机前端支持学校统一认证登录、可视化配置预约、立即/定时预约、每日任务、订单状态，以及官方交易付款二维码。优先获取支付宝原生交易码，由本人使用已登录的手机支付宝扫码确认付款；程序不保存第三方支付账号，也不自动扣款。若官方页面未提供交易码，返回校园付入口码，并明确提示可能需要登录收银台。

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

- **立即预约**：查询一次所选场馆，有空场则提交一笔真实订单并显示官方付款码，无空场立即返回，不持续轮询。
- **定时预约**：按指定的北京时间开始，提前准备学校登录和页面。
- **每日自动**：保存每天滚动执行的规则，开关同步到本机systemd定时器。
- **付款面板**：区分支付宝交易付款码与校园付收银台入口码，显示真实订单、金额及场馆付款倒计时，过期时隐藏二维码。生成支付宝交易码前核对收款方、金额及二维码实际编码地址，不填写支付账号密码。只有学校订单返回已支付且有效，才显示预约成功。
- **任务记录**：查看前端和每日任务的最新状态。
- **进度与付款码**：网页和小程序每0.5秒刷新任务，网页显示当前阶段的已等待时间。官方校园付入口码取得后立即显示，再更新为核对过的支付宝交易码；付款状态查询每次响应后间隔1秒。付款仍由本人在支付宝确认，以学校返回已支付且有效为准。

当前实测的场馆付款窗口约1分钟，从订单创建开始计时。校园付自身可能显示更长的倒计时，这不会延长场地保留时间。服务器返回的余票、规则、价格和付款期限可能变化，以当次订单为准。

学校账号密码保存于本机被Git忽略的 `.env`，权限为0600，以供每日任务在登录态失效后重新登录；前端不会回传密码。Cookie和localStorage令牌保存在 `artifacts/auth/school-session.json`，仅包含学校域名的数据。预约配置与任务记录保存在 `artifacts/web/` 和 `artifacts/daily/`，均不提交Git。请勿共享这些文件。

### 常驻浏览器

首次登录后，服务端保留一个常驻浏览器进程和已登录的学校页面，日志写入 `artifacts/web/session-worker.log`。再次登录只需一次读取同伴的请求，立即预约、定时预约和核对订单都直接使用这个会话，不再重新启动浏览器、恢复登录态和加载验证码模型。会话在最近一次成功请求后60秒内直接复用，超过后先发一个只读请求核验，失效时才恢复缓存登录态或重新登录。空闲时每4分钟核验一次登录态。

登录后和每次预约结束后，常驻浏览器会按当前预约规则预先打开所有场馆的预约页，之后只保持登录核验，不轮询余票。预约时直接接管这些标签页，切换日期后同时刷新所有场馆的余票。维护时段不预热；设置 `CGYY_WARM_TABS=0` 可关闭预热。没有预热页面时，每个场馆各开一个标签页并同时加载到目标日期，主馆没有空场时副馆已经就绪。开放时刻同时刷新所有已准备场馆的余票。预约任务运行期间的其他登录或核对请求，会退回一次性进程处理。控制台重启时常驻进程会在当前任务结束后退出。在 `.env` 中设置 `CGYY_PERSISTENT_BROWSER=0` 可恢复为每次单独启动浏览器。

### 浏览器标识与进度

学校网关会让标识中带 `HeadlessChrome` 的请求每次固定延迟约5秒。无界面运行时，学校域名的浏览器上下文使用同一浏览器自身的标识，只去掉 Headless 字样；有界面运行时标识本来就不含该字样。实测登录从约21秒降到1秒以内，同时准备两个场馆从约27秒降到2秒以内。

任务进度按步骤显示当前在做什么，例如打开场馆、切换日期、刷新余票、识别验证码、等待订单确认、申请付款交易，并显示本步和整个任务已等待的秒数。

## 手机推送

场馆付款窗口约1分钟，每日任务在清晨下单时人往往不在电脑前。在 `.env` 中设置 `CGYY_NOTIFY_URL` 后，订单生成、付款码就绪、付款成功或过期、需要核对时都会推送到手机。付款码就绪的推送直接带支付宝交易链接，在手机上点开即可跳转支付宝付款，不必扫码。

```bash
# 任选一种
export CGYY_NOTIFY_URL='https://api.day.app/你的Bark密钥'          # iOS Bark，按时效性通知提醒
export CGYY_NOTIFY_URL='https://sctapi.ftqq.com/你的SendKey.send'  # Server酱，推送到微信
export CGYY_NOTIFY_URL='https://ntfy.sh/你的主题名'                # ntfy，Android 和 iOS
```

自建的 Bark 或 ntfy 服务加前缀，例如 `bark+https://push.example.com/密钥`。其他地址按 JSON `{title, body, url}` 发送 POST。推送在后台发送，失败只记录日志，不影响预约和付款。推送内容包含付款链接，请只使用本人的推送账号。

## 微信小程序

`miniprogram/` 是同一控制台的小程序版本：登录、配置场次、立即/定时/每日预约、付款码与倒计时、核对订单、停止任务和预约记录，全部调用本机 `web_app.py` 的接口，不另存账号或订单数据。

1. 在 `.env` 中设置访问密钥（至少16位），没有密钥时服务端拒绝所有小程序请求：
   ```bash
   echo "export CGYY_MINIPROGRAM_KEY='$(python -c 'import secrets;print(secrets.token_urlsafe(24))')'" >> .env
   ```
2. 让手机能访问到服务。推荐保持默认的 `127.0.0.1`，由 Nginx/Caddy 以 HTTPS 域名反向代理到8765端口；仅在可信局域网内调试时才直接监听网卡地址：`deploy/run_web.sh --host 0.0.0.0`（明文HTTP会传输学校密码）。
3. 用微信开发者工具导入 `miniprogram/` 目录，项目已配置 AppID `wx705fa17426014d2c`，使用该小程序的管理员或开发者微信登录；如果使用其他小程序账号，请修改 `project.config.json` 中的 `appid`。开发调试可在“详情 → 本地设置”勾选“不校验合法域名”，真机预览时打开调试模式；正式体验版须在小程序后台把反代域名加入 request 合法域名（HTTPS且已备案）。
4. 首次打开填写服务器地址（如 `https://cgyy.example.com`）和访问密钥，测试连接后用统一认证登录。

小程序收到的是 Bearer 令牌，不使用Cookie；浏览器控制台与小程序共用单一登录会话，在一端登录会让另一端重新登录。手机上无法扫描本机屏幕，支付宝交易码可“复制支付宝付款链接”后粘贴到手机浏览器打开跳转支付宝，或“保存二维码到相册”后在支付宝扫一扫中选择相册；场馆付款窗口仍约1分钟。保存相册需要在小程序后台的用户隐私保护指引中声明相册（仅写入）权限。个人自用即可，处理第三方账号密码的小程序通常无法通过公开发布审核。

配置加载成功并取得最新任务状态后才可保存或预约；断网会提示状态可能过时，恢复连接后自动更新。待付款订单优先展示，不会被选中的历史记录遮住。跨天会刷新可选日期，手动指定且仍有效的日期会保留。

服务同时使用一个学校账号，配置和任务记录按账号保存。切换学校账号前必须处理运行中及待核对任务；切换时关闭每日定时器，恢复该账号自己的配置，需自行重新开启每日预约。旧版本未标记账号的记录在首次切换时归属原账号。若提交结果未知且没有订单号，须先在场馆官网个人中心确认没有有效或待付款订单，再点击“官网核对后结束异常任务”；此操作只结束本机异常记录，不会取消官方订单。

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

无界面下单需同时指定 `--payment-qr`。定时任务的 `--fast` 10秒预算从目标日期页面就绪后开始，覆盖读取当前余票、选场和提交；控制台“立即预约”允许45秒完成验证码与订单响应。首次登录和页面加载另设等待上限。`--at` 可提前登录并准备页面。已经在非高峰真实测试中实现3.28秒生成待付款订单，但不能保证7点高峰或每次验证码都成功。点选校验通过后仍须等待订单接口确认。仅当服务器明确返回“验证码非法校验”（业务码250或400）、未返回订单号且预算充足时，才通过网站重新生成挑战并验证一次，两次共用原时间预算。其他失败和结果未知时不自动重试，避免重复预约。

## 验证

```bash
python -m unittest discover -s tests -v
```

本地测试拦截网络，覆盖场地选择、验证码、登录态、每日日期滚动、官方二维码核对、过期处理和前端接口鉴权。二维码测试只创建校园付交易，不提交银行卡或第三方账户付款。

安装 Node.js 后，以上命令也会运行小程序页面回归测试；也可单独执行 `node --test tests/test_miniprogram.js`。这些用例模拟微信接口，覆盖付款码优先展示、加载前提交拦截、跨天日期、断网恢复和异常任务核对；真机扫码及相册权限仍需在微信中验证。

## License

[MIT](LICENSE)
