"""The project-side client library against a real core API on a real Unix socket."""

import asyncio
import re
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from conftest import core_app

from tg_mcp.config import ClientPolicy
from tg_mcp.events import EventBuffer

sys.path.insert(0, str(Path(__file__).parents[1] / "client"))
from tgcore_client import CoreError, TelegramCore  # noqa: E402

CALLS = ClientPolicy(
    name="calls",
    chats=frozenset({100}),
    events=frozenset({"new_message"}),
    text_pattern=re.compile("Пропущенный"),
)


class RunningCore:
    def __init__(self, config, client, policy, socket_path):
        self.buffer = EventBuffer(3600, 1000)
        app = core_app(config, client, policy, self.buffer)
        self.server = uvicorn.Server(uvicorn.Config(app, uds=str(socket_path), log_level="error", lifespan="off"))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_until_complete, args=(self.server.serve(),), daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "core did not start"
            time.sleep(0.02)
        return self

    def publish(self, chat_id, text, kind="new_message", message_id=1):
        """Deliver an update the way the core does: kept only if someone listens."""
        message = {"id": message_id, "date": datetime.now(UTC).isoformat(), "text": text}
        event = {"type": kind, "chat_id": chat_id, "chat_name": "x", "message": message}

        def deliver():
            if self.buffer.wanted(event):
                self.buffer.publish(event)

        self.loop.call_soon_threadsafe(deliver)

    def wait_for_listeners(self, count=1):
        deadline = time.monotonic() + 10
        while len(self.buffer._subscriptions) != count:
            assert time.monotonic() < deadline, f"expected {count} subscriptions"
            time.sleep(0.02)

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


def collect(core, count, **kwargs):
    received = []

    def consume():
        for item in core.events(**kwargs):
            received.append(item)
            if len(received) == count:
                break

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    return thread, received


@pytest.fixture
def socket_path(tmp_path):
    # Unix socket paths are limited to about 100 characters.
    path = Path(f"/tmp/tgcore-test-{time.monotonic_ns()}.sock")
    yield path
    path.unlink(missing_ok=True)


def test_stream_delivers_only_allowed_events_at_least_once(config, client, socket_path, tmp_path):
    cursor_file = tmp_path / "cursor"
    with RunningCore(config, client, CALLS, socket_path) as core:
        api = TelegramCore(socket_path)
        core.publish(100, "Пропущенный звонок 0")  # Nobody listens yet: not kept.
        thread, received = collect(api, 2, cursor_file=cursor_file)
        core.wait_for_listeners()
        core.publish(200, "Пропущенный звонок из чужого чата")
        core.publish(100, "Обычное сообщение")
        core.publish(100, "Пропущенный звонок 1")
        core.publish(100, "Пропущенный звонок 2")
        thread.join(10)
        assert [e["message"]["text"] for e in received] == ["Пропущенный звонок 1", "Пропущенный звонок 2"]


