"""End-to-end orchestration test: rent -> OTP -> login -> mine -> store -> deliver.

Jio endpoints and the SMS provider are replaced by fakes, so the test proves the
wiring of the whole chain without depending on the vendor's mood or a paid
number.
"""

from pathlib import Path

import pytest

from linkforge import hunter, proxy, runner
from linkforge.config import Config
from linkforge.runner import Farm

LINK = "https://serviceactivation.google.com/subscription/new/FAKE_TOKEN_123"
PHONE = "9999999999"  # normalize_phone strips the 91 country prefix


class FakeProvider:
    def __init__(self, phone="919999999999", balance=1.5):
        self.phone = phone
        self._balance = balance
        self.rented: list[str] = []
        self.completed: list[str] = []
        self.cancelled: list[str] = []

    def balance(self):
        return self._balance

    def rent(self, max_price=None):
        act = "act-%d" % (len(self.rented) + 1)
        self.rented.append(act)
        return act, self.phone

    def status(self, act_id):
        return ("OK", None)

    def ready(self, act_id):
        pass

    def complete(self, act_id):
        self.completed.append(act_id)

    def cancel(self, act_id):
        self.cancelled.append(act_id)


@pytest.fixture
def farm(tmp_path, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(hunter, "check_subscriber", lambda phone, proxies=None: "JIO")
    monkeypatch.setattr(hunter, "jio_send_otp_detail", lambda *a, **k: (hunter.OTP_OK, ""))
    monkeypatch.setattr(hunter, "poll_otp", lambda *a, **k: "123456")

    def fake_validate(session, _phone, _otp):
        # The real call writes the auth state into the jar as Set-Cookie;
        # an empty jar would be (correctly) dropped by SessionStore.save.
        session.cookies.set("jio-auth-token", "fake", domain="www.jio.com")
        return True

    monkeypatch.setattr(hunter, "jio_validate_otp", fake_validate)
    monkeypatch.setattr(hunter, "hunt_link", lambda *a, **k: (LINK, "OK"))
    monkeypatch.setattr(proxy, "for_requests", lambda: {})
    monkeypatch.setattr(runner, "install_doh_shield", lambda: None)
    monkeypatch.setattr(runner, "create_provider", lambda cfg: provider)
    cfg = Config(
        db_path=str(tmp_path / "test.db"),
        links_dir=str(tmp_path / "links"),
        run_file=str(tmp_path / "links" / "run-001.txt"),
    )
    return Farm(cfg, log=lambda *a: None, telegram=False), provider


def test_full_chain_mines_stores_and_delivers(farm):
    target, provider = farm
    link = target.hunt()
    assert link == LINK
    assert target.deliver(link) is True
    assert provider.rented, "no rent happened"
    assert provider.completed == provider.rented, "rent was never settled"
    assert Path(target.out).read_text(encoding="utf-8").strip() == LINK
    assert target.store.all_links() == [LINK]
    assert target.sessions.get_link(PHONE) == LINK
    assert target.deliver(LINK) is False, "duplicate link delivered twice"


def test_not_jio_fails_fast_and_keeps_no_link(farm, monkeypatch):
    target, provider = farm
    monkeypatch.setattr(hunter, "check_subscriber", lambda phone, proxies=None: "NOT_JIO")
    assert target.hunt() is None
    assert target.store.all_links() == []
    assert provider.rented, "no rent happened"
    assert provider.completed == [], "number settled despite NOT_JIO"


def test_otp_rejected_schedules_refund(farm, monkeypatch):
    target, provider = farm
    monkeypatch.setattr(hunter, "jio_validate_otp", lambda *a, **k: False)
    assert target.hunt() is None
    assert target.store.all_links() == []
    assert provider.completed == []
    assert target.sessions.get_cookies(PHONE) is None, "failed login must not persist a session"
