"""验证失败后的换图与停止条件；不连接真实验证码服务或加载模型。"""
import os
import unittest

from playwright.sync_api import sync_playwright

from text_captcha import CaptchaError, TextCaptchaSolver


HTML = '''<div class="verifybox"><div class="verify-img-panel">
<img style="width:330px;height:155px"></div><div>请依次点击【受,从,菜】</div>
<button class="verify-refresh">刷新</button></div>
<script>
window.clicks=0;window.rounds=0;window.succeedOn=2;
const image=document.querySelector('img');
function refresh(){rounds++;image.src='data:image/svg+xml;base64,'+btoa(
 '<svg xmlns="http://www.w3.org/2000/svg" width="330" height="155">'
 +'<text x="10" y="30">'+rounds+'</text></svg>');}
image.onclick=()=>{if(++clicks%3===0){if(rounds===succeedOn)
 document.querySelector('.verifybox').style.display='none';else refresh();}};
document.querySelector('button').onclick=refresh;
refresh();
</script>'''


class CaptchaBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        options = {'headless': True}
        if os.getenv('CGYY_BROWSER_BINARY'):
            options['executable_path'] = os.environ['CGYY_BROWSER_BINARY']
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content(HTML)
        self.solver = TextCaptchaSolver.__new__(TextCaptchaSolver)
        self.solver.recognize = lambda png, hint: ([(10, 10), (20, 20), (30, 30)], .95)

    def tearDown(self):
        self.page.close()

    def test_uses_refreshed_image_after_rejection(self):
        self.solver(self.page, timeout=5000)
        self.assertEqual(self.page.evaluate('clicks'), 6)
        self.assertFalse(self.page.locator('.verifybox').is_visible())

    def test_stops_after_three_rejected_challenges(self):
        self.page.evaluate('succeedOn=99')
        with self.assertRaisesRegex(CaptchaError, '三次'):
            self.solver(self.page, timeout=5000)
        self.assertEqual(self.page.evaluate('clicks'), 9)
        self.assertTrue(self.page.locator('.verifybox').is_visible())