def test_a_cursor_from_before_a_restart_reports_a_gap(config, client, socket_path, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text("deadbeef-7")
    with RunningCore(config, client, CALLS, socket_path) as core:
        thread, received = collect(TelegramCore(socket_path), 2, cursor_file=cursor_file)
        core.wait_for_listeners()
        core.publish(100, "Пропущенный звонок")
        thread.join(10)
        assert [e["type"] for e in received] == ["gap", "new_message"]


def test_requests_and_denials(config, client, socket_path):
    with RunningCore(config, client, CALLS, socket_path):
        api = TelegramCore(socket_path)
        assert api.health()["client"] == "calls"
        assert [c["chat_id"] for c in api.find_chats()["chats"]] == [100]
        with pytest.raises(CoreError) as denied:
            api.read_messages(100, "2026-11-05", "2026-11-07")
        assert denied.value.status == 403


MONITOR = ClientPolicy(name="monitor", chats=None, events=frozenset({"new_message", "edited"}), read=True)


def collect_watch(core, count, **kwargs):
    received = []

    def consume():
        for item in core.watch(**kwargs):
            received.append(item)
            if len(received) == count:
                break

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    return thread, received


def test_watch_stream_filters_chats_types_and_text(config, client, socket_path):
    with RunningCore(config, client, MONITOR, socket_path) as core:
        api = TelegramCore(socket_path)
        thread, received = collect_watch(api, 1, chats=[100], pattern=r"call \d+")
        core.wait_for_listeners()
        core.publish(200, "call 1 from another chat")
        core.publish(100, "no match", message_id=2)
        core.publish(100, "call 2", kind="edited", message_id=2)
        core.publish(100, "call 3", message_id=3)
        thread.join(10)
        assert [e["message"]["text"] for e in received] == ["call 3"]


def test_watch_stream_subscribes_only_to_the_chats_it_names(config, client, socket_path):
    with RunningCore(config, client, MONITOR, socket_path) as core:
        thread, _ = collect_watch(TelegramCore(socket_path), 1, chats=[100])
        core.wait_for_listeners()
        (subscription,) = core.buffer._subscriptions
        assert subscription.chats == {100} and subscription.types == {"new_message"}
        core.publish(100, "done")
        thread.join(10)


def test_watch_stream_first_run_has_no_history_and_a_restart_catches_up(
    config, client, socket_path, tmp_path
):
    from conftest import make_message

    now = datetime.now(UTC)
    client.messages[100] = [make_message(1, now - timedelta(hours=1), "old history")]
    state_file = tmp_path / "state"

    with RunningCore(config, client, MONITOR, socket_path) as core:
        api = TelegramCore(socket_path)
        thread, received = collect_watch(api, 1, chats=[100], state_file=state_file)
        core.wait_for_listeners()
        core.publish(100, "live", message_id=2)
        thread.join(10)
        assert [e["message"]["text"] for e in received] == ["live"]

        # While the script is down, two messages arrive; the core keeps neither.
        client.messages[100][:0] = [
            make_message(4, now + timedelta(seconds=2), "missed 2"),
            make_message(3, now + timedelta(seconds=1), "missed 1"),
        ]
        core.wait_for_listeners(0)
        core.publish(100, "missed 2", message_id=4)

        thread, received = collect_watch(api, 3, chats=[100], state_file=state_file)
        core.wait_for_listeners()
        core.publish(100, "missed 2", message_id=4)  # Also arrives live: a duplicate, skipped.
        core.publish(100, "live again", message_id=5)
        thread.join(10)
        # Only new messages since the last one delivered are filled in, oldest first.
        assert [e["message"]["text"] for e in received] == ["missed 1", "missed 2", "live again"]


def test_watch_rejects_bad_options(config, client, socket_path):
    api = TelegramCore(socket_path)
    with pytest.raises(ValueError):
        next(api.watch(mode="poll"))
    with pytest.raises(ValueError):
        next(api.watch(mode="poll", chats=[100], interval=1))
    with pytest.raises(ValueError):
        next(api.watch(events=["sent"]))


def test_watch_poll_delivers_new_messages_and_resumes_from_the_state_file(
    config, client, socket_path, tmp_path, monkeypatch
):
    import tgcore_client
    from conftest import make_message

    monkeypatch.setattr(tgcore_client, "MIN_POLL_INTERVAL_SECONDS", 0)
    now = datetime.now(UTC)
    client.messages[100] = [make_message(1, now - timedelta(hours=1), "Missed call old")]
    state_file = tmp_path / "state"

    with RunningCore(config, client, MONITOR, socket_path):
        api = TelegramCore(socket_path)
        options = dict(mode="poll", interval=0.1, chats=[100], pattern="Missed", state_file=state_file)
        thread, received = collect_watch(api, 2, **options)
        time.sleep(0.5)  # First round: the old message is from before the start.
        client.messages[100][:0] = [
            make_message(3, now + timedelta(seconds=2), "Missed call B"),
            make_message(2, now + timedelta(seconds=1), "hello"),
        ]
        time.sleep(0.3)
        client.messages[100].insert(0, make_message(4, now + timedelta(seconds=3), "Missed call C"))
        thread.join(10)
        assert [e["message"]["text"] for e in received] == ["Missed call B", "Missed call C"]
        assert received[0]["chat_name"] == "Александр"

        # "C" was never followed by a request for more, so it comes again after a restart.
        thread, received = collect_watch(api, 1, **options)
        thread.join(10)
        assert [e["message"]["id"] for e in received] == [4]


def test_watch_poll_stops_on_a_chat_the_project_may_not_read(config, client, socket_path, monkeypatch):
    import tgcore_client

    monkeypatch.setattr(tgcore_client, "MIN_POLL_INTERVAL_SECONDS", 0)
    with RunningCore(config, client, MONITOR, socket_path):
        with pytest.raises(CoreError) as denied:
            next(TelegramCore(socket_path).watch(mode="poll", interval=0.1, chats=[200]))
        assert denied.value.status == 400 and "access is closed" in str(denied.value)
