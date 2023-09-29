import os
import time
import pytz
import discord
import logging
import feedparser
import aiohttp
import asyncio
import typing
from time import mktime
from typing import List
from pytz import timezone
from datetime import datetime
from dotenv import load_dotenv
from discord import app_commands
from tinydb import TinyDB, Query
from discord.ext import tasks

# Loads the .env file that resides on the same level as the script.
load_dotenv()
# Get the API token from the .env file.
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
#GUILD_ID = os.getenv('GUILD_ID') # For testing. Speed up testing by only syncing commands to test server.

INSTANCES_API_URL = 'https://status.d420.de/api/v1/instances'
DISPLAY_DOMAIN = 'twitter.com' #'nitter.poast.org'
PUBLISHED_TIMEZONE = pytz.timezone('UTC') # Nitter RSS feed dates are in UTC timezone
REFERENCE_FEED = 'x' # Choose an account that is likely to stay up
TOKEN_SEPARATOR = '-' # Choose a value that is not part of a username (not 0-9, a-z, _)
FEED_REFRESH_INTERVAL_MINUTES = 15
ERROR_MSG = 'Oops, something went wrong!' # Generic error message

# Use logging handler
handler = logging.FileHandler(filename = 'discord.log', encoding = 'utf-8', mode = 'w')

# Load database from file
feeds_db = TinyDB('feeds.json')
instance_db = TinyDB('instance.json')
#config_db = TinyDB('config.json') # not yet implemented

# Current Nitter instance
instance_domain = ''

# Define intents
intents = discord.Intents.default()
intents.message_content = True
# Gets the client object from discord.py. Client is synonymous with bot.
bot = discord.Client(intents = intents)
# Define tree which will hold application commands
tree = app_commands.CommandTree(bot)

async def get_instance_from_database():
    global instance_domain
    if len(instance_db) == 0:
        instance_domain = ''
    else:
        instance_domain = instance_db.get(doc_id = 1)['domain']

async def get_rss_feed(name: str, session):
    async with session.get(f"https://{instance_domain}/{name}/rss") as response:
        html = await response.text()
        rss_posts = feedparser.parse(html)
        return rss_posts

async def check_feed_status(name: str, session):
    async with session.head(f'https://{instance_domain}/{name}/rss') as response:
        if response.status != 200:
            raise ValueError(f'Response status: {response.status}')

async def check_instance_status(domain: str, session):
    async with session.head(f'https://{domain}/{REFERENCE_FEED}/rss') as response:
        if response.status != 200:
            raise ValueError(f'Response status: {response.status}')

async def get_instances(session):
    async with session.get(INSTANCES_API_URL) as response:
        response_json = await response.json()
        return response_json['hosts']

async def get_fastest_instance():
    min_response_time = -1
    fastest_domain = ''
    async with aiohttp.ClientSession() as session:
        hosts = await get_instances(session)
        for index in range(len(hosts)):
            if hosts[index]['healthy'] and hosts[index]['rss'] and (min_response_time == -1 or hosts[index]['ping_avg'] < min_response_time):
                print(f"Found lower response time: {hosts[index]['domain']} with time of {hosts[index]['ping_avg']}")
                try:
                    await check_instance_status(hosts[index]['domain'], session)
                    min_response_time = hosts[index]['ping_avg']
                    fastest_domain = hosts[index]['domain']
                except Exception as e:
                    print(f'Error: {e}')
    #print(f"Fastest instance: {fastest_domain}, response time: {min_response_time}")
    return fastest_domain

# Get working instance with RSS - on first load when database empty and when fetching feed fails
async def update_instance(session):
    global instance_domain
    try:
        await check_instance_status(instance_domain, session)
    except Exception as e:
        print(f'Error: {e}')
        instance_domain = await get_fastest_instance()
        if len(instance_db) == 0:
            instance_db.insert({'domain':instance_domain})
        else:
            instance_db.update({'domain':instance_domain}, doc_ids = [1])
            print(f"Updated instance in database to: {instance_db.all()[0]['domain']}")
        print(f"Current instance changed to: {instance_domain}")

