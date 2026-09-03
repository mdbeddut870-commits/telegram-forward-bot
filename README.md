# Telegram Auto-Forward Bot

Automatically forward messages from any channel/group to unlimited destination channels/groups. Supports message filtering, keyword matching, caption control, and pause/resume.

## Features

- **Unlimited mappings** - Add as many source to destination pairs as needed
- **Message filters** - Forward only text, photo, video, document, audio, sticker, etc.
- **Keyword filtering** - Forward only messages containing specific words
- **Caption control** - Add custom captions or strip original ones
- **Pause/Resume** - Toggle any mapping on/off without deleting
- **Multi-destination** - One source can forward to many destinations
- **Grouped albums** - Multiple photos/videos are forwarded as one album in source order
- **Admin protection** - Only authorized users can control the bot
- **SQLite storage** - Settings persist across restarts

## Architecture

```
bot.py          Entry point - runs dual clients concurrently
config.py       Loads .env configuration
database.py     SQLite persistence (mappings, filters, state)
forwarder.py    Core forwarding engine (runs on USER client)
handlers.py     Bot command handlers (runs on BOT client)
```

**Why dual-client?** Telegram bots have Privacy Mode enabled by default - they cannot see channel posts or non-command group messages. The solution: a **User client** (your Telegram account) listens to all source chats, while a **Bot client** (@YourBot) handles admin commands.

## Setup

### 1. Get credentials

| What | Where |
|---|---|
| `API_ID` + `API_HASH` | [my.telegram.org/apps](https://my.telegram.org/apps) |
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) - send `/newbot` |
| `PHONE` | Your Telegram phone number with country code |
| `ADMIN_IDS` | Your user ID - message [@userinfobot](https://t.me/userinfobot) |
| `USER_STRING_SESSION` | Optional production session string; use this instead of interactive phone login |

### 2. Install

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

### 3. Run

```bash
python bot.py
```

First local run will ask for your phone number and a verification code (OTP from Telegram). Sessions are saved locally for future runs. For Railway or another non-interactive host, set `USER_STRING_SESSION` to a Telethon StringSession and do not commit any `.session` files.

## Bot Commands

Send these to your bot in Telegram private chat:

| Command | Description | Example |
|---|---|---|
| `/start` | Show welcome message | `/start` |
| `/add <src> <dst>` | Create forwarding mapping | `/add -1001111 -1002222` |
| `/remove <id>` | Delete a mapping | `/remove 3` |
| `/toggle <id>` | Pause or resume mapping | `/toggle 3` |
| `/list` | Show all mappings | `/list` |
| `/filter <id> <type> [keywords]` | Set message filter | `/filter 3 photo` |
| `/caption <id> [strip] <text>` | Set caption | `/caption 3 My Footer` |
| `/stats` | Show statistics | `/stats` |

### Filter types

`all` `text` `photo` `video` `document` `audio` `sticker` `location` `contact` `poll`

### Examples

```
# Forward all messages from Channel A to Channel B
/add -1001234567890 -1009876543210

# Only forward photos
/filter 1 photo

# Only forward messages containing "crypto" or "airdrop"
/filter 1 all crypto,airdrop

# Add footer to forwarded messages
/caption 1 Forwarded from My Channel

# Replace original caption entirely
/caption 1 strip My Custom Caption
```

## Finding Channel/Group IDs

Forward a message from the channel/group to [@userinfobot](https://t.me/userinfobot) or [@getidsbot](https://t.me/getidsbot). The ID looks like `-1001234567890`.

## Deployment

### Railway (recommended)

1. Push to GitHub
2. [railway.app](https://railway.app) -> New Project -> Deploy from GitHub
3. Add env vars in Railway dashboard

### Render

1. Push to GitHub
2. [render.com](https://render.com) -> New Background Worker
3. Connect repo, set env vars

### Fly.io

```bash
flyctl launch --name my-forward-bot
flyctl secrets set API_ID=... API_HASH=... BOT_TOKEN=... PHONE=... ADMIN_IDS=...
flyctl deploy
```

### VPS / Local

```bash
nohup python bot.py &
```

## Important Notes

1. **Your Telegram account** must be a member/admin in source channels/groups
2. **Your account** must also be in destination chats (forwarding uses your account)
3. For channels, your account needs admin with "Post Messages" permission
4. Never share your `.env` file - it contains your phone number and API credentials

## License

MIT
