# IPO Allotment Status Bot

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/AbGisHere/IPO-Allotment-Status-Bot/releases/tag/v1.0.0)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-26A5E4.svg?logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](#setup)

A Telegram bot that tells you as soon as an Indian IPO's allotment goes live, and whether you got shares.

- 📅 **`/upcoming`**: shows the IPO calendar, with each IPO's allotment (BoA) date and registrar
- 👀 **`/select TONBO`**: works out the registrar and alerts you as soon as allotment goes live
- 🎉 **Auto status check**: when an allotment goes live, it checks your saved PANs and says whether you got shares
- 🔍 **`/status TONBO`**: checks allotment for all your saved PANs whenever you ask

Supported registrars:

| Registrar | Site |
|---|---|
| MUFG Intime (formerly Link Intime) | https://in.mpms.mufg.com/Initial_Offer/public-issues.html |
| KFintech | https://ipostatus.kfintech.com |

## Requirements

- Python 3.9+, standard library only, so there's nothing to `pip install`
- `openssl` on your `PATH` (built into macOS and most Linux distros)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
git clone https://github.com/AbGisHere/IPO-Allotment-Status-Bot.git
cd IPO-Allotment-Status-Bot
cp .env.example .env        # then paste your bot token into .env
```

To run it in the foreground:

```bash
python3 bot.py
```

To run it in the background on macOS (it auto-starts on login and restarts if it crashes):

```bash
./install_launchd.sh
# logs:  tail -f bot.log
# stop:  launchctl bootout gui/$(id -u)/com.ipoallotmentbot
```

Then open your bot in Telegram and send `/start`. **The first chat to do this becomes the bot's only owner**, and it ignores everyone else.

## Commands

| Command | What it does |
|---|---|
| `/upcoming` | Upcoming, open and closed IPOs, with BoA date and registrar |
| `/select <name>` | Watch an IPO. Matching is fuzzy, so `tonbo`, `TONBO` and `Tonbo Imaging` all work. |
| `/unselect <name>` | Stop watching an IPO |
| `/list` | Watched IPOs and whether each one is out yet |
| `/pan <PAN> [label]` | Save a PAN. You can save several, e.g. one per family member. |
| `/pan` | List saved PANs (masked) |
| `/pan remove <PAN or label>` | Delete a saved PAN |
| `/status [name]` | Allotment result for every saved PAN |
| `/check` | Poll the registrars right now |
| `/all on` / `/all off` | Also alert on every new allotment, not just watched ones |
| `/clear` | Delete every message in the chat. Telegram limits bots to messages from the last 48 hours. |
| `/help` | Show all commands |

`python3 bot.py --check-once` prints the calendar and registrar counts. It doesn't need Telegram.

### Example alert

```
🚨 Allotment is out!

TONBO IMAGING INDIA LIMITED
Registrar: KFintech
https://ipostatus.kfintech.com/

Your allotment
• Me (ABxxxxx34F): 🎉 Allotted 21 of 21 shares (Retail)
```

## Configuration

Set these in `.env`, or as environment variables:

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (required) | Token from @BotFather |
| `FAST_INTERVAL` | `30` | Seconds between checks once a watched IPO's BoA date has arrived |
| `SLOW_INTERVAL` | `300` | Seconds between checks when nothing is due yet |

## How it works

- **Calendar:** IPO names, dates and statuses come from Investorgain's live IPO table. Each IPO's registrar comes from its detail page and is cached in `state.json`.
- **MUFG Intime:** the company dropdown comes from `IPO.aspx/GetDetails`. Results come from `IPO.aspx/SearchOnPan`, using the AES-encrypted token the page itself generates.
- **KFintech:** the company list is hardcoded in the site's JS bundle (`main.<hash>.js`). The bot downloads the bundle again only when the hash changes. Results come from the site's own API, with the PAN and client ID sent as headers.
- **Polling:** every 30s once a watched IPO's BoA date has arrived, and every 5 min otherwise. When an IPO first appears on a registrar, its data sometimes isn't uploaded yet. In that case the bot keeps retrying "not found" PANs for up to 3 hours.

## Project structure

```
bot.py              # the bot (single file, stdlib only)
install_launchd.sh  # macOS background service installer
.env.example        # config template
state.json          # created at runtime: watchlist, PANs, caches (gitignored)
```

## Privacy

- PANs are stored only in your local `state.json`, which is gitignored and never leaves your machine, except when the bot sends them to the registrar to look up your allotment.
- The bot deletes your `/pan` message from the chat after saving it, and masks PANs in every reply.
- The bot replies only to its owner's chat.

## Limitations

- IPOs handled by other registrars (Bigshare, Purva, Skyline, Cameo, Maashitla, and so on) appear in `/upcoming` marked ⚠️, but the bot can't monitor them yet.
- It relies on unofficial website endpoints, so if a site changes its page, that part may break.
- It only runs while the host machine is awake.

## Disclaimer

This project isn't affiliated with or endorsed by MUFG Intime, KFin Technologies, Investorgain, NSE, BSE or SEBI. It's for personal use, to check your own allotment status. Always confirm results on the registrar's official website. The software is provided as is, without warranty. See [LICENSE](LICENSE).

## License

[MIT](LICENSE) © 2026 Abhinav Gupta
