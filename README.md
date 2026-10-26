# yahoo2telegram

Forwards the mail arriving in a Yahoo Mail account to a Telegram group, or to
one topic of a group. A systemd timer runs the script every minute. Each run,
the script logs in to Yahoo over IMAP, picks up whatever arrived since the
previous run, and has a Telegram bot post it to the group.

It is a single Python file using only the standard library, so there is
nothing to `pip install`.

## What gets forwarded

- **Every new mail in the Inbox**: subject, From, To, Cc, Date and the body
  as plain text (HTML mail is converted). Long bodies are cut at 12,000
  characters and split across several Telegram messages.
- **Attachments**, sent as files right after the message.
- **Every new mail in the Spam folder**, tagged with
  `🚫 Yahoo put this in the Spam folder`. Yahoo's spam filter can't be turned
  off, and it sometimes catches real mail, so this makes sure nothing is lost.
  It can be switched off (see [Configuration](#configuration)).

Mailbox access is read-only, so the script never marks mail as read, moves or
deletes it. It remembers the last mail it forwarded, so nothing is posted twice.
On its first run it only notes where the mailbox currently ends, which means
mail that is already there is not sent.

## Requirements

- A Linux server with systemd (any VPS will do) and Python 3.8 or newer.
- A Yahoo Mail account (free accounts work).
- A Telegram group where you are an admin. It can have **Topics** enabled,
  in which case the mail can go to one particular topic.

## Setup

### 1. Create a Yahoo app password

Yahoo does not accept your normal password over IMAP.

1. Go to <https://login.yahoo.com/account/security>.
2. Choose **Generate app password** (it may be called *Generate and manage app
   passwords*), name it e.g. `yahoo2telegram`, and copy the 16-letter password.

### 2. Create the Telegram bot

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather), send
   `/newbot`, and follow the prompts.
2. Copy the token it gives you (looks like `123456789:AAH…`).
3. Add the bot to your group and **make it an admin**. Admin rights are the
   simplest way to be sure it can post, including in closed topics.

### 3. Find the group's chat ID (and topic ID)

In Telegram, right-click (or long-press) any message in the group and choose
**Copy Message Link**. If you want the mail in a particular topic, pick a
message inside that topic. You get one of these:

```
https://t.me/c/1234567890/45          group without Topics
               └────┬───┘ └ message (ignore)
                 group

https://t.me/c/1234567890/3/45        message inside a topic
               └────┬───┘ │ └ message (ignore)
                 group    topic
```

- **Chat ID**: put `-100` in front of the group number → `-1001234567890`
- **Topic ID** (optional): the middle number → `3`. Without one, the mail goes
  to the group itself, which in a group with Topics means the *General* topic.

A public group's links show its name (`t.me/groupname/45`) instead of the
number. In that case use the method below.

<details>
<summary>Alternative: ask the Telegram API</summary>

Send `/start@YourBotName` in the group (inside the topic, if you want one),
then run:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

The reply contains `"chat":{"id":-100…}` (the chat ID) and
`"message_thread_id":3` (the topic ID, only present inside a topic).
</details>

### 4. Install

Copy this folder to the server. Inside it, create your own settings file
from the template and fill in your values from steps 1–3:

```bash
install -m 600 yahoo2telegram.env.default .env
nano .env
```

```ini
YAHOO_USER=yourname@yahoo.com
YAHOO_APP_PASSWORD=abcdefghijklmnop
TG_BOT_TOKEN=123456789:AAH...
TG_CHAT_ID=-1001234567890
TG_THREAD_ID=3
```

Leave out `TG_THREAD_ID` to post to the group itself. Comments must be on
their own line; `KEY=value  # comment` would make the comment part of the
value.

`.env` is listed in `.gitignore`, so your password and bot token can't be
committed by mistake. Only edit `yahoo2telegram.env.default` to change the template itself.

Then install everything:

```bash
sudo mkdir -p /opt/yahoo2telegram
sudo cp yahoo2telegram.py /opt/yahoo2telegram/
sudo install -m 600 .env /etc/yahoo2telegram.env
sudo cp yahoo2telegram.service yahoo2telegram.timer /etc/systemd/system/
```

The installed settings file is readable by root only (`-m 600`), because it
holds your mail password and bot token. Once installed, you can delete the
local `.env` or keep it as a backup.

### 5. Test the bot

This runs the script with exactly the settings the service will use, and
sends a test message to the group:

```bash
sudo systemd-run --quiet --pipe --wait -p EnvironmentFile=/etc/yahoo2telegram.env \
    /usr/bin/python3 /opt/yahoo2telegram/yahoo2telegram.py --test
```

You should see `test message sent` and the message should appear in the
group (or topic).
If not, see [Troubleshooting](#troubleshooting).

### 6. Start it

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yahoo2telegram.timer
```

Check that it is working:

```bash
systemctl list-timers yahoo2telegram.timer   # next / last run
journalctl -u yahoo2telegram -f              # live log
```

Then send a mail to the Yahoo address. It should appear in Telegram within
about a minute, and the log shows `INBOX UID … : forwarded`.

## Configuration

All settings live in `/etc/yahoo2telegram.env`. Changes apply from the next
run, so nothing needs restarting.

| Setting | Default | Meaning |
|---|---|---|
| `YAHOO_USER` | *required* | Yahoo address |
| `YAHOO_APP_PASSWORD` | *required* | App password from step 1 |
| `TG_BOT_TOKEN` | *required* | Bot token from @BotFather |
| `TG_CHAT_ID` | *required* | Group chat ID (`-100…`) |
| `TG_THREAD_ID` | *unset* | Topic ID. Unset means the group itself (the *General* topic in a group with Topics) |
| `MAILBOX` | `INBOX` | Folder to forward |
| `SPAM_FOLDER` | `auto` | Spam folder to forward as well: `auto` finds it (Yahoo calls it `Bulk`), `none` turns spam forwarding off, or give a folder name |
| `MAX_BODY_CHARS` | `12000` | Longer bodies are cut, marked `[… truncated]` |
| `FORWARD_ATTACHMENTS` | `1` | `0` to send only the text |
| `BACKFILL` | `0` | `1` makes a mailbox's **first** run forward everything already in it, instead of starting from now. Careful, this can be hundreds of messages |

## Day-to-day use

- **Real mail in Spam**: it is forwarded anyway, with the 🚫 tag. Clicking
  **Not spam** in Yahoo helps the filter learn. The mail then moves to the
  Inbox, but the script recognises it and does not post it a second time.
- **Mail that was already in Spam before setup** is not forwarded. To get one,
  click **Not spam** in Yahoo: it lands in the Inbox as new mail and is
  forwarded on the next run.
- **Updating the script**: copy the new `yahoo2telegram.py` to
  `/opt/yahoo2telegram/`. The next run picks it up.
- **Starting over** (forget what was forwarded and continue from "now"):
  `sudo rm /var/lib/yahoo2telegram/state.json`

## Troubleshooting

Errors appear in `journalctl -u yahoo2telegram`.

| Error | Cause |
|---|---|
| `AUTHENTICATIONFAILED` / `LOGIN failed` | Wrong Yahoo address or app password. The normal account password does not work. If a fresh app password is also refused, IMAP access may be restricted on that account. |
| `Unauthorized` | Wrong bot token. |
| `chat not found` | Bot is not in the group, or the chat ID is wrong (it must start with `-100`). |
| `message thread not found` | Wrong topic ID. Fix it, or remove `TG_THREAD_ID` to post to the group itself. |
| `not enough rights to send` / `TOPIC_CLOSED` | Make the bot an admin of the group. |
| `missing environment variable …` | That line is missing or empty in `/etc/yahoo2telegram.env`. |

## Limitations

- New mail shows up with up to about a minute of delay (the timer interval;
  change `OnUnitActiveSec` in the `.timer` file to adjust).
- Attachments over 50 MB are not sent (a Telegram bot limit). A note is posted
  instead.
- Only Yahoo is supported as written. Other IMAP providers should work by
  changing `IMAP_HOST` at the top of the script, but that has not been tested.

## Uninstall

```bash
sudo systemctl disable --now yahoo2telegram.timer
sudo rm /etc/systemd/system/yahoo2telegram.{service,timer} /etc/yahoo2telegram.env
sudo rm -r /opt/yahoo2telegram /var/lib/yahoo2telegram /var/lib/private/yahoo2telegram
sudo systemctl daemon-reload
```

Also delete the app password in your Yahoo account security settings.

## Files

| File | Purpose |
|---|---|
| `yahoo2telegram.py` | The script |
| `yahoo2telegram.env.default` | Settings template. Copy it to `.env`, fill it in, install it as `/etc/yahoo2telegram.env` |
| `.gitignore` | Keeps `.env` (and run-time files) out of git |
| `yahoo2telegram.service` | systemd unit that runs the script once |
| `yahoo2telegram.timer` | Runs the service every minute |

## License

MIT, see [LICENSE](LICENSE).
