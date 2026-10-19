# 北航场馆预约脚本

根据 2026-10-19 的实际网页更新。旧版 Selenium 脚本不能直接按原说明运行；新版保留 `python cgyy.py` 入口，改用 Playwright 管理浏览器和等待页面，移除了固定 ChromeDriver、绝对 XPath 和旧版支付宝自动付款流程。新的支付宝账户付款由 `--auto-pay` 显式启用，凭据从环境变量读取。

## 本次实际验证

- 统一认证账号密码登录成功。遇到密码安全提醒时，可通过网站提供的“本次忽略”按钮继续；脚本需显式传入 `--ignore-password-warning`。
- 能列出当前场馆，并打开“学院路校区-体育馆主馆-羽毛球”（当前页面路径为 `/venue/venue-reservation/38`，代码不固定这个 ID）。
- 能读取日期栏、场地编号、价格和空闲状态，并翻页读取 12 块场地、07:00–22:00 的 180 个时段。
- 新版命令行完整运行验证成功：登录、打开主馆羽毛球、切换至 2026-10-20、查询 14:00–16:00；12 块场地均被课程占用，按预期返回 `3`（无可用场地）。
- 网站公告：每日 23:00–次日 05:00 运行维护，不支持预约支付。脚本在这个时间段可以查询，但拒绝提交。
- 网页说明主馆羽毛球最早可于入场前 3 日 07:00 购票；实际可选日期以日期栏为准。未开放的日期会明确报错。

经用户确认，已实际创建一笔主馆 2026-10-21 07:00–08:00、15 元的临时测试订单，并打开支付页，核对金额及“航财通·校园付”支付方式。复核时订单已取消、未付款，全程没有发起支付请求；未验证扫码或实际扣款。提交时出现了网站点选验证码，需人工完成，因此尚不能保证无人值守完成下单。默认运行只查询，不选择场地、不创建订单。

随后按用户要求继续测试另一笔同场次订单，已实际走通“场馆支付 → 使用电脑支付 → 校园付选择支付宝 → 支付宝使用电脑支付 → 登录支付宝账户付款”。支付宝官方收银台显示收款方为北京航空航天大学、金额 15 元，账户输入为手机号/邮箱，并明确要求**支付密码（不是登录密码）**。未填写支付宝凭据，未点击该表单的“下一步”，未测试实际扣款或后续二次验证。最终核对场馆订单为已取消、未支付。校园付/支付宝收银台另显示约 15 分钟的交易倒计时，这不代表场地锁定时间延长；场馆订单仍需单独核对状态。

## 安装

建议 Python 3.11 或 3.12：

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell 使用 .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Linux 如果提示缺少浏览器系统库，可使用 `python -m playwright install --with-deps chromium`（安装系统库需要相应系统权限）。无需下载或替换仓库中的旧 `chromedriver.exe`。

