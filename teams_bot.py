#!/usr/bin/env python3
"""
teams_bot.py — opt-in connector slot for chatting with the Recalq engine
from Microsoft Teams. NOT YET IMPLEMENTED — this is a scaffold: the config
gate and setup notes exist so enabling Teams is a documented, deliberate
choice (same "opt-in, off unless configured" shape as Telegram/Slack/
OpenRouter/architect mode elsewhere in this project), not a decision made
for you.

Why it isn't built yet, and why it's a bigger task than Telegram/Slack:
Teams doesn't support a long-poll/Socket-Mode-style model at all — the Bot
Framework PUSHES messages to you over HTTP, so this connector needs an
inbound HTTPS endpoint (a real one in production, or a tunnel like ngrok
for local dev) plus an Azure Bot Service registration (App ID + password,
JWT validation on every incoming request). That's a different shape of
work than Telegram/Slack's outbound-connection model, not just "another
platform" — budget it as its own task, not a quick follow-on.

To actually build this connector: an HTTP server (stdlib http.server is
enough — no framework needed, matching this project's "no frameworks"
approach elsewhere) that validates the Bot Framework's JWT on each POST,
extracts the message, and calls into memlayer.ask()/agent.run() the same
thin-adapter way telegram_bot.py does (same /agent, /cd, /reset, /model
commands, same per-conversation + project_namespace() isolation).

Setup (once implemented):
  1. Register a bot in Azure Bot Service, get the Microsoft App ID and
     App Password.
  2. Set a messaging endpoint (your public HTTPS URL, e.g. via ngrok for
     local dev) pointing at wherever this server listens.
  3. Add to .env:
       TEAMS_APP_ID=...
       TEAMS_APP_PASSWORD=...
       TEAMS_ALLOWED_USERS=<user id>[,<other id>...]
  4. Run: python3 teams_bot.py

TEAMS_ALLOWED_USERS would be a hard allowlist — default-deny, same
reasoning as Telegram's: LLM calls cost money and an open bot is a blank
check.
"""
import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

APP_ID = os.getenv("TEAMS_APP_ID", "")
APP_PASSWORD = os.getenv("TEAMS_APP_PASSWORD", "")


def main():
    if not APP_ID or not APP_PASSWORD:
        sys.exit("Teams connector not configured — set TEAMS_APP_ID and "
                  "TEAMS_APP_PASSWORD in .env to opt in. See this file's "
                  "docstring for setup steps. (It's also not implemented "
                  "yet — see the docstring for what's needed to build it.)")
    sys.exit("TEAMS_APP_ID/TEAMS_APP_PASSWORD are set, but teams_bot.py itself "
              "isn't implemented yet — this is a scaffold, not a working "
              "connector. See this file's docstring for what to build (an "
              "HTTP endpoint + Bot Framework JWT validation, a different "
              "shape of work than Telegram/Slack's outbound-connection model).")


if __name__ == "__main__":
    main()