async def feeds_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    choices = []
    for entry in feeds_db.search(Query().guild_id == interaction.guild.id):
        display_name = f"Feed: {entry['name']}, Channel: {bot.get_channel(entry['channel_id'])}"
        choice_value = f"{entry['name']}{TOKEN_SEPARATOR}{entry['channel_id']}"
        choices.append(app_commands.Choice(name = display_name, value = choice_value))
    return choices

async def get_timestamp_from_struct(time: time.struct_time):
    original_datetime = datetime.fromtimestamp(mktime(time))
    localized_datetime = PUBLISHED_TIMEZONE.localize(original_datetime)
    timestamp = int(datetime.timestamp(localized_datetime))
    return timestamp

async def get_display_timestamp(time: time.struct_time):
    timestamp = await get_timestamp_from_struct(time)
    return f'<t:{timestamp:.0f}:f>'

async def output_error_feed_not_found(name: str):
    return f'{ERROR_MSG} The feed \'{name}\' could not be found.'

async def get_display_link(original_link: str):
    return original_link.replace(instance_domain, DISPLAY_DOMAIN)

async def get_feed_data_from_identifier(identifier: str):
    tokens = identifier.split(TOKEN_SEPARATOR)
    return feeds_db.get((Query()['name'] == tokens[0]) & (Query()['channel_id'] == int(tokens[1])))

# Stores latest posts and updates last checked timestamp for feed
async def get_latest_posts(feed_data, rss_feed, session):
    posts = []
    if feed_data['last_checked'] == -1:
        posts.append(rss_feed.entries[0])
    else:
        for post in rss_feed.entries:
            timestamp = await get_timestamp_from_struct(post.published_parsed)
            if timestamp > feed_data['last_checked']:
                posts.insert(0, post)
            else:
                break
    current_timestamp = int(datetime.timestamp(datetime.now()))
    feeds_db.update({'last_checked': current_timestamp}, (Query()['name'] == feed_data['name']) & (Query()['channel_id'] == feed_data['channel_id']))
    return posts

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print('Getting last used instance...')
    await get_instance_from_database()
    print('Syncing slash commands...')
    for guild in bot.guilds:
        await tree.sync(guild = discord.Object(id = guild.id)) #guild = discord.Object(id = GUILD_ID))
    print('Updating current instance...')
    async with aiohttp.ClientSession() as session:
        await update_instance(session)
    print('Starting auto-feed updates...')
    auto_update_feeds.start()
    print('Ready')

# Ping command
@tree.command(name = 'ping', description = 'Test latency') #, guild = discord.Object(GUILD_ID))
async def ping_command(interaction: discord.Interaction):    
    try:
        await interaction.response.send_message(f'Pong! ({bot.latency*1000:.0f}ms)')
    except Exception as e:
        print(f'Error: {e}')
        await interaction.response.send_message(f'{ERROR_MSG}')

# Get current instances
@tree.command(name = 'get-instance', description = 'Get the current instance being used') #, guild = discord.Object(GUILD_ID))
async def get_curr_instances(interaction: discord.Interaction):
    try:
        if instance_domain == '':
            message = 'Current instance: none'
        else:
            message = f'Current instance: <https://{instance_domain}>'
        await interaction.response.send_message(message)
    except Exception as e:
        print(f'Error: {e}')
        await interaction.response.send_message(f'{ERROR_MSG}')

# Add feed command
@tree.command(name = 'add-feed', description = 'Add a user feed') #, guild = discord.Object(GUILD_ID))
@app_commands.describe(name = 'Name of feed to add (without the @ symbol)')
@app_commands.describe(channel = 'Channel to send feed messages in')
async def add_feed(interaction: discord.Interaction, name: str, channel: discord.TextChannel):    
    selected_channel = discord.utils.get(interaction.guild.channels, name = str(channel))
    # Check for duplicates
    if feeds_db.count(Query()['name'] == name) and feeds_db.count(Query()['channel_id'] == selected_channel.id):
        await interaction.response.send_message(f'The feed @{name} has already been added to <#{selected_channel.id}>.')
        return
    # Check if account/link is valid
    try:
        await interaction.response.defer(thinking = True)
        async with aiohttp.ClientSession() as session:
            await update_instance(session)
            #print(f'Adding feed using domain: {instance_domain}')
            rss = await check_feed_status(name, session)
            await interaction.followup.send(f'New posts from user **@{name.lower()}** will be sent in <#{selected_channel.id}>.')
            #timestamp = await get_timestamp_from_datetime(datetime.now())
            feeds_db.insert({'guild_id':interaction.guild.id, 'name':name.lower(), 'channel_id':selected_channel.id, 'last_checked':-1, 'enabled':True})
    except Exception as e:
        print(f'Error: {e}')
        #error_msg = await output_error_feed_not_found(name)
        await interaction.followup.send(f'{ERROR_MSG} The feed \'{name}\' could not be added.')

