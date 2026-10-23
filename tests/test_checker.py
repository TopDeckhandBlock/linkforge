import unittest
from unittest.mock import patch

import requests

from linkforge.checker import (
    DEAD, SUSPECT, UNKNOWN, LinkProbe, check_link, judge,
    link_age_seconds, probe_link,
)


LINK = "https://serviceactivation.google.com/subscription/new/sample_123=="


def response(status=200, url=LINK, body="", location=None):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body.encode("utf-8")
    result.encoding = "utf-8"
    if location is not None:
        result.headers["Location"] = location
    return result


class ProbeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("linkforge.checker.requests.Session")
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.session = self.factory.return_value.__enter__.return_value

    def test_signin_stops_without_following_or_claiming_alive(self):
        self.session.get.return_value = response(302, location="https://accounts.google.com/ServiceLogin")
        probe = probe_link(LINK)
        self.assertTrue(probe.redirected_to_signin)
        self.assertEqual(probe.final_host, "accounts.google.com")
        self.assertEqual(self.session.get.call_count, 1)
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(judge(probe, 60), UNKNOWN)
        self.assertEqual(judge(probe, 1800), SUSPECT)

    def test_generic_success_page_is_not_alive_or_dead(self):
        self.session.get.return_value = response(body="Welcome! Get started. Claim offer.")
        self.assertEqual(judge(probe_link(LINK)), UNKNOWN)

    def test_visible_expired_and_used_messages(self):
        for body in (
            "<p>This offer has expired</p>",
            "<p>This subscription <b>link</b> has already been used</p>",
            "<p>This activation link is invalid</p>",
        ):
            with self.subTest(body=body):
                self.session.get.return_value = response(body=body)
                probe = probe_link(LINK)
                self.assertEqual(judge(probe), DEAD)
                self.assertTrue(probe.definitive_error)

    def test_unused_script_or_template_messages_are_not_token_errors(self):
        self.session.get.return_value = response(body=(
            '<script>const error = "this offer has expired";</script>'
            '<template><p>This link has already been used</p></template>'
            '<style>/* this activation link is invalid */</style><p>Sign in</p>'
        ))
        self.assertEqual(judge(probe_link(LINK)), UNKNOWN)

    def test_account_ineligibility_is_not_proof_of_dead_token(self):
        self.session.get.return_value = response(body="Not eligible for this offer")
        self.assertEqual(judge(probe_link(LINK)), UNKNOWN)

    def test_transient_http_failures_remain_unknown_even_if_old(self):
        for status in (400, 401, 403, 422, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.session.get.return_value = response(status, body="This offer has expired")
                self.assertEqual(judge(probe_link(LINK), 7200), UNKNOWN)

    def test_not_found_and_gone_are_dead(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.session.get.return_value = response(status)
                self.assertEqual(judge(probe_link(LINK)), DEAD)

    def test_rejects_unsupported_input_before_network_access(self):
        for url in ("http://127.0.0.1/private", "https://evil.example/?partnerPromotionToken=sample"):
            with self.subTest(url=url):
                probe = probe_link(url)
                self.assertEqual(judge(probe), DEAD)
        self.factory.assert_not_called()

    def test_foreign_or_downgraded_redirect_is_not_followed(self):
        for location in (
            "https://evil.example/private",
            "http://one.google.com/offer",
            "https://accounts.google.com.evil.example/signin",
            "https://one.google.com@127.0.0.1/private",
            "https://one.google.com:444/offer",
            "http://127.0.0.1/private",
        ):
            with self.subTest(location=location):
                self.session.get.reset_mock()
                self.session.get.return_value = response(302, location=location)
                probe = probe_link(LINK)
                self.assertEqual(judge(probe), UNKNOWN)
                self.assertEqual(self.session.get.call_count, 1)
                self.assertIn("Blocked redirect", probe.error)

    def test_follows_allowed_redirect_then_stops_at_signin(self):
        intermediate = "https://one.google.com/activate-plan?partnerPromotionToken=sample_123=="
        self.session.get.side_effect = [
            response(302, location=intermediate),
            response(302, url=intermediate, location="https://accounts.google.com/ServiceLogin"),
        ]
        probe = probe_link(LINK)
        self.assertTrue(probe.redirected_to_signin)
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(judge(probe), UNKNOWN)

    def test_relative_redirect_and_explicit_error_page(self):
        self.session.get.side_effect = [
            response(302, location="/error"),
            response(url="https://serviceactivation.google.com/error", body="This link has already been used"),
        ]
        self.assertEqual(judge(probe_link(LINK)), DEAD)
        self.assertEqual(self.session.get.call_args.args[0], "https://serviceactivation.google.com/error")

    def test_explicit_token_error_redirect(self):
        self.session.get.return_value = response(302, location="https://one.google.com/error?error=already_redeemed")
        probe = probe_link(LINK)
        self.assertEqual(judge(probe), DEAD)
        self.assertEqual(probe.definitive_error, "already_redeemed")

    def test_missing_location_is_unknown(self):
        self.session.get.return_value = response(302)
        self.assertEqual(judge(probe_link(LINK)), UNKNOWN)

    def test_malformed_redirect_is_unknown_not_an_exception(self):
        self.session.get.return_value = response(302, location="https://[invalid/path")
        probe = probe_link(LINK)
        self.assertEqual(judge(probe), UNKNOWN)
        self.assertIn("Malformed", probe.error)
        self.assertEqual(self.session.get.call_count, 1)

    def test_redirect_loop_is_bounded(self):
        self.session.get.return_value = response(302, location=LINK)
        probe = probe_link(LINK)
        self.assertEqual(self.session.get.call_count, 6)
        self.assertEqual(judge(probe), UNKNOWN)
        self.assertIn("Too many", probe.error)

    def test_timeout_is_unknown_and_does_not_leak_token(self):
        self.session.get.side_effect = requests.Timeout("secret URL " + LINK)
        probe = probe_link(LINK)
        self.assertEqual(judge(probe), UNKNOWN)
        self.assertIn("Timeout", probe.error)
        self.assertNotIn("sample_123", probe.error)

    def test_tls_verification_is_not_disabled(self):
        self.session.get.return_value = response()
        probe_link(LINK)
        self.assertNotEqual(self.session.get.call_args.kwargs.get("verify"), False)

    def test_check_link_combines_age_with_probe(self):
        self.session.get.return_value = response(302, location="https://accounts.google.com/ServiceLogin")
        with patch("linkforge.checker.time.time", return_value=3600):
            verdict, probe, age = check_link(LINK, "1970-01-01 00:00:00", max_age=1800)
        self.assertEqual(verdict, SUSPECT)
        self.assertEqual(age, 3600)
        self.assertTrue(probe.redirected_to_signin)


class AgeTests(unittest.TestCase):
    def test_epoch_zero_is_not_replaced_by_current_time(self):
        self.assertEqual(link_age_seconds("1970-01-01 00:00:00", now=0), 0)

    def test_timestamps_are_utc(self):
        self.assertEqual(link_age_seconds("1970-01-01 00:00:00", now=3600), 3600)

    def test_missing_and_invalid_timestamps(self):
        self.assertIsNone(link_age_seconds(None))
        self.assertIsNone(link_age_seconds("not a timestamp"))

    def test_future_timestamp_does_not_make_negative_age(self):
        self.assertEqual(link_age_seconds("1970-01-02 00:00:00", now=0), 0)

    def test_network_failure_cannot_prove_expiration(self):
        self.assertEqual(judge(LinkProbe(), age_seconds=7200), UNKNOWN)


if __name__ == "__main__":
    unittest.main()
