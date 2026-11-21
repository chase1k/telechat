import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "telechat.py")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import telechat  # noqa: E402
from fake_discord import TOKEN, FakeDiscord  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        telechat.STATE_DIR = os.path.join(self.tmp.name, "state")


class LocalRoomTest(Base):
    def test_two_agents_talk(self):
        room = f"local:{self.tmp.name}/room.jsonl"
        alice, bob = telechat.Chat("alice", room), telechat.Chat("bob", room)
        alice.send("hi bob")
        self.assertEqual([m["text"] for m in bob.read()], ["hi bob"])
        self.assertEqual(bob.read(), [])
        bob.send("hi alice")
        self.assertEqual([(m["sender"], m["text"]) for m in alice.read()], [("bob", "hi alice")])
        self.assertEqual(len(alice.history()), 2)

    def test_long_message_is_chunked_and_complete(self):
        room = f"local:{self.tmp.name}/room.jsonl"
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(60))
        ids = telechat.Chat("alice", room).send(text)
        self.assertGreater(len(ids), 1)
        got = telechat.Chat("bob", room).read()
        self.assertTrue(all(len(m["text"]) <= telechat.CHUNK for m in got))
        self.assertEqual("\n".join(m["text"] for m in got), text)

    def test_wait_returns_when_message_arrives(self):
        room = f"local:{self.tmp.name}/room.jsonl"
        alice, bob = telechat.Chat("alice", room), telechat.Chat("bob", room)
        threading.Timer(0.3, lambda: alice.send("late")).start()
        self.assertEqual([m["text"] for m in bob.read(wait=5)], ["late"])


class DiscordRoomTest(Base):
    def start(self, **kw):
        fake = FakeDiscord(**kw)
        self.addCleanup(fake.close)
        telechat.DISCORD_API = fake.url
        return fake, telechat.make_code(TOKEN, "555")

    def test_webhook_names_humans_and_cursor(self):
        fake, room = self.start()
        alice, bob = telechat.Chat("alice", room), telechat.Chat("bob", room)
        self.assertEqual(alice.backend.check(), "Discord channel #agents")
        alice.send("idea: cache it @everyone")
        fake.human("Chase", "sounds good")
        fake.add({"id": "42", "username": "x"}, "", type=7)  # member-join system message
        got = bob.read()
        self.assertEqual([(m["sender"], m["kind"], m["text"]) for m in got],
                         [("alice", "agent", "idea: cache it @everyone"), ("Chase", "human", "sounds good")])
        self.assertEqual(bob.read(), [])
        self.assertEqual([m["sender"] for m in alice.read()], ["Chase"])  # own message filtered
        self.assertEqual(len(fake.webhooks), 1)
        bob.send("agreed")
        self.assertEqual(len(fake.webhooks), 1)  # bob reuses the room webhook
        self.assertEqual(fake.messages[-1]["author"]["username"], "bob")

    def test_falls_back_to_prefixed_bot_posts(self):
        fake, room = self.start(allow_webhooks=False)
        alice, bob = telechat.Chat("alice", room), telechat.Chat("bob", room)
        alice.send("no webhooks here")
        self.assertEqual(fake.messages[-1]["content"], "**[alice]** no webhooks here")
        self.assertEqual([(m["sender"], m["kind"], m["text"]) for m in bob.read()],
                         [("alice", "agent", "no webhooks here")])
        self.assertEqual(alice.read(), [])

    def test_pagination_past_100(self):
        fake, room = self.start()
        bob = telechat.Chat("bob", room)
        fake.human("Chase", "first")
        self.assertEqual(len(bob.read()), 1)
        for i in range(230):
            fake.human("Chase", f"m{i}")
        got = bob.read()
        self.assertEqual([m["text"] for m in got], [f"m{i}" for i in range(230)])

    def test_topics_new_reply_and_read(self):
        fake, room = self.start()
        alice, bob = telechat.Chat("alice", room), telechat.Chat("bob", room)
        fake.start_thread("777", "Old news", {"id": "42", "username": "chase"}, "from before bob joined")
        self.assertEqual(bob.read(), [])  # existing topics are not replayed on first read

        topic = alice.new_topic("Caching ideas", "should we cache?")
        self.assertEqual(topic["forum"], "ideas")
        self.assertEqual(fake.thread("Caching ideas")["parent_id"], "777")
        got = bob.read()
        self.assertEqual([(m["sender"], m.get("topic"), m["text"]) for m in got],
                         [("alice", "Caching ideas", "should we cache?")])

        bob.send("yes, with a TTL", topic="caching")  # partial title match
        fake.human("Chase", "main channel chatter")
        fake.human("Chase", "old thread reply", channel=fake.thread("Old news")["id"])
        got = alice.read()
        self.assertEqual([(m["sender"], m.get("topic"), m["text"]) for m in got],
                         [("bob", "Caching ideas", "yes, with a TTL"),
                          ("Chase", None, "main channel chatter"),
                          ("Chase", "Old news", "old thread reply")])
        self.assertEqual(alice.read(), [])
        self.assertEqual(fake.in_channel("555"), fake.in_channel("555")[-1:])  # nothing leaked to #agents
        self.assertEqual([m["text"] for m in bob.history(topic="Caching ideas")],
                         ["should we cache?", "yes, with a TTL"])
        self.assertEqual(sorted(t["title"] for t in bob.topics()), ["Caching ideas", "Old news"])
        with self.assertRaises(telechat.TelechatError):
            bob.send("where?", topic="no such topic")

    def test_topics_without_webhooks(self):
        fake, room = self.start(allow_webhooks=False)
        alice, bob = telechat.Chat("alice", room), telechat.Chat("bob", room)
        bob.read()
        alice.new_topic("Plain bot", "opening")
        alice.send("follow-up", topic="Plain bot")
        self.assertEqual([(m["sender"], m["topic"], m["text"]) for m in bob.read()],
                         [("alice", "Plain bot", "opening"), ("alice", "Plain bot", "follow-up")])

    def test_local_room_has_no_topics(self):
        chat = telechat.Chat("alice", f"local:{self.tmp.name}/room.jsonl")
        self.assertEqual(chat.topics(), [])
        with self.assertRaises(telechat.TelechatError):
            chat.new_topic("x", "y")

    def test_bad_token_and_bad_code(self):
        self.start()
        with self.assertRaises(telechat.TelechatError) as cm:
            telechat.Chat("alice", telechat.make_code("wrong", "555")).read()
        self.assertEqual(cm.exception.status, 401)
        with self.assertRaises(telechat.TelechatError):
            telechat.Chat("alice", "tc1_garbage")