浏览器安装和管理方式见 [Playwright 官方说明](https://playwright.dev/python/docs/browsers)。

## 配置和运行

复制 `configs/default.yaml` 为 `configs/local.yaml`，填写学号、场馆、时间和场地优先级。`configs/local.yaml` 已被 Git 忽略。密码留空，运行时会隐藏输入；也支持环境变量 `BUAA_SSO_USER`、`BUAA_SSO_PASSWORD`，其优先级高于配置文件。

```bash
# 只检查登录（不需要等待 07:00）
python cgyy.py --config configs/local.yaml --check-login

# 需要使用网页“本次忽略”按钮时加这个参数
python cgyy.py --config configs/local.yaml --check-login --ignore-password-warning

# 列出当前场馆名称
python cgyy.py --config configs/local.yaml --list-venues

# 查询指定日期，日期请改为实际需要的日期
python cgyy.py --config configs/local.yaml --date 2026-10-20

# 无桌面环境：加 --headless；提交时还需明确指定付款预览或自动付款
python cgyy.py --config configs/local.yaml --date 2026-10-20 --headless

# 实际提交订单，浏览器保留供核对订单及手动付款
python cgyy.py --config configs/local.yaml --date 2026-10-20 --submit

# 提前两分钟登录并准备场馆页面，到指定北京时间刷新余票并预约
python cgyy.py --config configs/local.yaml --date 2026-10-22 --at 2026-10-20T07:00:00 --submit
```

如有密码提醒，上述相应命令也需加 `--ignore-password-warning`。脚本会等待该按钮倒计时结束后再点击，不修改密码。

### 复用本机登录态

现在默认保存并复用学校的 Cookie 和 localStorage 登录令牌，文件位于 `artifacts/auth/school-session.json`。只保存北航场馆和统一认证域名的数据，不保存校园付或支付宝登录态。文件权限为 `0600`，默认目录已被 Git 忽略；不应共享该文件。

后续启动先恢复缓存，核对当前账号，并通过读取本人同伴列表向服务器验证登录态。有效时不读取密码、不重新走统一认证；失效后使用全新浏览器上下文正式登录并更新缓存。缓存按账号绑定，其他账号的缓存不会使用。`--session-state 路径` 可以指定缓存位置，`--no-session-cache` 禁用缓存；命令行入口和后台任务均支持。

2026-10-19 真实只读测试：首次登录保存缓存并关闭浏览器后，在独立 Python 进程、新浏览器中约 **1.313 秒**恢复登录、核验成功，并读取场馆列表，未读取密码、未创建订单或发起支付。记录在 `artifacts/session-20261019-185939/report.json`。这次只验证了短时间内跨进程复用，不能保证登录态能保持到第二天；预约余票、订单 token 和验证码仍在使用时重新获取。

### 10 秒下单模式

`--fast` 按页面顺序寻找满足连续时段的空闲场地，找到后立即停止翻页；同页内仍使用配置的场地优先级。配合 `--at` 时，登录、场馆页面和预约表单在开放前准备，开放后调用网站自己的余票刷新方法，获取最新日期范围、余票和订单 token。10 秒预算从开放后的本轮查询开始，到服务器确认待付款订单为止，不包括提前登录，也不包括付款。服务器超时或提交结果不明时仍停止，不重复下单。

```bash
# 有界面的标准 Chromium；服务器使用此模式需先配置 DISPLAY / Xvfb
python cgyy.py --config configs/local.yaml --date 2026-10-22 \
  --at 2026-10-20T07:00:00 --fast --submit --ignore-password-warning \
  --captcha-repo artifacts/Text_select_captcha
```

2026-10-19 对照测试中，同一公开页面：Headless Chrome 标识下约 5.3 秒返回，普通 Chrome 标识下约 0.2 秒返回；实际有界面 Chromium 的只读接口约 0.075 秒。使用有界面 Chromium 后，真实测试订单 `D261019001396` 从准备就绪、刷新余票至确认订单共 **3.280 秒**（其中查询约 0.208 秒），没有发起支付，随后复核已取消、未支付。记录在 `artifacts/fast-20261019-182134/`。前一轮被服务器以“验证码非法校验”拒绝，见 `artifacts/fast-20261019-182021/`；一次成功不能保证每次成功，也不能代替 07:00 放票高峰测试。从全新进程启动、登录开始的总耗时仍可能超过 10 秒。

配置字段：

| 字段 | 含义 |
| --- | --- |
| `venue` | 完整场馆名称或唯一匹配片段，建议先运行 `--list-venues` |
| `reserve_date` | `YYYY-MM-DD`；命令行 `--date` 优先 |
| `days_ahead` | `reserve_date` 为空时，预约北京时间今天之后几天，默认 2 |
| `reserve_time` | 默认 `[20, 21]`，同一场地连续 20:00–22:00；目前支持整小时时段 |
| `venues` | 每日任务的候选场馆，默认学院路主馆和副馆，不包含沙河 |
| `daily_schedule` | 默认06:05登录、07:00开放、08:00截止，开放前每300秒检查登录态 |
| `priority` | 优先尝试的场地编号，例如 `[3, 1, 2]`；随后检查页面上其他编号，不依赖表格行顺序。副馆实际为 17–24 号 |
| `candidates` | 网页同伴列表序号，例如 `[1]`；无需同伴则填 `[]`；兼容旧 `candidate` 字段 |
| `phone` | 页面要求手机号且未自动带出时填写，建议加引号 |

### 支付宝环境变量与自动付款

| 环境变量 | 内容 |
| --- | --- |
| `BUAA_SSO_USER` | 学校统一认证学号/工号 |
| `BUAA_SSO_PASSWORD` | 学校统一认证密码 |
| `ALIPAY_ACCOUNT` | 支付宝账号，手机号或邮箱 |
| `ALIPAY_PAY_PASSWORD` | 支付宝**支付密码**，不是登录密码 |
| `CGYY_MAX_PAY_AMOUNT` | 人民币单笔付款上限，例如 `15.00` |

支付宝凭据仅从环境变量读取，不写入 YAML、结果 JSON 或日志。可以在本机复制 `.env.example` 为 `.env` 后填写，再于运行脚本的同一终端执行 `source .env`；脚本不会自动加载 `.env`。已经启动的进程不会继承另一终端后来设置的变量。仅配置环境变量不会触发付款。

下面命令只校验付款环境变量，不登录、不创建订单、不付款：

```bash
python -c "from payment import PaymentSettings; PaymentSettings.from_env(charge=True); print('付款环境变量已配置')"
```

先核对 `configs/local.yaml` 中的场馆、日期和时段，然后选择运行方式：

```bash
# 创建订单并走到支付宝账户付款页，不填写凭据或提交付款
python cgyy.py --config configs/local.yaml --submit --payment-preview --ignore-password-warning

# 可能实际扣款：核对订单后填写环境变量中的账号及支付密码，完成账户验证，并在核对后的最终收银台点击一次付款
python cgyy.py --config configs/local.yaml --submit --auto-pay --ignore-password-warning
```

`--max-pay-amount 15.00` 可覆盖环境变量的单笔上限。付款前检查所选场次价格、服务器订单的场馆/日期/场地/时段、支付状态和剩余时间，并核对支付宝收款方及金额。默认只查询；`--submit` 单独使用时仍由本人付款。付款预览和自动付款可以配合 `--headless`，但网站验证码或二次验证可能需要人工处理；有界面提交会等待最多 120 秒供本人完成下单验证码；显式指定 `--captcha-repo` 时，也可在无界面模式自动处理场馆点选验证码。

自动付款会完成支付宝账户验证，并在已识别的最终收银台核对账户、订单名称及金额，再激活一次正式付款按钮。银行卡验证码、手机确认等额外步骤仍可能需要本人处理。只有场馆订单返回“已支付、正常”才记录付款成功；真实测试结果以本次运行的付款状态文件为准。

2026-10-19 的真实流程测试已验证：自动识别并通过场馆点选验证码、创建 15 元副馆订单、完成支付宝账户验证并进入最终收银台。最终付款控件位于 `securitycore.alipay.com/validate.htm` 的嵌入表单，需要再次输入支付密码并点击“确定”；页面的“支付15.00元”是金额标签。最后一轮支付宝返回“系统有点儿忙，一会儿再试试”，**尚未验证扣款成功**。不要把本地回归通过视为真实付款成功。测试记录保存在被 Git 忽略的 `artifacts/e2e-*` 中。

2026-10-19 16:26 再次真实测试，订单 `D261019001217`（15 元）已执行安全域内的最终密码确认。支付宝随后明确返回“本次交易存在风险，为保护你的资金安全，支付宝中断了本次操作。”，场馆复核为已取消、未支付。最初因账单未核实，付款结果保留 `payment_submitted_unknown`；详见 `artifacts/e2e-20261019-162344/final-report.json`。随后用户在支付宝核对到“提交后退款”，该笔记为用户确认退款、预约未成功保留。日志显示最终 `/business/api/paycommit.json` 请求在订单确认后约 40 秒发出；页面显示风险中断，具体退款原因未核实，不能据场馆“未支付”推断支付宝没有产生交易。

### 下单点选验证码

可显式启用 [MgArcher/Text_select_captcha](https://github.com/MgArcher/Text_select_captcha) 的本地 ONNX 模型。模型在下单前加载；验证码图片和提示文字只在本机处理，不发送到外部识别服务。适配当前 AJ-Captcha 的文字提示，检测图片中的文字区域，再按提示顺序匹配并点击。识别分数不足时刷新图片，验证失败时使用网站自动刷新的图片，最多尝试三次；始终不重新点击下单按钮。

```bash
git clone https://github.com/MgArcher/Text_select_captcha.git artifacts/Text_select_captcha
python -m pip install -r requirements-captcha.txt
python cgyy.py --config configs/local.yaml --submit --auto-pay --headless \
  --captcha-repo artifacts/Text_select_captcha --ignore-password-warning
```

仓库需要包含 `model/best_v3.onnx` 和 `model/pre_model_v7.onnx`。Linux 默认使用 Droid Sans Fallback 中文字体，其他系统用 `--captcha-font /path/to/chinese.ttf` 指定。此选项只处理场馆下单验证码，不处理统一认证或支付宝的二次认证。

付款状态保存至 `artifacts/payment-result.json`，可用 `--payment-result` 指定其他位置。`paid` 表示服务器已确认，`payment_preview_ready` 表示仅预览；`payment_submitted_unknown` 表示表单已尝试提交但结果不明，此时禁止直接重跑，须先核对支付宝账单及预约记录。相同结果文件使用互斥锁避免并发付款；异常退出遗留 `.lock` 时，也应先核对记录再处理。状态文件及锁不包含账号密码。尚未确认的付款不会自动重试。

2026-10-19 登录后只读核对，系统配置 `reservationPayOuttime`（预约未支付超时时间，单位分钟）为 `1`，即当前场馆付款窗口为 1 分钟。校园付/支付宝另有约 15 分钟的交易倒计时，不能据此认定场地仍被锁定。配置可能调整，提交后务必按网页实际倒计时和场馆订单状态付款，不能沿用旧说明中固定的“10 分钟”。提交响应超时不会自动重试，以便先到个人中心核对是否已创建订单。

## 排查和验证

- `--browser-binary /path/to/chrome` 或环境变量 `CGYY_BROWSER_BINARY` 可指定现有 Chrome / Chromium。
- 浏览器读取 `HTTPS_PROXY` / `HTTP_PROXY`，也可以通过 `--proxy http://127.0.0.1:7890` 指定代理。
- 登录失败不会持续重试密码。返回码：`0` 查询成功或订单已提交，`1` 配置/业务限制，`2` 浏览器操作失败，`3` 所选时段没有空闲场地，`130` 用户退出。
- 定时等待使用北京时间和休眠，不再循环占满 CPU；配置使用 `yaml.safe_load`；导入模块不会启动浏览器。
- `captcha2str.py` 和 `captcha_collections/` 是 2022 年旧支付宝验证码方案的历史文件，新版不调用，也不需要安装其依赖。

```bash
python -m unittest discover -s tests -v
```

60 项回归测试使用本地页面和模拟接口，不访问真实账户或创建真实订单，付款测试中的请求也全部拦截在本地。覆盖金额/场馆/日期/场地/时段不一致、过期、收款方或表单目标异常、付款结果不明、并发及重复提交，以及快速查场、网站余票刷新、开放前等待、跨年每日日期滚动和登录态保存/失效等情形。需要先安装 Chromium；自定义浏览器时可设置 `CGYY_BROWSER_BINARY`。

2026-10-19 上午复核发现，旧优先级 `[1, ..., 12]` 会漏掉副馆 17–24 号候选，现已修复。同时新增逐场地查询日志 `queries.jsonl`、提交前网页读取失败的一次重试，以及网页内部导航和开放前预加载。当天早上的运行快照与日志保留原样，方便追查；修正不能证明当时副馆是否曾有空位。

## Linux 每日后台预约

默认规则：每天北京时间06:05复用登录态，开放前每5分钟只读核验一次，失效才重新登录；06:55准备预约页面，06:59:45前再核验，07:00开始查场和提交。仅选择学院路主馆或副馆的任一场地，同一场地连续20:00–22:00，排除沙河。预约日期沿用 `days_ahead: 2`，每天重新计算，例如10月20日运行时预约10月22日，不固定到某一天。

每日模式自动启用快速下单预算；一轮没有空位时每隔30秒再查，最晚08:00结束。一旦尝试提交，结果不明时停止核对，不换场馆重复下单；确认一笔订单后停止当天任务。订单、付款入口和截图按运行日期保存在 `artifacts/daily/YYYY-MM-DD/`，避免前一天的完成状态阻塞下一天。

```bash
# 只核对下一次每日计划，不登录、不下单
python scheduled_booking.py --daily --validate

# 当前运行环境已配置 Chromium、验证码模型及 Xvfb / xauth 时使用
deploy/run_daily_booking.sh
```

`deploy/run_daily_booking.sh` 读取本机 `.env`，使用有界面的 Chromium，通过 Xvfb 在服务器运行；可选的本机共享库等设置放在 `artifacts/runtime/environment.sh`。默认由本人在场馆倒计时内付款；脚本不会主动向本聊天发送通知。自动付款仍须显式添加 `--auto-pay` 和单笔金额上限。

部署每天06:05启动的 systemd 用户定时器：

```bash
mkdir -p ~/.config/systemd/user
python - <<'PY'
from pathlib import Path
project = Path.cwd()
units = Path.home() / '.config/systemd/user'
(units / 'cgyy-daily.service').write_text(
    (project / 'deploy/cgyy-daily.service.in').read_text().replace('@PROJECT_ROOT@', str(project)))
(units / 'cgyy-daily.timer').write_text((project / 'deploy/cgyy-daily.timer').read_text())
PY
systemctl --user daemon-reload
systemctl --user enable --now cgyy-daily.timer
systemctl --user list-timers cgyy-daily.timer
journalctl --user -u cgyy-daily.service -f

# 停止每日任务
systemctl --user disable --now cgyy-daily.timer
systemctl --user stop cgyy-daily.service
```

机器和网络需在任务期间运行。停止正在提交的程序可能留下已生成的订单，需先到网站核对，再决定是否重新运行。旧的一次性配置仍支持显式的 `start_at`、`release_at`、`stop_at` 和 `--state`；旧快照不会自动切换为每日计划。

## License

[MIT](LICENSE)
