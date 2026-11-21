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

    def test_same_name_sessions_each_see_every_message(self):
        room = f"local:{self.tmp.name}/room.jsonl"
        one, two = telechat.Chat("bob", room), telechat.Chat("bob", room)
        self.assertEqual(one.read() + two.read(), [])  # both sessions are running
        telechat.Chat("alice", room).send("for both")
        self.assertEqual([m["text"] for m in one.read()], ["for both"])
        self.assertEqual([m["text"] for m in two.read()], ["for both"])

    def test_rate_guard_and_mentions(self):
        room = f"local:{self.tmp.name}/room.jsonl"
        alice, bob = telechat.Chat("alice", room), telechat.Chat("bob", room)
        self.addCleanup(setattr, telechat, "RATE", telechat.RATE)
        telechat.RATE = 3
        alice.send("@bob look at this")
        alice.send("mail bob@example.com, or ask @bobby and @bob.smith")
        alice.send("heads up @all.")
        with self.assertRaises(telechat.TelechatError) as cm:
            alice.send("one too many")
        self.assertIn("rate guard", str(cm.exception))
        got = bob.read()
        self.assertEqual([bool(m.get("mention")) for m in got], [True, False, True])
        self.assertIn("[mentions you] alice: @bob look", telechat.render(got))
        who = alice.who()
        self.assertEqual([(p["sender"], p["count"], p["you"]) for p in who], [("alice", 3, True)])
        topics, found = bob.search("LOOK bob")
        self.assertEqual((topics, [m["text"] for m in found]), ([], ["@bob look at this"]))

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

    def test_failed_poll_loses_nothing(self):
        fake, room = self.start()
        bob = telechat.Chat("bob", room)
        bob.read()
        fake.human("Chase", "do not drop me")
        fake.fail_threads = True
        with self.assertRaises(telechat.TelechatError):
            bob.read()
        fake.fail_threads = False
        self.assertEqual([m["text"] for m in bob.read()], ["do not drop me"])

    def test_deleted_webhook_is_recreated(self):
        fake, room = self.start()
        alice = telechat.Chat("alice", room)
        alice.send("one")
        fake.webhooks.clear()
        alice.send("two")  # falls back to the bot for this one message
        self.assertEqual(fake.messages[-1]["content"], "**[alice]** two")
        alice.send("three")
        self.assertEqual(fake.messages[-1]["author"]["username"], "alice")
        self.assertEqual(len(fake.webhooks), 1)

    def test_who_and_search_cover_topics(self):
        fake, room = self.start()
        alice, bob = telechat.Chat("alice", room), telechat.Chat("bob", room)
        alice.send("index is slow")
        alice.new_topic("Caching ideas", "should we cache the index?")
        fake.human("Chase", "try an LRU", channel=fake.thread("Caching ideas")["id"])
        who = {p["sender"]: p for p in bob.who()}
        self.assertEqual((who["alice"]["count"], who["alice"]["where"], who["alice"]["kind"]),
                         (2, "Caching ideas", "agent"))
        self.assertEqual(who["Chase"]["kind"], "human")
        self.assertIn("Chase (human): last posted", telechat.render_who(bob.who()))
        topics, found = bob.search("cach")
        self.assertEqual([t["title"] for t in topics], ["Caching ideas"])
        self.assertEqual([(m.get("topic"), m["text"]) for m in found],
                         [("Caching ideas", "should we cache the index?")])
        self.assertEqual([m["text"] for m in bob.search("index")[1]],
                         ["index is slow", "should we cache the index?"])
        self.assertIn("No match", telechat.render_search(*bob.search("zebra")))

    def test_file_sharing(self):
        for hooks in (True, False):
            fake, room = self.start(allow_webhooks=hooks)
            # each fake starts its ids over, so the second pass needs a clean reader
            alice, bob = telechat.Chat("alice", room), telechat.Chat(f"bob{int(hooks)}", room)
            bob.read()
            path = os.path.join(self.tmp.name, "notes.diff")
            with open(path, "w") as f:
                f.write("-old\n+new\n")
            alice.send_file(path, "the patch")
            alice.new_topic("Patches", "collecting them here")
            alice.send_file(path, topic="Patches")
            got = bob.read()
            self.assertEqual([(m["sender"], m.get("topic")) for m in got],
                             [("alice", None), ("alice", "Patches"), ("alice", "Patches")])
            self.assertTrue(got[0]["text"].startswith("the patch\n[file: notes.diff (10 B) http"))
            url = got[2]["text"].split()[-1].rstrip("]")
            self.assertEqual(bob.read_file(url), "-old\n+new\n")
        with self.assertRaises(telechat.TelechatError):
            bob.read_file("https://example.com/evil.txt")
        alice.send_file(path)  # no caption
        self.assertTrue(bob.read()[0]["text"].startswith("[file: notes.diff"))
        # the room's own key never goes out, wherever it hides
        copy = os.path.join(self.tmp.name, "innocent.txt")
        with open(copy, "w") as f:
            json.dump({"room": room}, f)
        before = len(fake.messages)
        other = telechat.make_code("another-bot-token", "999")  # a different room's invite
        shaped = "A" * 24 + "." + "b" * 6 + "." + "c" * 30  # token-shaped, not a real one
        for leak in (lambda: alice.send_file(copy), lambda: alice.send(f"join with {room}"),
                     lambda: alice.send(f"try {other}"), lambda: alice.send(f"bot token {shaped}"),
                     lambda: alice.send(f"token is {TOKEN}"), lambda: alice.new_topic("x", TOKEN)):
            with self.assertRaises(telechat.TelechatError):
                leak()
        self.assertEqual(len(fake.messages), before)
        for bad in (".env", "id_rsa", "server.pem"):
            secret = os.path.join(self.tmp.name, bad)
            with open(secret, "w") as f:
                f.write("x")
            with self.assertRaises(telechat.TelechatError):
                alice.send_file(secret)

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
                         ["chat_send", "chat_read", "chat_history", "chat_topics", "chat_new_topic",
                          "chat_who", "chat_search", "chat_send_file", "chat_read_file"])
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

        self.assertNotIn("mentions_you", note["params"]["meta"])

        carol = Mcp("carol", room, state, "--mentions-only")
        self.addCleanup(carol.close)
        self.assertIn("@carol", carol.instructions)
        alice.tool("chat_send", text="general chatter")
        alice.tool("chat_send", text="@carol can you check this?")
        note = carol.notification()
        self.assertEqual(note["params"]["content"], "@carol can you check this?")
        self.assertEqual(note["params"]["meta"]["mentions_you"], "true")
        self.assertIn("general chatter", carol.tool("chat_read")["content"][0]["text"])
        self.assertEqual([n["params"]["content"] for n in (bob.notification(), bob.notification())],
                         ["general chatter", "@carol can you check this?"])
        self.assertIn("alice - you", alice.tool("chat_who")["content"][0]["text"])

        bob.tool("chat_send", text="pong from bob")
        reply = alice.tool("chat_read", wait_seconds=5)
        self.assertIn("bob: pong from bob", reply["content"][0]["text"])
        self.assertTrue(alice.tool("nope").get("isError"))


if __name__ == "__main__":
    unittest.main()
