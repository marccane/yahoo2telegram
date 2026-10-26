#!/usr/bin/env python3
"""Forward new Yahoo Mail messages to a Telegram group or one of its topics.

Each run connects to Yahoo over IMAP, fetches every Inbox and Spam message
newer than the last one it forwarded, and posts it (headers, body text,
attachments) to a Telegram group via a bot, optionally into a forum topic.
Meant to be run every minute by a systemd timer. Only the Python standard
library is used.

Configuration comes from environment variables (see yahoo2telegram.env.default).
Run with --test to send a test message and exit.
"""
import email
import email.policy
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from html.parser import HTMLParser
from pathlib import Path

IMAP_HOST = "imap.mail.yahoo.com"
TG_TEXT_LIMIT = 4096
TG_FILE_LIMIT = 50 * 1024 * 1024


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        sys.exit(f"missing environment variable {name}")
    return value


YAHOO_USER = env("YAHOO_USER")
YAHOO_APP_PASSWORD = env("YAHOO_APP_PASSWORD")
TG_BOT_TOKEN = env("TG_BOT_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")
TG_THREAD_ID = os.environ.get("TG_THREAD_ID", "").strip()  # optional forum topic
if TG_THREAD_ID and not TG_THREAD_ID.isdigit():
    sys.exit("TG_THREAD_ID must be a number")
MAILBOX = env("MAILBOX", "INBOX")
SPAM_FOLDER = env("SPAM_FOLDER", "auto")  # "auto", a folder name, or "none"
MAX_BODY_CHARS = int(env("MAX_BODY_CHARS", "12000"))
FORWARD_ATTACHMENTS = env("FORWARD_ATTACHMENTS", "1") == "1"
BACKFILL = env("BACKFILL", "0") == "1"
STATE_FILE = Path(env("STATE_DIRECTORY", str(Path(__file__).resolve().parent))) / "state.json"

TG_TARGET = {"chat_id": TG_CHAT_ID}
if TG_THREAD_ID:
    TG_TARGET["message_thread_id"] = int(TG_THREAD_ID)


# ---------------------------------------------------------------- telegram

def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n").encode()
    for key, (filename, data) in files.items():
        filename = re.sub(r'["\r\n\\]', "_", filename)
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def tg(method, fields, files=None):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    if files:
        body, ctype = _multipart(fields, files)
    else:
        body, ctype = json.dumps(fields).encode(), "application/json"
    for _ in range(5):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.load(resp)
        except urllib.error.HTTPError as e:
            result = json.load(e)
        if result.get("ok"):
            return result["result"]
        retry_after = result.get("parameters", {}).get("retry_after")
        if retry_after:  # flood control: wait and retry
            time.sleep(retry_after + 1)
            continue
        raise RuntimeError(f"Telegram {method} failed: {result.get('description')}")
    raise RuntimeError(f"Telegram {method} failed: still rate limited")


def send_text(text):
    for chunk in split_text(text, TG_TEXT_LIMIT):
        tg("sendMessage", {
            **TG_TARGET,
            "text": chunk,
            "link_preview_options": {"is_disabled": True},
        })


def send_file(filename, data, caption=""):
    tg("sendDocument", {
        **TG_TARGET,
        "caption": caption[:1024],
    }, {"document": (filename, data)})


def split_text(text, limit):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks


# ---------------------------------------------------------------- email parsing

class _HtmlToText(HTMLParser):
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "table", "blockquote", "hr"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup):
    parser = _HtmlToText()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


