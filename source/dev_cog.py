from pathlib import Path
from subprocess import Popen, PIPE
from discord.ext import commands
import datetime
import discord
import logging
import psutil
import time

from database import delete, select
from utils import chunks

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class DevCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conn = bot.conn

    @commands.command(hidden=True)
    @commands.is_owner()
    async def load(self, ctx, ext):
        ext = ext + "_cog"
        try:
            await self.bot.load_extension(ext)
            await ctx.send(_('Extension loaded.'))
        except commands.ExtensionAlreadyLoaded:
            await self.bot.reload_extension(ext)
            await ctx.send(_('Extension reloaded.'))
        except commands.ExtensionNotFound:
            await ctx.send(_('Extension not found.'))
        except commands.ExtensionError:
            await ctx.send(_('Extension failed to load.'))

    @commands.command(hidden=True)
    @commands.is_owner()
    async def version(self, ctx, version):
        self.bot.version = version
        await self.bot.change_presence(activity=discord.Game(name=version))

    @commands.group(hidden=True)
    @commands.is_owner()
    async def git(self, ctx):
        ctx.git_cmd = ['git']

    @git.command()
    async def pull(self, ctx):
        ctx.git_cmd.append('pull')
        p = Popen(ctx.git_cmd, stdout=PIPE)
        await ctx.send(p.stdout.read().decode("utf-8"))

    @commands.command(hidden=True)
    @commands.is_owner()
    async def stats(self, ctx):
        """Shows stats about the bot"""
        guild_count = len(self.bot.guilds)
        member_count = sum([guild.member_count for guild in self.bot.guilds])

        available_memory = psutil.virtual_memory().available
        bot_process = psutil.Process()
        process_memory = bot_process.memory_info().vms
        cpu = bot_process.cpu_times()
        cpu_time = cpu.system + cpu.user
        cpu_time = str(datetime.timedelta(seconds=cpu_time))

        data_sizes = {
            'B': 1,
            'KB': 1024,
            'MB': 1048576,
            'GB': 1073741824
        }
        for size, value in data_sizes.items():
            if (process_memory / value) > 1 < 1024:
                process_memory_str = "{} {}".format(
                    round(process_memory / value, 2), size)
            if (available_memory / value) > 1 < 1024:
                available_memory_str = "{} {}".format(
                    round(available_memory / value, 2), size)

        title = "Bot stats"
        about = [
            _("Resource usage:"),
            _("**CPU time:** {0}").format(cpu_time),
            _("**Memory:** {0}/{1}\n").format(process_memory_str, available_memory_str),
            _("**Servers:** {0}").format(guild_count),
            _("**Members:** {0}").format(member_count)
        ]
        content = "\n".join(about)
        embed = discord.Embed(title=title, colour=discord.Colour(0x3498db), description=content)
        await ctx.send(embed=embed)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def list(self, ctx):
        await ctx.send(_("**We are in the following {0} guilds:**\n").format(len(self.bot.guilds)))
        for chunk in chunks(self.bot.guilds, 40):
            msg = "\n".join("{0} ({1})".format(guild.name, guild.id) for guild in chunk)
            await ctx.send(msg)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def cleanup(self, ctx):
        res = select(self.conn, 'Settings', ['guild_id', 'last_command'])
        current_time = datetime.datetime.now().timestamp()
        cutoff = 3600 * 24 * 90
        cutoff_time = current_time - cutoff
        active = 0
        inactive = 0
        deleted = 0
        for row in res:
            guild_id = row[0]
            last_command = row[1]
            guild = self.bot.get_guild(guild_id)
            if guild:
                if last_command and last_command > cutoff_time:
                    active += 1
                else:
                    inactive += 1
                    logger.info("Leaving guild {0}...".format(guild.name))
                    await guild.leave()
                    ## Don't immediately delete settings in case they rejoin.
            else:
                logger.info("We are no longer in {0}".format(guild_id))
                delete(self.conn, 'Settings', ['guild_id'], [guild_id])
                deleted += 1
        self.conn.commit()
        logger.info("Active guild count: {0}".format(active))
        logger.info("Inactive guild count: {0}".format(inactive))
        logger.info("Deleted guild count: {0}".format(deleted))
        await ctx.send("Active guild count: {0}".format(active))
        await ctx.send("Inactive guild count: {0}".format(inactive))
        await ctx.send("Deleted guild count: {0}".format(deleted))

    async def _sync_guild_commands(self, guild):
        self.bot.tree.copy_global_to(guild=guild)
        return await self.bot.tree.sync(guild=guild)

    async def _clear_discord_global_commands(self):
        """Remove global slash commands so they do not duplicate guild copies."""
        if self.bot.application_id is None:
            return
        await self.bot.http.bulk_upsert_global_commands(self.bot.application_id, payload=[])

    async def push_slash_commands(self):
        """Publish slash commands as guild copies only (instant, no global duplicates)."""
        for guild in self.bot.guilds:
            guild_cmds = await self._sync_guild_commands(guild)
            logger.info('Startup slash sync: %s commands in guild %s (%s).', len(guild_cmds), guild.name, guild.id)
        await self._clear_discord_global_commands()
        logger.info('Startup slash sync: cleared global command registrations.')

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        try:
            guild_cmds = await self._sync_guild_commands(guild)
            logger.info('Guild join slash sync: %s commands in %s (%s).', len(guild_cmds), guild.name, guild.id)
        except discord.HTTPException:
            logger.exception('Guild join slash sync failed for %s.', guild.id)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def sync(self, ctx):
        try:
            counts = []
            for guild in self.bot.guilds:
                guild_cmds = await self._sync_guild_commands(guild)
                counts.append(f'**{guild.name}:** {len(guild_cmds)} command(s)')
            await self._clear_discord_global_commands()
        except discord.HTTPException as exc:
            logger.exception('Slash command sync failed.')
            await ctx.send(f'Sync failed: {exc}')
            return
        lines = counts or ['No guilds to sync.']
        lines.append('Removed global duplicates — each command should appear once per server.')
        logger.info('Synced slash commands for %s guild(s).', len(counts))
        await ctx.send('Synced bot commands.\n' + '\n'.join(lines))

    async def replace_spec_emojis(self, role_names, emoji_dir):
        """Replace single-spec application emojis from `{Class}_{Color}.webp` files."""
        existing = {}
        for e in await self.bot.fetch_application_emojis():
            existing.setdefault(e.name, []).append(e)
        files = {p.stem.replace('-', '').lower(): p for p in Path(emoji_dir).glob('*.webp')}
        replaced, failed = [], []
        for role in role_names:
            for color in ('Red', 'Blue', 'Yellow'):
                name = f'{role}_{color}'
                path = files.get(name.replace('-', '').lower())
                if path is None:
                    continue
                try:
                    for old in existing.get(name, ()):
                        await old.delete()
                    await self.bot.create_application_emoji(name=name, image=path.read_bytes())
                    replaced.append(name)
                except discord.HTTPException as exc:
                    logger.warning('Could not replace emoji %s: %s', name, exc)
                    failed.append(name)
        return replaced, failed

    @commands.command(hidden=True)
    @commands.is_owner()
    async def spec_emojis(self, ctx):
        raids = self.bot.get_cog('RaidCog')
        events = self.bot.get_cog('EventsCog')
        emoji_dir = Path(__file__).resolve().parents[1] / 'emojis'
        await ctx.send('Replacing single-spec emojis…')
        try:
            replaced, failed = await self.replace_spec_emojis(raids.role_names, emoji_dir)
        except discord.HTTPException as exc:
            await ctx.send(f'Could not list application emojis: {exc}')
            return
        await raids.reload_emojis()
        refreshed = 0
        if events:
            now = time.time()
            for row in events.store.rows('SELECT raid_id FROM SharedEvents'):
                event = events.store.event(row['raid_id'])
                if event and not events.store.ended(event, now):
                    events.store.touch(row['raid_id'])
                    await events.refresh(row['raid_id'])
                    refreshed += 1
        msg = f'Replaced {len(replaced)} emojis, refreshed {refreshed} events.'
        if failed:
            msg += '\nFailed: ' + ', '.join(failed)
        await ctx.send(msg)


async def setup(bot):
    await bot.add_cog(DevCog(bot))
    logger.info("Loaded Dev Cog.")
