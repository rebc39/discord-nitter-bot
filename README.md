# Discord Nitter feed bot
Locally hosted Discord bot for Nitter feeds. Partly inspired by [Axobot](https://github.com/ZRunner/Axobot). 

Uses the [Nitter Status API](https://status.d420.de/about#api) \(source code on [Github](https://github.com/0xpr03/nitter-status)\) to get a working instance of Nitter with RSS enabled. 

Feeds are stored in `feeds.json` file (generated when the script is run).

Built as proof of concept. Likely to contain bugs. Use with caution.
##  Usage
1. Download or `git clone` the repo.
2. Install required packages with `pip install -r requirements.txt`.
3. Set up a Discord bot and get the bot token. \([Guide for setting up Discord bot](https://www.writebots.com/discord-bot-token)\).
4. Rename the `.env_example` file to `.env` and add your Discord bot token to the file.
5. Run `python3 main.py`.
6. Invite the bot to your server(s), if you haven't already done so.

Required scopes: `bot` and `applications.commands`

Required bot permissions: `Send Messages` and `Use Slash Commands`

