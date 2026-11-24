import datetime
import discord
import logging

from discord import app_commands
from discord.ext import commands
from discord.utils import find

from database import upsert

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class ConfigCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conn = self.bot.conn

    @staticmethod
    def td_format(td_object):
        seconds = int(td_object.total_seconds())
        periods = [
            ('year', 60 * 60 * 24 * 365),
            ('month', 60 * 60 * 24 * 30),
            ('day', 60 * 60 * 24),
            ('hour', 60 * 60),
            ('minute', 60),
            ('second', 1)
        ]

        strings = []
        for period_name, period_seconds in periods:
            if seconds > period_seconds:
                period_value, seconds = divmod(seconds, period_seconds)
                has_s = 's' if period_value > 1 else ''
                strings.append("%s %s%s" % (period_value, period_name, has_s))

        return ", ".join(strings)

    async def about_embed(self):
        dev = "Baviaan#4862"
        repo = "https://github.com/Baviaan/lotro"
        code = "dGcBzPN"
        server = "https://discord.gg/"+code
        app_info = await self.bot.application_info()
        try:
            host = app_info.team.name
        except AttributeError:
            host = app_info.owner.name
        uptime = datetime.datetime.utcnow() - self.bot.launch_time
        uptime = self.td_format(uptime)

        invite_link = "https://discord.com/api/oauth2/authorize?client_id={0}&permissions=268462080&scope=bot" \
                      "%20applications.commands".format(self.bot.user.id)
        #donate_link = "https://www.paypal.com/donate?hosted_button_id=WWPCUJVJPMT7W"
        releases = repo + "/releases/latest"
        async with  self.bot.http_session.get(releases, allow_redirects=False) as r:
            if r.ok:
                try:
                    location = r.headers['location']
                except KeyError:
                    latest_version = "N/A"
                else:
                    (x, y, latest_version) = location.rpartition('/')
            else:
                latest_version = "N/A"

        title = "{0}".format(self.bot.user)
        about = [
            _("A bot for scheduling raids!"),
            _("**Developer:** {0}").format(dev),
            _("**[Source code]({0})**").format(repo),
            _("**[Support server]({0})**").format(server),
            _("**[Invite me!]({0})**").format(invite_link),
            #_("**[Donate]({0})**").format(donate_link),
            "",
            _("**Hosted by:** {0}").format(host),
            _("**Uptime:** {0}.").format(uptime),
            "",
            _("**Using version:** {0}").format(self.bot.version),
            _("**Latest version:** {0}").format(latest_version)
        ]

        content = "\n".join(about)
        embed = discord.Embed(title=title, colour=discord.Colour(0x3498db), description=content)
        return embed

    @staticmethod
    def welcome_msg(guild_name):
        msg = _("Greetings {0}! I am here to coordinate your LOTRO raids and cross-server events.\n\n"
                "Administrators configure the server with `/settings`. `/watch` chooses channels scanned for shared-event codes. "
                "`/commands` opens everything else, including setup and setup-reset. `/profile` sets your class, specialization, and personal timezone.\n\n"
                "Saving the required channels in `/settings` posts sticky raid creation panels in the panel channel.\n\n"
                "Open `/commands` and choose help for the full setup guide.\n"
                "Repository & docs: <https://github.com/nayberhq/grima-bot>"
                ).format(guild_name)
        return msg

    @staticmethod
    def build_help_embeds(topic: str = 'all') -> list[discord.Embed]:
        colour = discord.Colour(0x3498db)
        embeds = []

        if topic in ('all', 'forum'):
            forum_embed = discord.Embed(
                title="1. /settings, Raid Forum & Sticky Cluster Panels",
                colour=colour,
                description=(
                    "Gríma configures the server from `/settings`. `/commands` → setup opens the same editor and runs the permission smoke test.\n\n"
                    "**Step A — Create the Mandatory Channels**\n"
                    "• **Raid Forum (`#raids`):** Create a **Forum Channel**. Turn **OFF** *\"Require people to select tags when posting\"*. Grant the bot `View Channel`, `Create Posts`, `Send Messages in Posts`, `Manage Posts`, `Embed Links`, `Read Post History`, and `Create Invite`.\n"
                    "• **Announcement Push Channel (`#raid-network-push`):** Text or Announcement Channel with `View Channel`, `Send Messages`, and `Read Message History`.\n"
                    "• **Event Panel Channel (`#create-raid`):** Text Channel with `View Channel`, `Send Messages`, `Embed Links`, and `Read Message History`.\n"
                    "• **Voice Category (optional):** `View Channel` and `Manage Channels`. Leave it unset and events do not create or link a voice channel.\n\n"
                    "**Step B — Optional Forums**\n"
                    "Events use the first forum that applies: a matching tier forum (`t1_forum`–`t5_forum`), then `instance_forum` for ≤6-player events, then `raid_forum`.\n\n"
                    "**Step C — `/settings`**\n"
                    "• Page 1 is the `(required)` channels: `raid_forum`, `announcement_channel`, and `panel_channel`, plus an optional voice category. **Save** appears after those three are set.\n"
                    "• Later pages cover instance and tier forums, `watch_channel`, roles, calendar, RSS, and timings. `/watch` adds or removes watched channels.\n"
                    "• `/commands` → setup-reset clears this server's saved setup. Existing events stay.\n"
                    "• A missing permission stops setup with a checklist. The first successful setup posts the cluster panels in the panel channel."
                ),
            )
            embeds.append(forum_embed)

        if topic in ('all', 'sync'):
            sync_embed = discord.Embed(
                title="2. Announcements Push & Pull Channel Sync",
                colour=colour,
                description=(
                    "Servers on the same deployment mirror events with `mirrorkey:<code>`.\n\n"
                    "**Outgoing push (`announcement_channel`)**\n"
                    "Set it in `/settings`. New shared events post `<Event> — <Timestamp>` with `mirrorkey:<code>`. On an Announcement channel, click **Publish** yourself; Gríma does not auto-publish.\n\n"
                    "**Incoming pull (`/watch`)**\n"
                    "1. Follow a partner's announcement channel (for example `#raid-network-pull`).\n"
                    "2. Grant the bot `View Channel` and `Read Message History`.\n"
                    "3. Add the channel with `/watch` or the `watch_channel` field in `/settings`. **Watch channel calendar** is on by default. **Watch channel event syncing** is off by default.\n"
                    "4. Open `/commands` and choose network-status to inspect or retry failed discoveries.\n\n"
                    "**Share, sync, and repair**\n"
                    "Open `/commands` inside the event post. event-share creates the code, and event-share revoke stops new mirrors. A partner raid leader uses event-sync. event-announce sends a message to every linked thread. event-status, event-repair, event-unlink, and event-reconnect manage the links."
                ),
            )
            embeds.append(sync_embed)

        if topic in ('all', 'roles'):
            roles_embed = discord.Embed(
                title="3. Roles Channel, Class Specs & Server Permissions",
                colour=colour,
                description=(
                    "**`/profile`**\n"
                    "Players set class, specialization, an optional tier (or all tiers), and a personal timezone. **Clear class roles** removes the Discord class roles a signup can grant. Grant the bot `Manage Roles` and place it above the class roles.\n"
                    "Signing up in a forum event without a saved spec opens a private class picker.\n\n"
                    "**`/commands`**\n"
                    "Raid leaders build rosters and sync events from this panel. Open it inside the event post for roster and event actions.\n\n"
                    "**`/settings` roles and extras**\n"
                    "`leader_role` limits who can create events. Leave it unset and anyone can. `kin_role` and `kin_icon` badge kinship members. The calendar channel, Discord scheduled events, and RSS channel are optional."
                ),
            )
            embeds.append(roles_embed)

        return embeds

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        logger.info("We have joined {0}.".format(guild))
        timestamp = int(datetime.datetime.now().timestamp())
        upsert(self.conn, 'Settings', ['last_command'], [timestamp], ['guild_id'], [guild.id])
        self.conn.commit()
        channels = guild.text_channels
        channel = find(lambda x: x.name == 'welcome', channels)
        if not channel or not channel.permissions_for(guild.me).send_messages:
            channel = find(lambda x: x.name == 'general', channels)
        # Otherwise pick the first channel the bot can send a message in.
        if not channel or not channel.permissions_for(guild.me).send_messages:
            for ch in channels:
                if ch.permissions_for(guild.me).send_messages:
                    channel = ch
                    break
        if channel and channel.permissions_for(guild.me).send_messages:
            msg = self.welcome_msg(guild.name)
            await channel.send(msg)

    async def help_respond(self, interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None, public: bool = False):
        selected = topic.value if topic else "all"
        embeds = self.build_help_embeds(selected)
        await interaction.response.send_message(embeds=embeds, ephemeral=not public)

    async def about_respond(self, interaction: discord.Interaction):
        about = await self.about_embed()
        await interaction.response.send_message(embed=about)

    async def welcome_respond(self, interaction: discord.Interaction):
        welcome = self.welcome_msg(interaction.guild.name)
        await interaction.response.send_message(welcome)

    async def privacy_respond(self, interaction: discord.Interaction):
        privacy = _("**Summary:**\n"
                     "When you sign up for a raid the bot stores the time, your discord id, discord nickname and the class("
                    "es) you sign up with. Ordinary text-channel events are deleted 2 hours after the scheduled "
                    "raid time. Shared forum events retain their roster and discussion after closing, and their "
                    "roster is visible in every server that mirrors the event. Cancelling your signup removes "
                    "your current signup record, but does not delete previously posted Discord messages.\n"
                    "Configured channel watchers read message text to find mirrorkey sharing codes. They store "
                    "channel/message IDs and event links, not a copy of the channel's conversation.\n"
                    "If you set a default time zone for yourself, the bot will additionally store your time zone "
                    "along with your discord id such that it can parse times provided in your commands in your "
                    "preferred time zone.\n"
                    "Ask the administrators hosting this bot about retention and deletion for this deployment.")
        await interaction.response.send_message(privacy)


async def setup(bot):
    await bot.add_cog(ConfigCog(bot))
    logger.info("Loaded Config Cog.")
