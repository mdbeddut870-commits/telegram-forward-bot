"""KuCoin single-post regression: link must land in the FIRST post, no second post.

NOTE: legacy test written for KUCOIN_MODE=copy. Button mode is covered by
/tmp/kucoin_btn_test.py. Force copy mode here so both suites stay green.
"""
import asyncio
import os
import sys
import tempfile

REPO = "/Users/mdriponislam/Forwading/telegram-forward-bot"
sys.path.insert(0, REPO)
os.environ["DB_PATH"] = tempfile.mkdtemp() + "/kucoin_single.db"

import config
config.KUCOIN_MODE = "copy"
import forwarder

LINK = "https://www.kucoin.com/pt/gemslot/MHA"


class FakeMsg:
    def __init__(self, id, text="hello", media=None):
        self.id = id
        self.text = text
        self.message = text
        self.media = media
        self.date = "now"
        self.reply_to = None

    async def edit(self, text):
        raise AssertionError("KuCoin path must never edit")


calls = []


class FakeClient:
    async def forward_messages(self, *a, **k):
        calls.append(("forward",))
        return [FakeMsg(777, "fwd")]

    async def send_message(self, dest, text, **kw):
        calls.append(("send", dest, text))
        return FakeMsg(999, text)

    async def send_file(self, dest, media, caption=None, **kw):
        calls.append(("sendfile", dest, caption))
        return FakeMsg(999, caption)

    async def edit_message(self, *a, **k):
        calls.append(("legacy-edit",))

    async def get_messages(self, *a, **k):
        return None


DEST = {"dest_id": -1, "source_name": "SRC", "dest_name": "DST",
        "filter_type": "all", "keywords": "", "add_caption": "",
        "strip_caption": 0, "hide_header": 0, "dedup": 0, "mapping_id": 1}

# 1. KuCoin media post -> exactly ONE sendfile with link + source line
calls.clear()
r = asyncio.run(forwarder._forward_to_destination(
    FakeClient(), [FakeMsg(1, "KuCoin event live", media="m1")], 100, DEST))
assert r == [], r
assert len(calls) == 1 and calls[0][0] == "sendfile", calls
assert "Forwarded from SRC" in calls[0][2] and LINK in calls[0][2], calls
print("KUCOIN MEDIA SINGLE POST PASS")

# 2. KuCoin text post -> exactly ONE send with link + source line
calls.clear()
r = asyncio.run(forwarder._forward_to_destination(
    FakeClient(), [FakeMsg(2, "kucoin gemslot promo")], 100, DEST))
assert r == [], r
assert len(calls) == 1 and calls[0][0] == "send", calls
assert "Forwarded from SRC" in calls[0][2] and LINK in calls[0][2], calls
print("KUCOIN TEXT SINGLE POST PASS")

# 3. Normal post -> native forward only, no link, no second post
calls.clear()
r = asyncio.run(forwarder._forward_to_destination(
    FakeClient(), [FakeMsg(3, "hello world")], 100, DEST))
assert len(calls) == 1 and calls[0][0] == "forward", calls
print("NORMAL FORWARD PASS")

# 4. KuCoin album -> every item carries the link, no forward call
calls.clear()
r = asyncio.run(forwarder._forward_to_destination(
    FakeClient(),
    [FakeMsg(4, "kucoin drop", media="m1"), FakeMsg(5, "part2", media="m2")],
    100, DEST))
assert r == [], r
assert len(calls) == 2 and all(c[0] == "sendfile" for c in calls), calls
assert all(LINK in c[2] for c in calls), calls
print("KUCOIN ALBUM PASS")

print("ALL KUCOIN SINGLE-POST PASS")