class Mcp:
    def __init__(self, name, room, state, *flags):
        env = dict(os.environ, TELECHAT_NAME=name, TELECHAT_ROOM=room, TELECHAT_STATE=state,
                   TELECHAT_CONFIG=os.path.join(state, "none.json"))
        self.proc = subprocess.Popen([sys.executable, SCRIPT, "mcp", *flags], env=env, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.lines = queue.Queue()
        threading.Thread(target=lambda: [self.lines.put(json.loads(l)) for l in self.proc.stdout],
                         daemon=True).start()
        self.n = 0
        self.notes = []
        init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "0"}})
        self.capabilities = init["capabilities"]
        self.instructions = init["instructions"]
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def write(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def next(self, timeout=10):
        return self.lines.get(timeout=timeout)

    def request(self, method, params=None):
        self.n += 1
        self.write({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        while True:
            msg = self.next()
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise AssertionError(msg["error"])
                return msg["result"]
            self.notes.append(msg)

    def tool(self, name, **args):
        return self.request("tools/call", {"name": name, "arguments": args})

    def notification(self):
        return self.notes.pop(0) if self.notes else self.next()

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()


class McpTest(Base):
    def test_tools_and_push(self):
        room = f"local:{self.tmp.name}/room.jsonl"
        state = os.path.join(self.tmp.name, "state")
        alice = Mcp("alice", room, state)
        self.addCleanup(alice.close)
        self.assertNotIn("experimental", alice.capabilities)
        self.assertEqual([t["name"] for t in alice.request("tools/list")["tools"]],
                         ["chat_send", "chat_read", "chat_history", "chat_topics", "chat_new_topic"])
        self.assertIn("No topics yet", alice.tool("chat_topics")["content"][0]["text"])
        alice.tool("chat_send", text="before bob joined")

        bob = Mcp("bob", room, state, "--push")
        self.addCleanup(bob.close)
        self.assertEqual(bob.capabilities["experimental"], {"claude/channel": {}})
        self.assertEqual(bob.request("ping"), {})
        self.assertIn("before bob joined", bob.tool("chat_history")["content"][0]["text"])
        alice.tool("chat_send", text="ping from alice")
        note = bob.notification()
        self.assertEqual(note["method"], "notifications/claude/channel")
        self.assertEqual(note["params"]["content"], "ping from alice")
        self.assertEqual(note["params"]["meta"]["sender"], "alice")

        bob.tool("chat_send", text="pong from bob")
        reply = alice.tool("chat_read", wait_seconds=5)
        self.assertIn("bob: pong from bob", reply["content"][0]["text"])
        self.assertTrue(alice.tool("nope").get("isError"))


if __name__ == "__main__":
    unittest.main()
