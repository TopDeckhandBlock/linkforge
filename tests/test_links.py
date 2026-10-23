import json
import unittest
from urllib.parse import quote

from linkforge.jio.hunt import find_google_links
from linkforge.jio.relink import extract_token, same_link
from linkforge.links import normalize_google_link


TOKEN = "sample_123-ABC+def/ghi=="
LINK = "https://serviceactivation.google.com/subscription/new/" + TOKEN


class LinkParsingTests(unittest.TestCase):
    def test_preserves_complete_path_token(self):
        self.assertEqual(extract_token(LINK), TOKEN)

    def test_decodes_percent_encoded_path_token(self):
        self.assertEqual(extract_token(LINK.rsplit("/new/", 1)[0] + "/new/" + quote(TOKEN, safe="")), TOKEN)

    def test_google_one_path_variant(self):
        url = "https://one.google.com/activate-plan/subscription/new/" + TOKEN
        self.assertEqual(extract_token(url), TOKEN)
        self.assertTrue(same_link(LINK, url))

    def test_query_token_preserves_literal_plus_and_padding(self):
        url = "https://one.google.com/activate-plan?partnerPromotionToken=" + TOKEN
        self.assertEqual(extract_token(url), TOKEN)

    def test_query_token_decodes_percent_encoding(self):
        url = "https://one.google.com/activate-plan?partnerPromotionToken=" + quote(TOKEN, safe="")
        self.assertEqual(extract_token(url), TOKEN)
        self.assertTrue(same_link(LINK, url))

    def test_serviceactivation_query_variant_is_preserved(self):
        for suffix in ("", "/"):
            url = "https://serviceactivation.google.com/subscription/new" + suffix
            url += "?partnerPromotionToken=" + quote(TOKEN, safe="")
            with self.subTest(url=url):
                self.assertEqual(extract_token(url), TOKEN)
                self.assertEqual(find_google_links(url), [url])
                self.assertTrue(same_link(LINK, url))

    def test_promo_variants(self):
        for url in (
            "https://one.google.com/promo/" + TOKEN,
            "https://one.google.com/promo?code=" + quote(TOKEN, safe=""),
        ):
            with self.subTest(url=url):
                self.assertEqual(extract_token(url), TOKEN)

    def test_escaped_json_url(self):
        text = json.dumps({"renamed": LINK}).replace("/", r"\/")
        self.assertEqual(find_google_links(text), [LINK])

    def test_unicode_escaped_url_separators(self):
        text = LINK.replace(":", r"\u003a").replace("/", r"\u002f").replace("=", r"\u003d")
        self.assertEqual(find_google_links(text), [LINK])

    def test_html_escaped_query(self):
        url = "https://one.google.com/activate-plan?source=jio&amp;partnerPromotionToken=" + quote(TOKEN, safe="")
        links = find_google_links('<a href="' + url + '">Redeem</a>')
        self.assertEqual(len(links), 1)
        self.assertEqual(extract_token(links[0]), TOKEN)
        self.assertNotIn("&amp;", links[0])

    def test_nested_payload_deduplicates_by_complete_token(self):
        second = "https://one.google.com/activate-plan?partnerPromotionToken=" + quote(TOKEN, safe="")
        payload = {"data": [None, {"changed_name": LINK}, [second, LINK]]}
        self.assertEqual(find_google_links(payload), [LINK])

    def test_traversal_preserves_order(self):
        other = LINK.replace("sample_123", "sample_456")
        self.assertEqual(find_google_links({"data": [LINK, {"url": other}]}), [LINK, other])

    def test_deep_or_cyclic_python_payload_does_not_recurse(self):
        payload: list[object] = [LINK]
        for _ in range(1200):
            payload = [payload]
        payload.append(payload)
        self.assertEqual(find_google_links(payload), [LINK])

    def test_rejects_marker_in_foreign_url(self):
        for url in (
            "https://evil.example/?partnerPromotionToken=sample",
            "https://evil.example/serviceactivation.google.com/subscription/new/sample",
            "https://serviceactivation.google.com.evil.example/subscription/new/sample",
            "https://serviceactivation.google.com@evil.example/subscription/new/sample",
            "https://evil.example/?next=" + LINK,
        ):
            with self.subTest(url=url):
                self.assertEqual(find_google_links(url), [])
                self.assertEqual(extract_token(url), "")

    def test_rejects_insecure_or_malformed_links(self):
        for url in (
            LINK.replace("https:", "http:"),
            LINK.replace(".com/", ".com:444/"),
            LINK.replace("https://", "https://user:pass@"),
            "https://serviceactivation.google.com:bad/subscription/new/sample",
            "https://[bad/subscription/new/sample",
            LINK + "%0a",
        ):
            with self.subTest(url=url):
                self.assertEqual(normalize_google_link(url), "")

    def test_ignores_assets_signin_and_generic_google_pages(self):
        urls = [
            "https://one.google.com/promo/banner.png",
            "https://one.google.com/",
            "https://accounts.google.com/ServiceLogin?continue=" + quote(LINK, safe=""),
            "https://myjiostatic.cdn.jio.com/assets/partnerPromotionToken.js",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(find_google_links(url), [])

    def test_trailing_prose_punctuation(self):
        self.assertEqual(find_google_links("Redeem (" + LINK + ")."), [LINK])

    def test_query_and_fragment_do_not_become_part_of_token(self):
        self.assertEqual(extract_token(LINK + "?source=jio#offer"), TOKEN)

    def test_empty_and_non_url_values(self):
        for value in (None, "", 123, {"message": "no offer"}):
            with self.subTest(value=value):
                self.assertEqual(find_google_links(value), [])
        self.assertEqual(extract_token(""), "")
        self.assertFalse(same_link(None, LINK))

    def test_distinct_complete_tokens_stay_distinct(self):
        self.assertFalse(same_link(LINK, LINK.replace("ghi==", "xyz==")))


if __name__ == "__main__":
    unittest.main()
