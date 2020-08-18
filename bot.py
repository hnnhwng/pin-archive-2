import argparse
import configparser
import datetime
import functools
import os
import pickle
import time

import discord
from discord.ext import commands

import requests

import util

DEFAULT_REACTS = 7


def guild_save_config(config_path: str, guild_id: int, key: str, value):
    """Save a config value for a guild.

    - config_path: path to the config directory
    - guild_id: id as integer
    - key: string key of the config value to save
    - value: value of the config value to save
    """
    guild_id = str(guild_id)
    directory = os.path.join(config_path, guild_id)
    os.makedirs(directory, exist_ok=True)
    filename = os.path.join(directory, key)
    print(f"Saving config {config_path} {guild_id} {key} {value}")
    with open(filename, "wb+") as f:
        pickle.dump(value, f)


def guild_read_config(config_path: str, guild_id: int, key: str):
    """Read a config value for a guild."""
    guild_id = str(guild_id)
    filename = os.path.join(config_path, guild_id, key)
    try:
        with open(filename, "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return None


class MainCog(commands.Cog):
    def __init__(self, bot, config_path: str):
        self.bot = bot
        self.config_path = config_path
        self.config_cache = {}
        self.webhook_adapter = discord.RequestsWebhookAdapter()

    def read_config(self, guild: discord.Guild, key: str):
        """Read a config value for a guild at the given key."""
        try:
            return self.config_cache[key]
        except KeyError:
            value = guild_read_config(self.config_path, guild.id, key)
            self.config_cache[key] = value
            return value

    def save_config(self, guild: discord.Guild, key: str, value):
        """Save a config value for a guild with the given key-value.'

        Anything pickleable can be saved."""
        self.config_cache[key] = value
        guild_save_config(self.config_path, guild.id, key, value)

    def get_react_count(self, guild: discord.Guild):
        """Get the reaction count threshold for a given guild."""
        val = self.read_config(guild, "reaction_count")
        if val is None:
            return DEFAULT_REACTS
        return val

    @commands.Cog.listener()
    async def on_ready(self):
        print("Ready!")

    async def archive_message(self, message: discord.Message):
        """Forwards a message to the archive channel."""

        channel_id = self.read_config(message.guild, "archive_channel")
        if channel_id is None:
            await message.channel.send(
                "Bot not initialized. Use +init <pin archive channel> to initialize."
            )
            return
        channel = self.bot.get_channel(channel_id)

        name = message.author.display_name
        avatar_url = message.author.avatar_url
        server = message.guild.id
        message_url = f"https://discordapp.com/channels/{server}/{message.channel.id}/{message.id}"

        webhook = self.read_config(message.guild, "webhook_url")

        if not webhook:
            print("No webhook???")
            return

        webhook = discord.Webhook.from_url(webhook,
                                           adapter=self.webhook_adapter)

        embed = discord.Embed(title=f"📩",
                              url=message_url,
                              description=message.content,
                              timestamp=message.created_at,
                              color=0x7289da)
        embed.set_author(name=name, url=message_url, icon_url=avatar_url)
        embed.set_footer(text=f"Sent in {message.channel.name}")

        if message.embeds:
            thumbnail = message.embeds[0].thumbnail
            if thumbnail.url:
                # If the thumbnail URL appears in the message, we can directly
                # set it as the image of the embed
                if thumbnail.url and thumbnail.url in message.content:
                    embed.set_image(url=thumbnail.url)
                # Otherwise, it's not direct link to an image, so we set it as the
                # thumbnail of the embed instead
                else:
                    embed.set_thumbnail(url=thumbnail.url)

        attachments = message.attachments

        # Add links to attachments as extra fields
        for attachment in attachments:
            embed.add_field(name="🔗", value=attachment.url)

        # TODO: Set the image to one of the attachments if there are no embeds

        # Heuristic: if the embed URL is in the message content already,
        # don't create an embed
        embeds = [embed] + ([
            embed
            for embed in message.embeds if embed.url is discord.Embed.Empty
            or embed.url not in message.content
        ] or [])

        webhook_message = {
            "content": f"[Message from {name}]({message_url})",
            "wait": False,
            "embeds": embeds
        }

        webhook.send(**webhook_message)

    @commands.command()
    async def init(self, ctx, pin_channel: discord.TextChannel):
        """Initialize the bot with the given pin-archive channel."""
        if not ctx.message.channel.permissions_for(
                ctx.message.author).administrator:
            return
        guild = ctx.guild

        self.save_config(guild, "archive_channel", pin_channel.id)

        # Create webhook and save it
        old_webhook_url = self.read_config(guild, "webhook_url")
        if old_webhook_url:
            old_webhook = discord.Webhook.from_url(old_webhook_url)
            old_webhook.delete()

        webhook = await pin_channel.create_webhook(
            name="Pin Archive 2 Webhook",
            reason="+init command for pin archiver")

        self.save_config(guild, "webhook_url", webhook.url)

        await ctx.send(
            f"Set archive channel to #{pin_channel} and created webhook")

    @commands.command()
    async def archive(self, ctx, message: discord.Message):
        """Archive a message.

        The message gets converted using discord.MessageConverter."""
        if not ctx.message.channel.permissions_for(
                ctx.message.author).manage_messages:
            return

        await self.archive_message(message)

    @commands.command()
    async def setreactcount(self, ctx, count: int):
        """Set the reaction count threshold."""
        if not ctx.message.channel.permissions_for(
                ctx.message.author).manage_messages:
            return

        self.save_config(ctx.guild, "reaction_count", count)
        await ctx.send(f"Set reaction count to {count} :pushpin:")

    @commands.command()
    async def getreactcount(self, ctx):
        """Get the reaction count threshold."""
        count = self.get_react_count(ctx.guild)
        await ctx.send(f"Reaction count is {count} :pushpin:")

    async def maybe_unpin(self, channel):
        """Unpin a message from a channel if we're at the 50-message limit."""
        pins = await channel.pins()
        if len(pins) > 48:  # some leeway
            await pins[-1].unpin()

    @commands.Cog.listener()
    async def on_raw_reaction_add(
            self, raw_reaction: discord.RawReactionActionEvent):
        # TODO: Configurable emoji

        channel = self.bot.get_channel(raw_reaction.channel_id)
        guild = channel.guild
        # Skip reactions in the archive channel
        if raw_reaction.channel_id == self.read_config(guild,
                                                       "archive_channel"):
            return

        message_id = raw_reaction.message_id
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            print(f"Message {message_id} not found")
            return
        except discord.Forbidden:
            return

        reaction = discord.utils.get(message.reactions, emoji='📌')
        if reaction is None:
            return

        if reaction.count >= self.get_react_count(reaction.message.guild):
            # Expensive check that we don't want to run too often
            if message_id in [message.id for message in await channel.pins()]:
                print("Not pinning duplicate pin")
                return
            await reaction.message.pin()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for the system pins_add message and copy the pinned message to the archive channel."""
        if message.type != discord.MessageType.pins_add:
            return
        if message.channel.id == self.read_config(message.guild,
                                                  "archive_channel"):
            return

        # TODO: is there a TOCTTOU here?
        message = (await message.channel.pins())[0]
        await self.maybe_unpin(message.channel)
        await self.archive_message(message)

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(self,
                                           channel: discord.abc.GuildChannel,
                                           last_pin: datetime.datetime):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c",
                        "--config",
                        help="Config file path",
                        default="config_pin_archive.ini")

    args = parser.parse_args()

    config = configparser.ConfigParser()
    config.read(args.config)
    token = util.try_config(config, "MAIN", "Token")
    prefix = util.try_config(config, "MAIN", "Prefix")
    config_path = util.try_config(config, "MAIN", "ConfigPath")

    os.makedirs(config_path, exist_ok=True)

    bot = commands.Bot(command_prefix=prefix)
    bot.add_cog(MainCog(bot, config_path))
    bot.run(token)


if __name__ == "__main__":
    main()
