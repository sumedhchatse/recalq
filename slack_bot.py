#!/usr/bin/env python3
"""
slack_bot.py — opt-in connector slot for chatting with the Recalq engine
from Slack. NOT YET IMPLEMENTED — this is a scaffold: the config gate and
setup notes exist so enabling Slack is a documented, deliberate choice
(same "opt-in, off unless configured" shape as Telegram/OpenRouter/
architect mode elsewhere in this project), not a decision made for you.

Why it isn't built yet: Slack needs Socket Mode (a WebSocket connection
the bot opens outward, so no public HTTPS endpoint is needed — the closest
fit to Telegram's long-polling model) via the official `slack_sdk` package,
which is a new dependency this project doesn't currently have. That's a
real tradeoff worth a deliberate yes, not something to add silently.

To actually build this connector: follow telegram_bot.py's pattern exactly
(thin adapter onto memlayer.ask()/agent.run(), same /agent, /cd, /reset,
/model commands, same per-chat + project_namespace() isolation) — swap its
urllib long-poll loop for a slack_sdk SocketModeClient event loop.

Setup (once implemented):
  1. Create a Slack app at api.slack.com/apps, enable Socket Mode.
  2. Install it to your workspace, get a Bot Token (xoxb-...) and an
     App-Level Token (xapp-...) with the connections:write scope.
  3. Add to .env:
       SLACK_BOT_TOKEN=xoxb-...
       SLACK_APP_TOKEN=xapp-...
       SLACK_ALLOWED_USERS=<user id>[,<other id>...]
  4. pip install slack_sdk
  5. Run: python3 slack_bot.py

SLACK_ALLOWED_USERS would be a hard allowlist — default-deny, same
reasoning as Telegram's: LLM calls cost money and an open bot is a blank
check.
"""
import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")


def main():
    if not BOT_TOKEN or not APP_TOKEN:
        sys.exit("Slack connector not configured — set SLACK_BOT_TOKEN and "
                  "SLACK_APP_TOKEN in .env to opt in. See this file's docstring "
                  "for setup steps. (It's also not implemented yet — see the "
                  "docstring for what's needed to build it.)")
    sys.exit("SLACK_BOT_TOKEN/SLACK_APP_TOKEN are set, but slack_bot.py itself "
              "isn't implemented yet — this is a scaffold, not a working "
              "connector. See this file's docstring for what to build "
              "(follow telegram_bot.py's pattern) and the slack_sdk dependency "
              "it needs.")


if __name__ == "__main__":
    main()