# Remove feed command
@tree.command(name = 'remove-feed', description = 'Remove a user feed') #, guild = discord.Object(GUILD_ID))
@app_commands.describe(identifier = 'Name of feed to remove (without the @ symbol)')
@app_commands.rename(identifier = 'feed')
@app_commands.autocomplete(identifier = feeds_autocomplete)
async def remove_feed(interaction: discord.Interaction, identifier: str):    
    try:
        feed_data = await get_feed_data_from_identifier(identifier)
        feeds_db.remove((Query()['name'] == feed_data['name']) & (Query()['channel_id'] == feed_data['channel_id']))
        await interaction.response.send_message(f"The feed **@{feed_data['name']}** in <#{feed_data['channel_id']}> has been removed from the list of feeds.")
    except Exception as e:
        print(f'Error: {e}')
        error_msg = await output_error_feed_not_found(name)
        await interaction.followup.send(f'{error_msg}')

# Change channel command
@tree.command(name = 'change-channel', description = 'Change the channel posts for a feed are sent to') #, guild = discord.Object(GUILD_ID))
@app_commands.describe(identifier = 'Feed to change channel (without the @ symbol)')
@app_commands.describe(channel = 'New channel for feed (without the @ symbol)')
@app_commands.rename(identifier = 'feed')
@app_commands.autocomplete(identifier = feeds_autocomplete)
async def change_channel(interaction: discord.Interaction, identifier: str, channel: discord.TextChannel):
    try:
        print(identifier)
        feed_data = await get_feed_data_from_identifier(identifier)
        print(feed_data)
        selected_channel = discord.utils.get(interaction.guild.channels, name = str(channel))
        if feeds_db.count((Query()['name'] == feed_data['name']) & (Query()['channel_id'] == selected_channel.id)) > 0:
            await interaction.response.send_message(f"The feed **@{feed_data['name']}** is already in <#{selected_channel.id}>.")
            return
        feeds_db.update({'channel_id':selected_channel.id}, (Query()['name'] == feed_data['name']) & (Query()['channel_id'] == feed_data['channel_id']))
        await interaction.response.send_message(f"New posts from user **@{feed_data['name']}** will now be sent in <#{selected_channel.id}>.")
    except Exception as e:
        print(f'Error: {e}')
        error_msg = await output_error_feed_not_found(name)
        await interaction.followup.send(f'{error_msg}')

# Get feeds command
@tree.command(name = 'list-feeds', description = 'List all added user feeds') #, guild = discord.Object(GUILD_ID))
async def get_feeds(interaction: discord.Interaction):    
    message = ''
    try:
        feeds = feeds_db.search(Query().guild_id == interaction.guild.id)
        if feeds == []:
            message = 'There are no added feeds.'
        else:
            message = 'List of feeds:'
            for index in range(len(feeds)):
                message += f"\nFeed: **@{feeds[index]['name']}**, Channel: <#{feeds[index]['channel_id']}>"
        await interaction.response.send_message(message)
    except Exception as e:
        print(f'Error: {e}')
        await interaction.response.send_message(f'{ERROR_MSG}')

