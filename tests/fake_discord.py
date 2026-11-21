"""A tiny in-memory stand-in for the parts of the Discord REST API telechat uses.

One server ("1") with a text channel 555 (#agents) and a forum channel 777 (ideas).
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "fake-token"
BOT = {"id": "900", "username": "roombot", "bot": True}
TEXT, FORUM = "555", "777"


class FakeDiscord:
    def __init__(self, allow_webhooks=True):
        self.allow_webhooks = allow_webhooks
        self.fail_threads = False
        self.messages = []
        self.webhooks = []
        self.threads = []
        self.files = {}
        self.next_id = 1000
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def handle_any(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if url.path.startswith("/attachments/"):
                    data = fake.files.get(url.path)
                    self.send_response(200 if data is not None else 404)
                    self.send_header("Content-Length", str(len(data or b"")))
                    self.end_headers()
                    self.wfile.write(data or b"")
                    return
                ctype = self.headers.get("Content-Type") or ""
                if ctype.startswith("multipart/form-data"):
                    body = {}
                    for part in raw.split(b"--" + ctype.split("boundary=")[1].encode())[1:-1]:
                        head, _, content = part.partition(b"\r\n\r\n")
                        content = content[:-2]  # trailing CRLF
                        if b'name="payload_json"' in head:
                            body.update(json.loads(content))
                        else:
                            fname = re.search(rb'filename="([^"]*)"', head).group(1).decode()
                            body["_file"] = (fname, content)
                else:
                    body = json.loads(raw) if raw else {}
                authed = self.headers.get("Authorization") == f"Bot {TOKEN}"
                code, out = fake.route(self.command, url.path, query, body, authed)
                self.reply(code, out)

            do_GET = do_POST = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add(self, author, content, channel=TEXT, **extra):
        with self.lock:
            self.next_id += 1
            msg = {"id": str(self.next_id), "type": 0, "content": content, "author": author,
                   "channel_id": channel, "timestamp": "2026-11-20T12:00:00.000000+00:00",
                   "attachments": [], **extra}
            self.messages.append(msg)
            for t in self.threads:
                if t["id"] == channel:
                    t["last_message_id"] = msg["id"]
            return msg

    def attach(self, body):
        if "_file" not in body:
            return {}
        fname, data = body["_file"]
        path = f"/attachments/{len(self.files) + 1}/{fname}"
        self.files[path] = data
        return {"attachments": [{"id": "1", "filename": fname, "size": len(data), "url": self.url + path}]}

    def human(self, name, content, channel=TEXT):
        return self.add({"id": "42", "username": name.lower(), "global_name": name}, content, channel)

    def start_thread(self, parent, title, author, content, **extra):
        with self.lock:
            tid = str(self.next_id + 1)  # like Discord: the opening message shares the thread's id
            self.threads.append({"id": tid, "name": title, "parent_id": parent, "type": 11,
                                 "last_message_id": None})
        return self.add(author, content, tid, **extra)

    def thread(self, title):
        return next(t for t in self.threads if t["name"] == title)

    def in_channel(self, channel):
        return [m for m in self.messages if m["channel_id"] == channel]

    def route(self, method, path, query, body, authed):
        hook = re.fullmatch(r"/webhooks/(\d+)/(\w+)", path)
        if hook and method == "POST":
            wh = next((w for w in self.webhooks
                       if w["id"] == hook.group(1) and w["token"] == hook.group(2)), None)
            if not wh:
                return 404, {"message": "Unknown Webhook"}
            author = {"id": wh["id"], "username": body.get("username", "telechat"), "bot": True}
            if "thread_name" in body:
                if wh["channel_id"] != FORUM:
                    return 400, {"message": "Webhooks can only create threads in forum channels"}
                return 200, self.start_thread(FORUM, body["thread_name"], author, body["content"],
                                              webhook_id=wh["id"])
            target = query.get("thread_id", wh["channel_id"])
            if "thread_id" in query and not any(
                    t["id"] == target and t["parent_id"] == wh["channel_id"] for t in self.threads):
                return 400, {"message": "Unknown Channel"}
            if target == FORUM:
                return 400, {"message": "Webhooks posted to forum channels must have a thread_name or thread_id"}
            return 200, self.add(author, body["content"], target, webhook_id=wh["id"], **self.attach(body))
        if not authed:
            return 401, {"message": "401: Unauthorized"}
        if path == "/users/@me":
            return 200, BOT
        if path == "/guilds/1/channels":
            return 200, [{"id": TEXT, "name": "agents", "type": 0},
                         {"id": FORUM, "name": "ideas", "type": 15}]
        if path == "/guilds/1/threads/active":
            if self.fail_threads:
                return 500, {"message": "Internal Server Error"}
            return 200, {"threads": self.threads}
        if path == f"/channels/{FORUM}/threads/archived/public":
            return 200, {"threads": []}
        if path == f"/channels/{FORUM}/threads" and method == "POST":
            msg = self.start_thread(FORUM, body["name"], BOT, body["message"]["content"])
            return 200, {"id": msg["channel_id"], "name": body["name"]}
        chan = re.fullmatch(r"/channels/(\d+)(/messages|/webhooks)?", path)
        known = [TEXT, FORUM] + [t["id"] for t in self.threads]
        if not chan or chan.group(1) not in known:
            return 404, {"message": "Unknown Channel"}
        cid, what = chan.group(1), chan.group(2)
        if what is None:
            return 200, {"id": cid, "name": "agents", "guild_id": "1"}
        if what == "/webhooks":
            if not self.allow_webhooks:
                return 403, {"message": "Missing Permissions"}
            if method == "GET":
                return 200, [w for w in self.webhooks if w["channel_id"] == cid]
            wh = {"id": str(7000 + len(self.webhooks)), "token": "whsecret", "name": body["name"],
                  "channel_id": cid}
            self.webhooks.append(wh)
            return 200, wh
        if cid == FORUM:
            return 400, {"message": "Cannot send or read messages in a forum channel directly"}
        if method == "POST":
            return 200, self.add(BOT, body["content"], cid, **self.attach(body))
        mine = self.in_channel(cid)
        limit = int(query.get("limit", 50))
        if "after" in query:
            newer = [m for m in mine if int(m["id"]) > int(query["after"])]
            return 200, list(reversed(newer[:limit]))
        return 200, list(reversed(mine[-limit:]))
