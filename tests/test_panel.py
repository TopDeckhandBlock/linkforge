"""Panel contract: every button resolves, every view renders."""

from types import SimpleNamespace

from linkforge.panel import Panel


class StubManager:
    def __init__(self):
        self.running = False
        self.attempts = 3
        self.found = []
        self.out = "links/REDEEM.txt"
        self.started_with = None
        self.stopped = False
        self.cfg = SimpleNamespace(
            service="Jio50",
            provider="pvacodes",
            max_price=1.0,
            otp_timeout=180,
            concurrency=2,
            proxy_enabled=False,
            tg_chat_id="7448683285",
            db_path="results.db",
        )

    def money(self):
        return "$1.00"

    def status_text(self):
        return "status body"

    def last_links(self, limit=5):
        return "link body"

    def start(self, minutes=240, target=None):
        self.started_with = minutes
        self.running = True
        return "Запуск: %d мин." % minutes

    def stop(self):
        self.stopped = True
        self.running = False
        return "Останавливаю."

    def hunt_async(self):
        return "Охота запущена."

    def set_proxy(self, enabled):
        self.cfg.proxy_enabled = enabled
        return "Прокси включены." if enabled else "Прокси выключены."

    def logs_tail(self, limit=18):
        return "12:00:00 attempt 1"


def _buttons(panel):
    for _name, view in panel.actions().items():
        _text, keyboard = view("")
        for row in keyboard:
            for button in row:
                yield button


def test_every_button_resolves_to_an_action():
    panel = Panel(StubManager())
    actions = panel.actions()
    seen = 0
    for button in _buttons(panel):
        action, _, arg = button["callback_data"].partition(":")
        assert action in actions, "dead button: %s" % button["callback_data"]
        text, keyboard = actions[action](arg)
        assert isinstance(text, str) and text
        assert isinstance(keyboard, list)
        seen += 1
    assert seen >= 8


def test_every_command_answers():
    panel = Panel(StubManager())
    for name, handler in panel.commands().items():
        reply = handler()
        text = reply[0] if isinstance(reply, tuple) else reply
        assert isinstance(text, str) and text, "empty answer for /%s" % name


def test_start_button_passes_minutes():
    manager = StubManager()
    panel = Panel(manager)
    panel.actions()["run"]("60")
    assert manager.started_with == 60


def test_proxy_toggle_flips_config():
    manager = StubManager()
    panel = Panel(manager)
    panel.actions()["px"]("1")
    assert manager.cfg.proxy_enabled is True
    panel.actions()["px"]("0")
    assert manager.cfg.proxy_enabled is False
