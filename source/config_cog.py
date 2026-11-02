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
                "Administrators can configure the entire server and run a permission smoke test with a single command:\n"
                "`/setup raid_forum:#raids announcement_channel:#raid-network-push panel_channel:#create-raid voice_category:Raid Voice`\n\n"
                "Once configured, `/setup` posts sticky raid creation panels grouped chronologically by LOTRO release cluster "
                "(newest clusters at the bottom) in your panel channel so raid leaders can schedule any raid with a single click.\n\n"
                "Run `/help` anytime for the complete step-by-step server setup guide (raid forum, push/pull announcement sync, and roles channel).\n"
                "Repository & docs: <https://github.com/nayberhq/grima-bot>"
                ).format(guild_name)
        return msg

    @staticmethod
    def build_help_embeds(topic: str = 'all') -> list[discord.Embed]:
        colour = discord.Colour(0x3498db)
        embeds = []

        if topic in ('all', 'forum'):
            forum_embed = discord.Embed(
                title="1. Unified /setup, Raid Forum & Sticky Cluster Panels",
                colour=colour,
                description=(
                    "Gríma configures your entire server and runs a **permission smoke test** in a single `/setup` command.\n\n"
                    "**Step A — Create the Mandatory Channels & Category**\n"
                    "• **Raid Forum (`#raids`):** Create a **Forum Channel**. In its settings, turn **OFF** *\"Require people to select tags when posting\"*. Grant the bot `View Channel`, `Send Messages`, `Send Messages in Threads`, `Create Public Threads`, `Manage Threads`, `Embed Links`, `Read Message History`, and `Create Invite`.\n"
                    "• **Announcement Push Channel (`#raid-network-push`):** Create a Text or Announcement Channel and grant the bot `View Channel`, `Send Messages`, and `Read Message History`.\n"
                    "• **Event Panel Channel (`#create-raid`):** Create a Text Channel for raid leaders and grant the bot `View Channel`, `Send Messages`, `Embed Links`, and `Read Message History`.\n"
                    "• **Voice Category (`Raid Voice`):** Create a Category and grant the bot `View Channel` + `Manage Channels` so temporary raid voice channels are created at start time.\n\n"
                    "**Step B — Choose Your Forum Routing Mode (`forum_mode` in `/setup`)**\n"
                    "• **Single Channel *(default)*:** All events use `raid_forum`.\n"
                    "• **Instance + Raid:** ≤6-player instances use `instance_forum`; ≥12-player raids use `raid_forum`.\n"
                    "• **Tiers:** 1 forum per tier (`t1_forum`, `t2_forum`, `t3_forum`, `t4_forum`, `t5_forum`; untiered events use `raid_forum`).\n\n"
                    "**Step C — Run `/setup` (Includes Permission Smoke Test)**\n"
                    "`/setup raid_forum:#raids announcement_channel:#raid-network-push panel_channel:#create-raid voice_category:Raid Voice [forum_mode:...] [instance_forum:#instances] [t1_forum..t5_forum] [watch_channel:#pull] [leader_role:@Raid Leader]`\n"
                    "• If any permission or channel flag is missing, `/setup` halts and displays an exact checklist of what to fix.\n"
                    "• Once all checks pass, `/setup` posts all **8 LOTRO Cluster Panels** (with LOTRO Wiki artwork) in chronological release order (Level 50 Angmar at the top → Level 160 Cluster & Custom/PvMP at the bottom) and keeps them sticky at the bottom of `panel_channel`."
                ),
            )
            embeds.append(forum_embed)

        if topic in ('all', 'sync'):
            sync_embed = discord.Embed(
                title="2. Announcements Push & Pull Channel Sync",
                colour=colour,
                description=(
                    "Servers using the same `grima-bot` deployment can automatically publish and mirror shared events via `mirrorkey:<code>` markers.\n\n"
                    "**Outgoing Push Channel (`announcement_channel` in `/setup`)**\n"
                    "1. Configured via the mandatory `announcement_channel` parameter in `/setup`.\n"
                    "2. Whenever a new shared event is created from the cluster panels, Gríma posts `<Event> — <Timestamp>` with `mirrorkey:<code>` to this channel.\n"
                    "3. *Announcement (News) Channels:* Gríma does not auto-publish; click **Publish** on messages you want crossposted to following servers.\n\n"
                    "**Incoming Pull Channel (`watch_channel` in `/setup`)**\n"
                    "1. Create a local channel (e.g. `#raid-network-pull`) that **Follows** your partner servers' Announcement push channels.\n"
                    "2. Ensure **grima-bot** has `View Channel` + `Read Message History` in that channel.\n"
                    "3. Select `watch_channel:#raid-network-pull` when running `/setup`. `/setup` smoke-tests the bot's read access and watches for new `mirrorkey:<code>` messages.\n"
                    "4. Use `/network-status` (`retry:True`) to inspect or retry any failed event discoveries.\n\n"
                    "**Manual Sync & Cross-Server Broadcasts**\n"
                    "• **Manual Share/Pull:** Run `/event-share` inside an event post to get its code (`revoke:True` to disable new mirrors); partner raid leaders run `/event-sync code:<code>`.\n"
                    "• **Broadcast Updates:** Run `/event-announce message:<text>` inside an event post to send an announcement to every linked server's forum thread.\n"
                    "• **Manage & Repair:** Use `/event-status`, `/event-repair code:<code>`, `/event-unlink`, or `/event-reconnect`."
                ),
            )
            embeds.append(sync_embed)

        if topic in ('all', 'roles'):
            roles_embed = discord.Embed(
                title="3. Roles Channel, Class Specs & Server Permissions",
                colour=colour,
                description=(
                    "**Step A — Set Up a `#roles` / `#class-setup` Channel**\n"
                    "Create a text channel (e.g. `#roles`) where members can configure their classes, specializations, and timezones:\n"
                    "• **Set Class Specializations (`/specs`):** Players run `/specs classes:<Class> spec:<Red/Blue/Yellow> [tier:<1-5>]` to save their specialization (omit `tier` to apply across all tiers, or set per-tier specs).\n"
                    "• **Inline Forum Signup Picker:** In shared forum events, if a player clicks `✅` or a class button before running `/specs`, Gríma automatically opens a private **Class & Specialization Picker** so they can choose their class/spec and finish signing up immediately.\n"
                    "• **Classic Discord Class Roles (`/remove_roles`):** For legacy text-channel raids, clicking a class button assigns a matching Discord role (grant the bot `Manage Roles` and place `grima-bot`'s role above the LOTRO class roles in `Server Settings → Roles`). Players can clear their class roles anytime with `/remove_roles`.\n"
                    "• **Personal Timezone (`/time_zones personal`):** Players can set their personal timezone so times they type when creating events are parsed in their local time.\n\n"
                    "**Step B — Configure Raid Leader, Kinship & Optional Channels in `/setup`**\n"
                    "• **Raid Leader Role (`leader_role` in `/setup`):** Members with this role can create events from the cluster panels, edit raid settings, build/edit rosters (`/roster-create`, `/roster-manage`, `/roster-edit`, `/roster-swap`), and run `/event-sync`.\n"
                    "• **Kinship Role & Icon (`kin_role` & `kin_icon` in `/setup`):** Badges kinship members on raid signups.\n"
                    "• **Optional Live Calendar & RSS (`calendar_channel`, `discord_events`, `rss_channel` in `/setup`):** Posts an auto-updating raid summary embed, creates native Discord Scheduled Events, and/or posts official LOTRO forum RSS news."
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

    @app_commands.command(name=_("help"), description=_("Show the server setup guide for raid forums, push/pull sync, and roles."))
    @app_commands.describe(
        topic=_("Choose which setup section to display (defaults to All)."),
        public=_("Post the guide publicly in the channel instead of only to you (defaults to False)."),
    )
    @app_commands.choices(topic=[
        app_commands.Choice(name="All — Complete Server Setup Guide", value="all"),
        app_commands.Choice(name="1. Raid Forum Channel & Creation Panels", value="forum"),
        app_commands.Choice(name="2. Announcements Push & Pull Channel Sync", value="sync"),
        app_commands.Choice(name="3. Roles Channel, Class Specs & Permissions", value="roles"),
    ])
    @app_commands.guild_only()
    async def help_respond(self, interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None, public: bool = False):
        selected = topic.value if topic else "all"
        embeds = self.build_help_embeds(selected)
        await interaction.response.send_message(embeds=embeds, ephemeral=not public)

    @app_commands.command(name=_("about"), description=_("Show information about this bot."))
    @app_commands.guild_only()
    async def about_respond(self, interaction: discord.Interaction):
        about = await self.about_embed()
        await interaction.response.send_message(embed=about)

    @app_commands.command(name=_("welcome"), description=_("Resend the welcome message."))
    @app_commands.guild_only()
    async def welcome_respond(self, interaction: discord.Interaction):
        welcome = self.welcome_msg(interaction.guild.name)
        await interaction.response.send_message(welcome)

    @app_commands.command(name=_("privacy"), description=_("Show information on data collection."))
    @app_commands.guild_only()
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