def clean(text):
    lines = [re.sub(r"[ \t\xa0​]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def body_text(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):  # unknown or broken charset
        content = part.get_payload(decode=True).decode("utf-8", "replace")
    if part.get_content_subtype() == "html":
        content = html_to_text(content)
    return clean(content)


def forward(msg, label=""):
    header = [label] if label else []
    header += [f"📧 {msg.get('Subject', '(no subject)')}",
               f"From: {msg.get('From', '?')}"]
    if msg.get("To"):
        header.append(f"To: {msg['To']}")
    if msg.get("Cc"):
        header.append(f"Cc: {msg['Cc']}")
    if msg.get("Date"):
        header.append(f"Date: {msg['Date']}")

    body = body_text(msg)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n[… truncated]"

    attachments = []
    if FORWARD_ATTACHMENTS:
        for part in msg.iter_attachments():
            data = part.get_payload(decode=True) or b""
            name = part.get_filename() or f"attachment.{part.get_content_subtype()}"
            attachments.append((name, data))
    if attachments:
        header.append(f"Attachments: {len(attachments)}")

    send_text("\n".join(header) + "\n\n" + (body or "(no text body)"))

    for name, data in attachments:
        if len(data) > TG_FILE_LIMIT:
            send_text(f"⚠️ Attachment {name} skipped ({len(data) // 1024**2} MB, bot limit is 50 MB)")
            continue
        try:
            send_file(name, data)
        except RuntimeError as e:
            send_text(f"⚠️ Attachment {name} could not be sent: {e}")


# ---------------------------------------------------------------- state & imap

def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {"mailboxes": {}, "recent_ids": []}
    if "uidvalidity" in state:  # format used before the spam folder was watched
        state = {"mailboxes": {MAILBOX: state}, "recent_ids": []}
    return state


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def quote(mailbox):
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find_spam_folder(imap):
    if SPAM_FOLDER.lower() == "none":
        return None
    if SPAM_FOLDER.lower() != "auto":
        return SPAM_FOLDER
    typ, data = imap.list()
    names = {}
    for line in data if typ == "OK" else []:
        m = re.match(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) "?(?P<name>.*?)"?$', line or b"")
        if m:
            names[m["name"].decode()] = m["flags"].decode().lower()
    # Yahoo flags its spam folder ("Bulk") as \Junk.
    return next((n for n, flags in names.items() if "\\junk" in flags),
                "Bulk" if "Bulk" in names else None)


def mailbox_status(imap, mailbox):
    typ, data = imap.status(quote(mailbox), "(UIDVALIDITY UIDNEXT)")
    if typ != "OK":
        raise RuntimeError(f"STATUS {mailbox} failed: {data}")
    line = data[0].decode()
    validity = int(re.search(r"UIDVALIDITY (\d+)", line).group(1))
    uidnext = int(re.search(r"UIDNEXT (\d+)", line).group(1))
    return validity, uidnext


def message_id(msg):
    try:
        return str(msg.get("Message-ID") or "").strip()
    except Exception:  # malformed header
        return ""


def sync(imap, state, mailbox, label):
    validity, uidnext = mailbox_status(imap, mailbox)
    box = state["mailboxes"].get(mailbox)
    if box is None or box["uidvalidity"] != validity:
        if box is not None:
            print(f"{mailbox}: UIDVALIDITY changed, restarting from the newest message")
        # First time this mailbox is seen: start from "now" so everything
        # already in it isn't dumped into the group, unless BACKFILL=1.
        start = 0 if (box is None and BACKFILL) else uidnext - 1
        box = state["mailboxes"][mailbox] = {"uidvalidity": validity, "last_uid": start}
        save_state(state)

    imap.select(quote(mailbox), readonly=True)  # read-only: never marks mail as read
    typ, data = imap.uid("SEARCH", None, f"UID {box['last_uid'] + 1}:*")
    if typ != "OK":
        raise RuntimeError(f"SEARCH {mailbox} failed: {data}")
    # "N:*" always matches the highest UID, even when it is below N.
    uids = sorted(u for u in map(int, data[0].split()) if u > box["last_uid"])

    for uid in uids:
        typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        raw = next((item[1] for item in data if isinstance(item, tuple)), None)
        if typ == "OK" and raw:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            msgid = message_id(msg)
            # A mail moved out of spam with "Not spam" reappears in the inbox
            # under a new UID; don't post it a second time.
            if msgid and msgid in state["recent_ids"]:
                print(f"{mailbox} UID {uid}: already forwarded, skipped")
            else:
                forward(msg, label)
                print(f"{mailbox} UID {uid}: forwarded")
                if msgid:
                    state["recent_ids"] = (state["recent_ids"] + [msgid])[-1000:]
        box["last_uid"] = uid
        save_state(state)  # after each message, so a crash never re-sends


def run():
    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=60)
    try:
        imap.login(YAHOO_USER, YAHOO_APP_PASSWORD)
        state = load_state()
        sync(imap, state, MAILBOX, "")
        spam = find_spam_folder(imap)
        if spam:
            sync(imap, state, spam, "🚫 Yahoo put this in the Spam folder")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_text("✅ yahoo2telegram test message: the bot can post here.")
        print("test message sent")
    else:
        run()
