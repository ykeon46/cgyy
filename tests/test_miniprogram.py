from pathlib import Path
import shutil
import subprocess
import unittest


class MiniprogramTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), '小程序页面回归测试需要 Node.js')
    def test_page_regressions(self):
        result = subprocess.run(
            ['node', '--test', str(Path(__file__).with_suffix('.js'))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
