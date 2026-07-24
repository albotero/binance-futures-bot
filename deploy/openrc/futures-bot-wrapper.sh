#!/bin/sh
# Wrapper script for Binance Futures Bot
# This sets up the environment before launching the bot

# Source the config file
if [ -f /etc/conf.d/futures-bot ]; then
    . /etc/conf.d/futures-bot
fi

# Set defaults if not provided
: ${BOT_DIR:="/home/binance-bot/github/binance-futures-bot"}
: ${BOT_VENV:="${BOT_DIR}/.venv"}
: ${BOT_MODE:="run-web"}

# Set up environment
export PYTHONPATH="${BOT_DIR}/src"

# Change to bot directory
cd "${BOT_DIR}" || exit 1

# Execute the bot
exec "${BOT_VENV}/bin/python" -m futures_bot.main ${BOT_MODE}
