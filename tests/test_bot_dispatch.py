"""Bot dispatch contract: chat allowlist, command routing, callback routing."""

from linkforge.bot import Bot


class Recorder(Bot):
    def __init__(self, chat_id="42"):
        super().__init__("token", chat_id)
        self.calls: list[tuple[str, dict]] = []

    def _call(self, method, http_timeout=30, **payload):
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 7}}

    def methods(self):
        return [name for name, _payload in self.calls]


def _commands():
    return {"menu": lambda: ("menu text", [[{"text": "x", "callback_data": "st"}]])}


def _actions():
    return {"st": lambda arg: ("status text for %r" % arg, None)}


def test_command_from_owner_gets_answer_with_keyboard():
    bot = Recorder()
    bot._update({"message": {"chat": {"id": 42}, "text": "/menu"}}, _commands(), _actions())
    method, payload = bot.calls[-1]
    assert method == "sendMessage"
    assert payload["chat_id"] == "42"
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "st"


def test_foreign_chat_is_ignored():
    bot = Recorder()
    bot._update({"message": {"chat": {"id": 999}, "text": "/menu"}}, _commands(), _actions())
    bot._update(
        {"callback_query": {"id": "q", "message": {"chat": {"id": 999}, "message_id": 1}, "data": "st"}},
        _commands(),
        _actions(),
    )
    assert bot.calls == []


def test_callback_edits_the_card_and_passes_argument():
    bot = Recorder()
    bot._update(
        {"callback_query": {"id": "q1", "message": {"chat": {"id": 42}, "message_id": 11}, "data": "st:60"}},
        _commands(),
        _actions(),
    )
    assert bot.methods()[0] == "answerCallbackQuery"
    method, payload = bot.calls[-1]
    assert method == "editMessageText"
    assert payload["message_id"] == 11
    assert payload["text"] == "status text for '60'"


def test_unknown_command_points_to_menu():
    bot = Recorder()
    bot._update({"message": {"chat": {"id": 42}, "text": "/nonsense"}}, _commands(), _actions())
    method, payload = bot.calls[-1]
    assert method == "sendMessage"
    assert "/menu" in payload["text"]