# Enable feed
@tree.command(name = 'enable-feed', description = 'Enable a feed if it has been disabled') #, guild = discord.Object(GUILD_ID))
@app_commands.describe(identifier = 'Feed to enable')
@app_commands.rename(identifier = 'feed')
@app_commands.autocomplete(identifier = feeds_autocomplete)
async def enable_feed(interaction: discord.Interaction, identifier: str):
    try:
        await interaction.response.defer(thinking = True)
        feed_data = await get_feed_data_from_identifier(identifier)
        if feed_data['enabled'] == True:
            await interaction.followup.send(f"The feed @{feed_data['name']} is already enabled.")
            return
        feeds_db.update({'enabled': True}, (Query()['name'] == feed_data['name']) & (Query()['channel_id'] == feed_data['channel_id']))
        await interaction.followup.send(f"The feed has been enabled. New posts from **@{feed_data['name']}** will now be sent in <#{feed_data['channel_id']}>.")
    except Exception as e:
        print(f'Error: {e}')
        await interaction.followup.send(f'{ERROR_MSG}')

# Disable feed
@tree.command(name = 'disable-feed', description = 'Disable an active feed') #, guild = discord.Object(GUILD_ID))
@app_commands.describe(identifier = 'Feed to disable')
@app_commands.rename(identifier = 'feed')
@app_commands.autocomplete(identifier = feeds_autocomplete)
async def disable_feed(interaction: discord.Interaction, identifier: str):
    try:
        await interaction.response.defer(thinking = True)
        feed_data = await get_feed_data_from_identifier(identifier)
        if feed_data['enabled'] == False:
            await interaction.followup.send(f"The feed @{feed_data['name']} is already disabled.")
            return
        feeds_db.update({'enabled': False}, (Query()['name'] == feed_data['name']) & (Query()['channel_id'] == feed_data['channel_id']))
        await interaction.followup.send(f"The feed has been disabled. New posts from **@{feed_data['name']}** will no longer be sent in <#{feed_data['channel_id']}>.")
    except Exception as e:
        print(f'Error: {e}')
        await interaction.followup.send(f'{ERROR_MSG}')

# Reload feed
# Nitter feeds don't use ETag or last modified
# Compare using Unix timestamp
@tree.command(name = 'update-feed', description = 'Refresh a feed and check for new posts') #, guild = discord.Object(GUILD_ID))
@app_commands.describe(identifier = 'Feed to update')
@app_commands.autocomplete(identifier = feeds_autocomplete)
@app_commands.rename(identifier = 'feed')
async def update_feed(interaction: discord.Interaction, identifier: str):
    try:
        await interaction.response.defer(thinking = True)
        feed_data = await get_feed_data_from_identifier(identifier)
        if feed_data['enabled'] == False:
            await interaction.followup.send(f"The feed @{feed_data['name']} is currently disabled. To reload, enable the feed first.")
            return
        channel = bot.get_channel(feed_data['channel_id'])
        async with aiohttp.ClientSession() as session:
            await update_instance(session)
            rss = await get_rss_feed(feed_data['name'], session)
            posts = await get_latest_posts(feed_data, rss, session)
            for post in posts:
                timestamp = await get_display_timestamp(post.published_parsed)
                link = await get_display_link(post.link)
                await channel.send(f'**{rss.feed.title}** ({timestamp}):\n{link}')
        message = f"Updated feed **@{feed_data['name']}** in <#{channel.id}>."
        if posts == []:
            message += ' No new posts since the last update.'
        elif len(posts) == 1:
            message += f" There is {len(posts)} new posts since the last update."
        else:
            message += f" There are {len(posts)} new posts since the last update."
        await interaction.followup.send(message)
    except Exception as e:
        print(f'Error: {e}')
        await interaction.followup.send(f'{ERROR_MSG} The feed could not be reloaded.')

# Last post command
# Change to suppress embed and use own embed?
@tree.command(name = 'last-post', description = 'Get most recent post for a feed') #, guild = discord.Object(GUILD_ID))
@app_commands.describe(feed = 'Name of feed')
async def get_last_post(interaction: discord.Interaction, feed: str):
    try:
        await interaction.response.defer(thinking = True)
        async with aiohttp.ClientSession() as session:
            await update_instance(session)
            rss = await get_rss_feed(feed, session)
            post = rss.entries[0]
            timestamp = await get_display_timestamp(post.published_parsed)
            link = await get_display_link(post.link)
            await interaction.followup.send(f'Latest post from **{rss.feed.title}** ({timestamp}):\n{link}')
    except Exception as e:
        print(f'Error: {e}')
        error_msg = await output_error_feed_not_found(feed)
        await interaction.followup.send(f'{error_msg}')

