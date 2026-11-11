import io
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from src.models import MiniMax, ModelError


def http_error(status, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return HTTPError("https://api.minimax.cn/anthropic/v1/messages", status, "", headers, None)


class MiniMaxTest(unittest.TestCase):
    def setUp(self):
        with patch.dict("os.environ", {"MINIMAX_API_KEY": "fake-test-key"}, clear=True):
            self.model = MiniMax()

    def test_rate_limit_retries_same_model_request(self):
        turn = {"content": [{"type": "text", "text": "ok"}]}
        with patch("src.models.sleep") as sleep, patch.object(
            self.model.opener, "open",
            side_effect=[http_error(429, "3"), io.BytesIO(json.dumps(turn).encode())],
        ) as send:
            self.assertEqual(self.model.next_turn("test", [], []), turn)
        sleep.assert_called_once_with(3)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[0].data, send.call_args_list[1].args[0].data)

    def test_rate_limit_retries_are_bounded(self):
        with patch("src.models.sleep") as sleep, patch.object(
            self.model.opener, "open", side_effect=[http_error(429) for _ in range(3)],
        ) as send:
            with self.assertRaisesRegex(ModelError, "minimax_http_429"):
                self.model.next_turn("test", [], [])
        self.assertEqual(send.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [15, 45])

    def test_long_retry_after_and_invalid_credentials_are_not_retried(self):
        for error in (http_error(429, "120"), http_error(401)):
            with self.subTest(status=error.code), patch("src.models.sleep") as sleep, patch.object(
                self.model.opener, "open", side_effect=error,
            ) as send:
                with self.assertRaisesRegex(ModelError, f"minimax_http_{error.code}"):
                    self.model.next_turn("test", [], [])
                self.assertEqual(send.call_count, 1)
                sleep.assert_not_called()
