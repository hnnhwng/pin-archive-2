import argparse
import configparser
import datetime
import functools
import os
import pickle
import time

import discord
from discord.ext import commands

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
        avatar = message.author.avatar_url
        pin_content = message.content
        server = message.guild.id
        # TODO: try to clean up this timestamp
        current_date = datetime.datetime.utcfromtimestamp(int(time.time()))

        emb = discord.Embed(description=pin_content,
                            color=0x7289da,
                            timestamp=current_date)

        emb.set_author(
            name=name,
            icon_url=avatar,
            url=
            f"https://discordapp.com/channels/{server}/{message.channel.id}/{message.id}"
        )

        if message.attachments:
            img_url = message.attachments[0].url
            emb.set_image(url=img_url)

        emb.set_footer(text='Sent in #{}'.format(message.channel))

        try:
            await channel.send(embed=emb)
        except discord.errors.Forbidden:
            await message.channel.send(
                f"Pin Archiver does not have permission to send messages in {channel.name}."
            )

    @commands.command()
    async def init(self, ctx, pin_channel: discord.TextChannel):
        """Initialize the bot with the given pin-archive channel."""
        self.save_config(ctx.guild, "archive_channel", pin_channel.id)
        await ctx.send(f"Set archive channel to #{pin_channel}")

    @commands.command()
    async def archive(self, ctx, message: discord.Message):
        """Archive a message.
        
        The message gets converted using discord.MessageConverter."""
        if ctx.message.channel.permissions_for(ctx.message.author).manage_messages:
            await self.archive_message(message)

    @commands.command()
    async def setreactcount(self, ctx, count: int):
        """Set the reaction count threshold."""
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
        message_id = raw_reaction.message_id
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            print(f"Message {message_id} not found")
            return
        except discord.Forbidden:
            return

        guild = channel.guild

        reaction = discord.utils.get(message.reactions, emoji='📌')
        if reaction is None:
            return

        if reaction.count >= self.get_react_count(reaction.message.guild):
            await self.maybe_unpin()
            await reaction.message.pin()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for the system pins_add message and copy the pinned message to the archive channel."""
        if message.type != discord.MessageType.pins_add:
            return
        guild = message.guild
        # TODO: is there a TOCTTOU here?
        entry = (await
                 guild.audit_logs(action=discord.AuditLogAction.message_pin,
                                  limit=1,
                                  oldest_first=False).flatten())[0]
        channel, message_id = entry.extra.channel, entry.extra.message_id
        message = await channel.fetch_message(message_id)
        await self.archive_message(message)

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(self,
                                           channel: discord.abc.GuildChannel,
                                           last_pin: datetime.datetime):
        if last_pin is None:
            return
        print(last_pin)


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
