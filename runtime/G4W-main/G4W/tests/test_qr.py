import hashlib
import unittest

from G4W.wechat.qr import make_matrix, make_svg


class QrTests(unittest.TestCase):
    def test_weixin_login_url_generates_scannable_shape(self):
        value = "https://liteapp.weixin.qq.com/q/example?qrcode=1234567890abcdef1234567890abcdef&bot_type=3"
        matrix = make_matrix(value)
        self.assertEqual(len(matrix), len(matrix[0]))
        self.assertGreaterEqual(len(matrix), 37)
        self.assertTrue(matrix[0][0])
        self.assertIn("<svg", make_svg(value))
        flattened = "".join("1" if cell else "0" for row in matrix for cell in row)
        self.assertEqual(
            hashlib.sha256(flattened.encode()).hexdigest(),
            "8a3e88816c7f2515dec4ec60487bf093db6c4e1f299a6f88fe33c424ae11b53e",
        )


if __name__ == "__main__":
    unittest.main()
