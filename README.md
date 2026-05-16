# azurabot

An IRC bot that announces now-playing information from an [AzuraCast](https://www.azuracast.com/) radio station via webhooks. Supports multiple IRC networks simultaneously, per-channel announce modes, and full management via IRC private messages.

---

## Requirements

- Python **3.11+** (3.10 works but requires the `tomllib` backport — already in `requirements.txt`)
- An AzuraCast instance with webhook support enabled
- A publicly reachable HTTP endpoint for the webhook receiver (or a local tunnel like ngrok during development)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/youruser/azurabot.git
cd azurabot
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Configuration

Copy the example config and edit it:

```bash
cp config.toml config.local.toml
$EDITOR config.local.toml
```

### config.toml reference

```toml
[azuracast]
# Now Playing API URL used as a fallback for !np/!next when the bot has
# just started and no webhook event has been received yet.
# Leave empty to disable the fallback.
nowplaying_url = "https://your-azuracast.example.com/api/nowplaying/your_shortcode"

[webhook]
host   = "0.0.0.0"   # Interface to bind the HTTP receiver
port   = 8080         # Port AzuraCast will POST to
secret = "changeme"   # Must match the secret set in AzuraCast

[database]
path = "bot.db"       # SQLite file path (created automatically)

[owner]
nickname = "YourNick"     # Your bot-local owner nickname
password = "yourpassword" # Password for /msg bot identify <nick> <pass>

[announce]
# Available variables: {radio_name} {artist} {title} {listeners} {dj_name} {song_id}
# mIRC color codes are supported using \xNN notation — use SINGLE quotes here
# so TOML passes the backslashes through. Double quotes would cause a parse error.
format = '\x02[{radio_name}]\x02 Now Playing: \x02{artist} - {title}\x02 | \x0312{listeners} listeners\x03 | DJ: {dj_name}'

dedup_window_sec    = 60    # Ignore duplicate song_id within this window
prune_interval_sec  = 3600  # How often to prune old announce log entries
prune_older_than_sec = 300  # Delete log entries older than this

[announce.fallbacks]
dj_name = "Auto DJ"
artist  = "Unknown Artist"
title   = "Unknown Title"

[commands]
trigger      = "!"
# np_format defaults to the announce format above if not set
# next_format supports: {artist}, {title}, {text}, {radio_name}
next_format = 'Next up: \x02{artist} - {title}\x02'
```

#### Network entries

Add one `[[networks]]` block per IRC server. `auth_method` must be one of: `sasl`, `nickserv`, `on_connect`, `none`.

```toml
# SASL PLAIN (e.g. Libera, OFTC)
[[networks]]
name      = "Libera"
host      = "irc.libera.chat"
port      = 6697
tls       = true
nick      = "mybot"
auth_method = "sasl"
sasl_user   = "mybot"
sasl_pass   = "secret"

# NickServ IDENTIFY (older networks)
[[networks]]
name      = "SomeNet"
host      = "irc.somenet.org"
port      = 6697
tls       = true
nick      = "mybot"
auth_method  = "nickserv"
nickserv_pass = "secret"

# Custom on-connect commands (e.g. DALnet)
[[networks]]
name      = "DALnet"
host      = "irc.dal.net"
port      = 6667
tls       = false
nick      = "mybot"
auth_method       = "on_connect"
on_connect_delay_sec = 2
on_connect_commands  = [
  "PRIVMSG NickServ@services.dal.net :IDENTIFY secret",
  "MODE mybot +x"
]

# No auth
[[networks]]
name = "OpenNet"
host = "irc.example.net"
port = 6667
tls  = false
nick = "mybot"
auth_method = "none"
```

---

## AzuraCast webhook setup

1. In your AzuraCast admin panel, go to **Station → Webhooks → Add Webhook**.
2. Choose **Generic/Custom (HTTP POST)**.
3. Set the URL to: `http://your-server:8080/webhook?secret=changeme`
4. Enable the **Song Changed** trigger.
5. Save.

---

## Running the bot

```bash
# With the default config.toml
python main.py

# With a custom config path
python main.py config.local.toml
```

The database path in `[database]` is resolved relative to the config file's location, so `bot.db` always lives next to your config regardless of what directory you launch from.

To run as a background service, use `systemd`, `supervisord`, or `screen`/`tmux`.

### Example systemd unit

```ini
[Unit]
Description=azurabot IRC radio announcer
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/azurabot
ExecStart=/opt/azurabot/.venv/bin/python main.py config.toml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Reloading config without restarting

Send `SIGHUP` to reload `config.toml` live:

```bash
kill -HUP $(pgrep -f "python main.py")
```

Networks are diffed against the running state: new ones connect, removed ones disconnect, changed ones reconnect, unchanged ones are left alone. See also the `reload` PM command below.

---

## Owner authentication

The bot uses its own password system — no dependency on NickServ being present.

From any IRC client, PM the bot:

```
/msg mybot identify YourNick yourpassword
```

Your session is tied to your `nick!user@host` mask and lives until you disconnect or explicitly log out:

```
/msg mybot logout
```

After **3 failed attempts**, your hostmask is locked out for **10 minutes**.

---

## PM commands

All commands are sent as private messages to the bot. You must be authenticated first.

### Channel management

| Command | Description |
|---|---|
| `add <#channel> [mode]` | Register a channel on the current network. Mode defaults to `live`. |
| `remove <#channel> [network]` | Unregister a channel. |
| `list` | List all registered channels across all networks. |
| `info <#channel> [network]` | Show full details for a channel. |

### Per-channel settings

| Command | Description |
|---|---|
| `set mode <#channel> <mode> [network]` | Change announce mode (`live`, `hourly`, `Xm`, `Xh`). |
| `set listeners <#channel> on\|off [network]` | Toggle listener count in announcements. |
| `set dj <#channel> on\|off [network]` | Toggle DJ name in announcements. |
| `enable <#channel> [network]` | Resume announcements to a paused channel. |
| `disable <#channel> [network]` | Pause announcements without removing the channel. |

### Announce modes

| Mode | Behaviour |
|---|---|
| `live` | Announce on every song change immediately. |
| `hourly` | Alias for `1h`. |
| `Xm` | Announce at most once every X minutes, only if the song changed. |
| `Xh` | Announce at most once every X hours, only if the song changed. |

### Testing & diagnostics

| Command | Description |
|---|---|
| `test <#channel> [network]` | Send a test announcement using the last received song (or a dummy if none yet). |
| `status` | Webhook server health, queue depth, time of last event. |
| `netstatus` | Connection status of all IRC networks. |
| `reconnect <network>` | Force-disconnect and reconnect a specific network. |
| `reload [--purge]` | Reload `config.toml` and apply network changes live. Pass `--purge` to also remove DB channels for any networks dropped from the config. |
| `help` | Show the full command list. |

### Notes on `[network]`

When omitted, the network defaults to whichever network the PM was received on. Use the explicit `[network]` argument when `#channel` exists on multiple networks, e.g.:

```
info #radio Libera
remove #radio DALnet
```

---

## Channel commands

The following commands can be used by anyone in a registered channel. Each command is suppressed per channel if the song hasn't changed since the last reply — the current song acts as the natural cooldown. The bot only responds in channels that have been registered with `add`.

| Command | Description |
|---|---|
| `!np` / `!nowplaying` | Show the currently playing song. Falls back to a live AzuraCast API poll if no webhook event has been received yet (requires `nowplaying_url` to be set). |
| `!next` | Show the next queued song. Same fallback behaviour as `!np`. |

---

## Project structure

```
azurabot/
├── main.py                 Entry point, signal handling, reload orchestration
├── config.py               TOML loader + dataclasses
├── db.py                   SQLite schema + async helpers
├── webhook.py              aiohttp receiver, parses AzuraCast payload, API fallback fetch
├── dispatcher.py           Queue consumer, announce mode logic, dedup
├── announcer.py            Format string renderer (mIRC colors)
├── irc/
│   ├── client.py           Pure asyncio IRC client per network, auth, reconnect
│   └── manager.py          Manages all clients, live network diffing on reload
├── commands/
│   ├── pm_commands.py      Owner PM command parser + handlers
│   └── channel_commands.py Public channel commands (!np, !next)
├── requirements.txt
├── config.toml             Example config (copy and edit)
└── README.md
```

---

## Security notes

- The webhook `secret` prevents unauthorized parties from triggering announcements. Always set it and keep it out of version control.
- The owner `password` is stored in `config.toml` in plaintext — restrict file permissions (`chmod 600 config.toml`).
- Bot sessions are in-memory and reset on restart — you will need to re-identify after a bot restart.
- `config.toml` should **not** be committed to a public repository. Add it to `.gitignore`.

---

## License

MIT

An IRC bot that announces now-playing information from an [AzuraCast](https://www.azuracast.com/) radio station via webhooks. Supports multiple IRC networks simultaneously, per-channel announce modes, and full management via IRC private messages.

---
