import json
import unittest
from unittest.mock import Mock

import requests

from linkforge.jio.hunt import ACTIVATE_URL, AUTH_URL, GOOGLE_URL, NAVIGATE_URL, hunt_link


LINK = "https://serviceactivation.google.com/subscription/new/sample_123=="


def response(data=None, status=200, text=None):
    result = requests.Response()
    result.status_code = status
    result.encoding = "utf-8"
    result._content = (json.dumps(data or {}) if text is None else text).encode("utf-8")
    return result


def session_for(last, activation=None):
    session = Mock(spec=requests.Session)
    session.get.side_effect = [
        response({"loginFlag": "true"}),
        response(),
        response(activation or {"errorCode": "200"}),
        last,
    ]
    return session


class HuntParsingTests(unittest.TestCase):
    def test_standard_response_and_exact_chain(self):
        session = session_for(response({"redirectionURL": LINK}))
        self.assertEqual(hunt_link(session), (LINK, "ok"))
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [
            AUTH_URL, NAVIGATE_URL, ACTIVATE_URL, GOOGLE_URL,
        ])

    def test_nested_renamed_response(self):
        session = session_for(response({"data": [{"renamed": {"url": LINK}}]}))
        self.assertEqual(hunt_link(session), (LINK, "ok"))

    def test_array_response_falls_back_to_body(self):
        session = session_for(response([{"url": LINK}]))
        self.assertEqual(hunt_link(session), (LINK, "ok"))

    def test_escaped_body_response(self):
        text = json.dumps({"url": LINK}).replace("/", r"\/")
        self.assertEqual(hunt_link(session_for(response(text=text))), (LINK, "ok"))

    def test_direct_redirection_url_has_priority(self):
        other = LINK.replace("sample_123", "sample_456")
        session = session_for(response({"data": {"url": other}, "redirectionURL": LINK}))
        self.assertEqual(hunt_link(session), (LINK, "ok"))

    def test_rejects_spoofed_direct_url_but_finds_nested_real_link(self):
        session = session_for(response({
            "redirectionURL": "https://evil.example/?partnerPromotionToken=sample",
            "data": {"url": LINK},
        }))
        self.assertEqual(hunt_link(session), (LINK, "ok"))

    def test_spoofed_url_alone_is_not_a_success(self):
        session = session_for(response({"redirectionURL": "https://evil.example/?partnerPromotionToken=sample"}))
        self.assertEqual(hunt_link(session), (None, "no_url"))

    def test_already_registered_offer_can_still_return_link(self):
        session = session_for(response({"redirectionURL": LINK}), {"errorCode": "409", "message": "already availed"})
        self.assertEqual(hunt_link(session), (LINK, "ok"))

    def test_already_registered_without_link(self):
        session = session_for(response(), {"errorCode": "409", "message": "already availed"})
        self.assertEqual(hunt_link(session), (None, "already_active"))

    def test_fresh_only_refuses_recycled_number(self):
        # A first hunt landing on "already availed" means a previous renter
        # activated this number; its token redeems to "already activated".
        session = session_for(response({"redirectionURL": LINK}), {"errorCode": "409", "message": "already availed"})
        self.assertEqual(hunt_link(session, fresh_only=True), (None, "recycled_number"))

    def test_fresh_only_accepts_genuine_fresh_activation(self):
        session = session_for(response({"redirectionURL": LINK}), {"errorCode": "200"})
        self.assertEqual(hunt_link(session, fresh_only=True), (LINK, "ok"))

    def test_relink_path_reissues_from_own_already_availed_session(self):
        # fresh_only=False (relink default): "already availed" is OUR own prior
        # activation, so the re-issued token is still valid and must come back.
        session = session_for(response({"redirectionURL": LINK}), {"errorCode": "409", "message": "already availed"})
        self.assertEqual(hunt_link(session), (LINK, "ok"))

    def test_google_api_outage_or_throttle_is_retryable(self):
        for status in (401, 403, 429, 500, 502, 503):
            with self.subTest(status=status):
                session = session_for(response({"redirectionURL": LINK}, status=status))
                self.assertEqual(hunt_link(session), (None, "network"))

    def test_explicit_dead_session_stops_chain(self):
        session = Mock(spec=requests.Session)
        session.get.return_value = response({"loginFlag": None})
        self.assertEqual(hunt_link(session), (None, "session_invalid"))
        self.assertEqual(session.get.call_count, 1)

    def test_non_json_auth_page_is_not_a_dead_session(self):
        session = Mock(spec=requests.Session)
        session.get.return_value = response(text="<html>Temporarily unavailable</html>")
        self.assertEqual(hunt_link(session), (None, "network"))


if __name__ == "__main__":
    unittest.main()