# Manually update all feeds
@tree.command(name = 'update-all-feeds', description = 'Refresh all feeds in the server') #, guild = discord.Object(GUILD_ID))
async def manually_update_all_feeds(interaction: discord.Interaction):
    try:
        message = ''
        successful_updates = []
        failed_updates = []
        await interaction.response.defer(thinking=True)
        async with aiohttp.ClientSession() as session:
            await update_instance(session)
            feeds = feeds_db.search(Query().guild_id == interaction.guild.id)
            if feeds == []:
                message = 'There are no feeds to update! Add a feed to get started.'
            else:
                for index in range(len(feeds)):
                    try:
                        if feeds[index]['enabled'] == False:
                            continue
                        channel = bot.get_channel(feeds[index]['channel_id'])
                        rss = await get_rss_feed(feeds[index]['name'], session)
                        posts = await get_latest_posts(feeds[index], rss, session)
                        number_of_posts = 0
                        for post in posts:
                            timestamp = await get_display_timestamp(post.published_parsed)
                            link = await get_display_link(post.link)
                            await channel.send(f'**{rss.feed.title}** ({timestamp}):\n{link}')
                            number_of_posts += 1
                        successful_updates.append({'name': feeds[index]['name'], 'channel_id': feeds[index]['channel_id'], 'posts': number_of_posts})
                    except Exception as e:
                        print(f'Error: {e}')
                        failed_updates.append({'name': feeds[index]['name'], 'channel_id': feeds[index]['channel_id']})
        # Print summary
        if successful_updates != []:
            message += ':green_circle: Successfully updated feeds:'
            for item in successful_updates:
                message += f"\nFeed: **@{item['name']}**, Channel: <#{item['channel_id']}> ({item['posts']} new posts)"
        if successful_updates != [] and failed_updates != []:
            message += '\n\n'
        if failed_updates != []:
            message += ':o: Failed to update feeds:'
            for item in failed_updates:
                message += f"\nFeed: **@{item['name']}**, Channel: <#{item['channel_id']}>"
        await interaction.followup.send(message)
    except Exception as e:
        print(f'Error: {e}')
        await interaction.followup.send(f'{ERROR_MSG}')

# Automatically update all feeds
# Nitter RSS feed updates at interval of 15 minutes (900 seconds)
@tasks.loop(minutes = FEED_REFRESH_INTERVAL_MINUTES)
async def auto_update_feeds():
    try:
        #failed_updates = []
        #message = ''
        async with aiohttp.ClientSession() as session:
            await update_instance(session)
            for guild in bot.guilds:
                feeds = feeds_db.search(Query().guild_id == guild.id)
                for index in range(len(feeds)):
                    try:
                        if feeds[index]['enabled'] == False or int(datetime.timestamp(datetime.now())) - feeds[index]['last_checked'] < (FEED_REFRESH_INTERVAL_MINUTES - 1) * 60:
                            print(f"Skipping {feeds[index]['name']} in {guild.name} at {time.ctime()}")
                            continue
                        print(f"Checking {feeds[index]['name']} in {guild.name} at {time.ctime()}")
                        channel = bot.get_channel(feeds[index]['channel_id'])
                        rss = await get_rss_feed(feeds[index]['name'], session)
                        posts = await get_latest_posts(feeds[index], rss, session)
                        for post in posts:
                            timestamp = await get_display_timestamp(post.published_parsed)
                            link = await get_display_link(post.link)
                            await channel.send(f'**{rss.feed.title}** ({timestamp}):\n{link}')
                    except Exception as e:
                        print(f'Error: {e}')
                        #failed_updates.append({'name': feeds[index]['name'], 'channel_id': feeds[index]['channel_id']})
                        #if failed_updates != []:
                        #    message += ':o: Failed to update feeds:'
                        #    for item in failed_updates:
                        #        message += f"\nFeed: **@{item['name']}**, Channel: <#{item['channel_id']}>"
    except Exception as e:
        print(f'Error: {e}')

# Executes the bot with the specified token.
bot.run(DISCORD_TOKEN, log_handler = handler)
