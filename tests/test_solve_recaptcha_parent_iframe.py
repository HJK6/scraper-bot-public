"""Regression: solve_recaptcha must thread parent_iframe_xpath so a reCAPTCHA
nested inside another iframe (e.g. a Fillout embed) is solvable. Without this the
server-side solver searches the top frame, finds nothing, and returns False."""
from unittest import mock

import client
import server


def test_client_solve_recaptcha_threads_parent_iframe_xpath():
    bot = client.ScraperBot(base_url="http://scraper-bot.test")
    captured = {}

    def fake_post(path, json_data=None, destructive=False):
        captured["path"] = path
        captured["body"] = json_data
        return {"solved": True}

    with mock.patch.object(bot, "_post", side_effect=fake_post):
        ok = bot.solve_recaptcha(
            "sid123", max_attempts=2,
            parent_iframe_xpath="//iframe[contains(@src,'fillout')]",
        )

    assert ok is True
    assert captured["path"].endswith("/sessions/sid123/solve_recaptcha")
    assert captured["body"]["parent_iframe_xpath"] == "//iframe[contains(@src,'fillout')]"
    assert captured["body"]["max_attempts"] == 2


def test_client_solve_recaptcha_omits_parent_iframe_when_none():
    bot = client.ScraperBot(base_url="http://scraper-bot.test")
    captured = {}

    def fake_post(path, json_data=None, destructive=False):
        captured["body"] = json_data
        return {"solved": False}

    with mock.patch.object(bot, "_post", side_effect=fake_post):
        bot.solve_recaptcha("sid", max_attempts=3)

    assert "parent_iframe_xpath" not in captured["body"]


def test_request_model_accepts_parent_iframe_xpath():
    req = server.SolveRecaptchaRequest(parent_iframe_xpath="//iframe[@title='x']")
    assert req.parent_iframe_xpath == "//iframe[@title='x']"
    assert server.SolveRecaptchaRequest().parent_iframe_xpath is None
