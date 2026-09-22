"""Batch-2 regression tests: config safe-parsing, stats counters, header guarantee."""
import os
import sys
import tempfile

REPO = "/Users/mdriponislam/Forwading/telegram-forward-bot"
sys.path.insert(0, REPO)


def test_config_bad_env_no_crash():
    os.environ["SEND_CONCURRENCY"] = "not-a-number"
    os.environ["SOURCE_POLL_INTERVAL_SECONDS"] = "bogus"
    os.environ["SOURCE_POLL_SOURCE_IDS"] = "1, xx, -100123"
    os.environ["REMOVE_SOURCE_IDS"] = "bad,, -5"
    for mod in ("config",):
        sys.modules.pop(mod, None)
    import config
    assert config.SEND_CONCURRENCY == 16, config.SEND_CONCURRENCY
    assert config.SOURCE_POLL_INTERVAL_SECONDS == 10, config.SOURCE_POLL_INTERVAL_SECONDS
    assert -100123 in config.SOURCE_POLL_SOURCE_IDS
    assert -5 in config.REMOVE_SOURCE_IDS
    print("CONFIG SAFE-PARSE PASS")


def test_stats_table():
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "t.db")
    for mod in ("config", "database"):
        sys.modules.pop(mod, None)
    import database as db
    db.init_db()
    db.bump_stat(1, "forwarded")
    db.bump_stat(1, "forwarded")
    db.bump_stat(1, "failed")
    db.bump_stat(2, "dedup_skip")
    s = db.get_stats(7)
    assert s["forwarded"] == 2, s
    assert s["failed"] == 1, s
    assert s["dedup_skip"] == 1, s
    assert len(s["per_day"]) == 1
    assert s["top"][0]["mapping_id"] == 1
    print("STATS TABLE PASS")


def test_header_guarantee():
    import asyncio
    for mod in ("config", "database", "forwarder"):
        sys.modules.pop(mod, None)
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "h.db")
    import forwarder

    class FakeMsg:
        def __init__(self, id, text="hello", media=None):
            self.id = id
            self.text = text
            self.message = text
            self.media = media
            self.date = "now"
            self.reply_to = None

    calls = []

    class FakeClient:
        async def forward_messages(self, dest, ids, from_peer=None, drop_author=None):
            calls.append(("forward", dest, ids, from_peer, drop_author))
            mids = ids if isinstance(ids, list) else [ids]
            return [FakeMsg(m, "fwd") for m in mids]

        async def send_message(self, dest, text, file=None):
            calls.append(("send", dest, text, file))
            return FakeMsg(999, text)

        async def edit_message(self, dest, msg, text):
            calls.append(("edit", dest, getattr(msg, "id", msg), text))

    class FailClient(FakeClient):
        async def forward_messages(self, *a, **k):
            raise Exception("protected")

    DEST = {"dest_id": -1, "source_name": "SRC", "dest_name": "DST",
            "filter_type": "all", "keywords": "", "add_caption": "",
            "strip_caption": 0, "hide_header": 1, "dedup": 0, "mapping_id": 1}
    # plain
    calls.clear()
    asyncio.run(forwarder._forward_to_destination(FakeClient(), [FakeMsg(10)], 100, DEST))
    assert calls[0][0] == "forward" and calls[0][4] is False
    assert not any(c[0] == "send" for c in calls)
    # caption -> edit not send
    calls.clear()
    asyncio.run(forwarder._forward_to_destination(FakeClient(), [FakeMsg(11, "orig")], 100, dict(DEST, add_caption="EXTRA")))
    assert any(c[0] == "edit" for c in calls)
    assert not any(c[0] == "send" for c in calls)
    # fallback album: both attributed, returns []
    calls.clear()
    r = asyncio.run(forwarder._forward_to_destination(
        FailClient(), [FakeMsg(20, "t1", media="m1"), FakeMsg(21, "t2", media="m2")], 100, DEST))
    assert r == []
    sends = [c for c in calls if c[0] == "send"]
    assert len(sends) == 2 and all("Forwarded from SRC" in c[2] for c in sends)
    # claim LRU bounded
    forwarder._claimed_messages.clear()
    for i in range(forwarder._CLAIMED_MAX_ENTRIES + 100):
        forwarder._claim_message(9, i)
    assert len(forwarder._claimed_messages) <= forwarder._CLAIMED_MAX_ENTRIES
    print("HEADER GUARANTEE PASS")


if __name__ == "__main__":
    test_config_bad_env_no_crash()
    test_stats_table()
    test_header_guarantee()
    print("ALL BATCH2 PASS")
