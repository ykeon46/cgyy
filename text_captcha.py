"""AJ-Captcha adapter for MgArcher/Text_select_captcha (optional local dependency)."""
from __future__ import annotations

from itertools import permutations
import logging
from pathlib import Path
import re
import sys
import time

LOG = logging.getLogger('cgyy')


class CaptchaError(ValueError):
    pass


class TextCaptchaSolver:
    def __init__(self, repository: Path, font: Path):
        repository = repository.resolve()
        if not (repository / 'src/captcha.py').is_file() or not font.is_file():
            raise CaptchaError('请指定 Text_select_captcha 仓库和中文字体文件。')
        if any(not (repository / 'model' / name).is_file()
               for name in ('best_v3.onnx', 'pre_model_v7.onnx')):
            raise CaptchaError('Text_select_captcha 仓库缺少所需的两个 ONNX 模型。')
        sys.path.insert(0, str(repository))
        try:
            from src.captcha import TextSelectCaptcha
            from PIL import ImageFont
            self.model = TextSelectCaptcha()
            self.font = ImageFont.truetype(str(font), 56)
        except (ImportError, OSError, RuntimeError) as error:
            raise CaptchaError('验证码模型加载失败，请检查可选依赖、ONNX 模型及字体。') from error

    def recognize(self, png: bytes, hint: str):
        import cv2
        import numpy as np
        from PIL import Image, ImageDraw

        match = re.search(r'请依次点击\s*[【\[]([^】\]]+)[】\]]', hint)
        if not match:
            raise CaptchaError('无法读取点选验证码提示。')
        characters = [c for c in match[1] if not c.isspace() and c not in ',，、']
        if not 1 <= len(characters) <= 5:
            raise CaptchaError('点选验证码提示长度异常。')
        image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise CaptchaError('无法读取验证码图片。')
        boxes = [d[:4] for d in self.model.yolo.inference(image) if d[5] == 0]
        if not len(characters) <= len(boxes) <= 10:
            raise CaptchaError('验证码中文字区域检测不完整。')
        targets = [image[y1:y2, x1:x2] for x1, y1, x2, y2 in boxes]
        references = []
        for character in characters:
            canvas = Image.new('RGB', (64, 64), 'white')
            ImageDraw.Draw(canvas).text((4, -3), character, font=self.font, fill='black')
            references.append(cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2BGR))
        scores = self.model.pre.reason_all_batch(references, targets)
        assignment = max(permutations(range(len(boxes)), len(characters)),
                         key=lambda indices: sum(scores[i][j] for i, j in enumerate(indices)))
        confidence = min(scores[i][j] for i, j in enumerate(assignment))
        if confidence < .70:
            raise CaptchaError('验证码文字匹配置信度不足，停止点击。')
        points = [((boxes[j][0] + boxes[j][2]) / 2, (boxes[j][1] + boxes[j][3]) / 2)
                  for j in assignment]
        return points, confidence

    def __call__(self, page, *, timeout=20000):
        deadline = time.monotonic() + timeout / 1000
        box = page.locator('.verifybox:visible')
        image = box.locator('.verify-img-panel img')
        def remaining(maximum=15000):
            ms = int((deadline - time.monotonic()) * 1000)
            if ms <= 0:
                raise CaptchaError('验证码处理超时。')
            return min(ms, maximum)

        for attempt in range(3):
            image.wait_for(timeout=remaining())
            page.wait_for_function('''() => {
                const b = [...document.querySelectorAll('.verifybox')].find(b => b.offsetParent);
                const i = b?.querySelector('.verify-img-panel img');
                return i && i.complete && i.naturalWidth > 0;
            }''', timeout=remaining())
            source = image.get_attribute('src')
            hint = box.inner_text()
            try:
                points, confidence = self.recognize(image.screenshot(), hint)
            except CaptchaError:
                if attempt == 2:
                    raise
                LOG.info('验证码识别不足，刷新验证图片。')
                box.locator('.verify-refresh').click()
            else:
                LOG.info('验证码识别：%s；最低匹配分 %.3f', hint.splitlines()[-1], confidence)
                for x, y in points:
                    remaining()
                    image.click(position={'x': x, 'y': y})
                    page.wait_for_timeout(150)
            result = page.wait_for_function('''previous => {
                const b = [...document.querySelectorAll('.verifybox')].find(b => b.offsetParent);
                if (!b) return 'closed';
                const i = b.querySelector('.verify-img-panel img');
                return i && i.complete && i.naturalWidth > 0 && i.getAttribute('src') !== previous
                    ? 'refreshed' : false;
            }''', arg=source, timeout=remaining()).json_value()
            if result == 'closed':
                LOG.info('点选验证码已通过。')
                return
            if attempt < 2:
                LOG.info('验证码未通过，使用网站刷新后的图片（第 %s 次）。', attempt + 2)
        raise CaptchaError('验证码连续三次未通过，未重复提交订单。')
