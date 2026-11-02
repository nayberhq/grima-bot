import dateparser
import discord
import logging
import pytz
import re

from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands

from database import select_one, select_order, upsert
from utils import chunks
from event_store import EventStore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@app_commands.guild_only()
class CalendarCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conn = bot.conn
        EventStore(self.conn)
        self.time_cog = bot.get_cog('TimeCog')
        self.upcoming_events = None
        self.cached_events_at = None

    def is_raid_leader(self, user, guild):
        if user.guild_permissions.administrator:
            return True
        raid_leader_id = select_one(self.conn, 'Settings', ['raid_leader'], ['guild_id'], [guild.id])
        if raid_leader_id:
            raid_leader = guild.get_role(raid_leader_id)
            if raid_leader in user.roles:
                return True
        return False

    async def post_calendar(self, guild_id, channel):
        embed = self.calendar_embed(guild_id)
        msg = await channel.send(embed=embed)
        ids = "{0}/{1}".format(channel.id, msg.id)
        res = upsert(self.conn, 'Settings', ['calendar'], [ids], ['guild_id'], [guild_id])
        self.conn.commit()

    async def update_calendar(self, guild_id):
        conn = self.bot.conn
        res = select_one(conn, 'Settings', ['calendar'], ['guild_id'], [guild_id])
        if not res:
            return
        result = res.split("/")
        chn_id = int(result[0])
        msg_id = int(result[1])
        chn = self.bot.get_channel(chn_id)
        try:
            msg = chn.get_partial_message(msg_id)
        except AttributeError:
            logger.warning("Calendar channel not found for guild {0}.".format(guild_id))
            res = upsert(conn, 'Settings', ['calendar'], [None], ['guild_id'], [guild_id])
            if res:
                conn.commit()
            return

        embed = self.calendar_embed(guild_id)
        try:
            await msg.edit(embed=embed)
        except discord.Forbidden:
            logger.warning("Calendar access restricted for guild {0}.".format(guild_id))
            return
        except discord.NotFound:
            logger.warning("Calendar post not found for guild {0}.".format(guild_id))
            upsert(conn, 'Settings', ['calendar'], [None], ['guild_id'], [guild_id])
            conn.commit()
            return
        except discord.HTTPException as e:
            logger.warning("Failed to update calendar for guild {0}.".format(guild_id))
            logger.warning(e)
            return

    def calendar_embed(self, guild_id):
        conn = self.bot.conn
        raids = conn.execute('''SELECT r.channel_id,r.raid_id,r.name,r.tier,r.time FROM Raids r
            LEFT JOIN SharedEvents e USING (raid_id) WHERE r.guild_id=?
            AND (e.raid_id IS NULL OR (e.cancelled=0 AND r.time+e.duration>CAST(strftime('%s','now') AS INTEGER)))
            UNION ALL SELECT c.thread_id,c.message_id,r.name,r.tier,r.time
            FROM EventCopies c JOIN Raids r USING (raid_id) JOIN SharedEvents e USING (raid_id)
            WHERE c.guild_id=? AND r.guild_id!=? AND e.cancelled=0
            AND r.time+e.duration>CAST(strftime('%s','now') AS INTEGER) ORDER BY 5''', (guild_id,guild_id,guild_id)).fetchall()

        title = _("Scheduled runs:")
        desc = _("Click the link to sign up!")
        embed = discord.Embed(title=title, description=desc, colour=discord.Colour(0x3498db))
        for raid in raids[:20]:
            timestamp = int(raid[4])
            tier = raid[3]
            if tier:
                msg = "[{name} {tier}](<https://discord.com/channels/{guild}/{channel}/{msg}>)\n".format(
                guild=guild_id, channel=raid[0], msg=raid[1], name=raid[2], tier=raid[3])
            else:
                msg = "[{name}](<https://discord.com/channels/{guild}/{channel}/{msg}>)\n".format(
                guild=guild_id, channel=raid[0], msg=raid[1], name=raid[2])
            embed.add_field(name=f"<t:{timestamp}:F>", value=msg, inline=False)
        embed.set_footer(text=_("Last updated"))
        embed.timestamp = datetime.now()
        return embed

    async def create_guild_event(self, guild, raid_id):
        conn = self.bot.conn
        res = select_one(conn, 'Settings', ['guild_events'], ['guild_id'], [guild.id])
        if not res:
            return 0
        channel_id, name, tier, description, timestamp = select_one(conn, 'Raids', ['channel_id', 'name', 'tier', 'boss', 'time'], ['raid_id'], [raid_id])

        location = f"https://discord.com/channels/{guild.id}/{channel_id}/{raid_id}"
        start_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        shared = EventStore(conn).event(raid_id)
        end_time = datetime.fromtimestamp(timestamp+(shared['duration'] if shared else 7200), tz=timezone.utc)
        if tier:
            event_name = " ".join([name, tier])
        else:
            event_name = name

        try:
            event = await guild.create_scheduled_event(name=event_name, start_time=start_time, end_time=end_time, entity_type=discord.EntityType.external, privacy_level=discord.PrivacyLevel.guild_only, location=location, description=description)
        except discord.Forbidden:
            logger.warning("Missing manage events permission for guild {0}".format(guild.id))
            event_id = None
        else:
            event_id = event.id
        return event_id

    async def modify_guild_event(self, guild, raid_id):
        conn = self.bot.conn
        res = select_one(conn, 'Settings', ['guild_events'], ['guild_id'], [guild.id])
        if not res:
            return
        event_id, name, tier, description, timestamp = select_one(conn, 'Raids', ['event_id', 'name', 'tier', 'boss', 'time'], ['raid_id'], [raid_id])
        if not event_id:
            return

        # discord.py does not have partial event
        event = await guild.fetch_scheduled_event(event_id, with_counts=False)
        start_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        shared = EventStore(conn).event(raid_id)
        end_time = datetime.fromtimestamp(timestamp+(shared['duration'] if shared else 7200), tz=timezone.utc)
        if tier:
            event_name = " ".join([name, tier])
        else:
            event_name = name
        try:
            await event.edit(name=event_name, description=description, start_time=start_time, end_time=end_time)
        except discord.Forbidden:
            logger.warning("Missing manage events permission for guild {0}".format(guild.id))

    async def delete_guild_event(self, guild, raid_id):
        conn = self.bot.conn
        event_id = select_one(conn, 'Raids', ['event_id'], ['raid_id'], [raid_id])
        if not event_id:
            return

        # discord.py does not have partial event
        event = await guild.fetch_scheduled_event(event_id, with_counts=False)
        try:
            await event.delete()
        except discord.Forbidden:
            logger.warning("Missing manage events permission for guild {0}".format(guild.id))

    async def get_events(self):
        current_time = datetime.now().timestamp()
        if self.cached_events_at and self.cached_events_at + 86400 > current_time:
            return self.upcoming_events

        event_url = "https://www.lotro.com/news/lotro-public-event-schedule-en"
        async with self.bot.http_session.get(event_url) as resp:
            if not resp.ok:
                logger.warning("Could not connect to lotro.com")
                return self.upcoming_events
            text = await resp.text()

        stripped = re.sub('<[^<]+?>', '', text)
        stripped = stripped.replace('\xa0', '')
        stripped = stripped.replace('&gt;', '>')
        # Let us hope this questionable pattern is stable enough
        pattern = r'ALL TIMES EASTERN/SERVER TIME(.*)Ends \(Eastern Time\)(.*)Share On:'

        prog = re.compile(pattern, flags=re.DOTALL)
        result = prog.search(stripped)
        events_data = result.group(2).strip().splitlines() + ['']
        events = [chunk for chunk in chunks(events_data, 5)]
        parsed_events = [(event[0], self.parse_event_time(event[1]), self.parse_event_time(event[2])) for event in events]

        cutoff_unlock = current_time - 30 * 86400
        cutoff_past = current_time - 86400
        cutoff_future = current_time + 60 * 86400

        upcoming_events = []
        for event in parsed_events:
            start_time = current_time
            if event[1]:
                start_time = event[1]
            if event[2]:
                if cutoff_past < event[2] and start_time < cutoff_future:
                    upcoming_events.append(event)
            else:
                if cutoff_unlock < start_time < cutoff_future:
                    upcoming_events.append(event)
            if start_time > cutoff_future:
                break

        self.cached_events_at = current_time
        self.upcoming_events = upcoming_events
        return upcoming_events

    async def events_embed(self, guild_id):
        events = await self.get_events()
        events = events[:25]

        title = _("Upcoming events:")
        embed = discord.Embed(title=title, colour=discord.Colour(0x3498db))
        for e in events:
            time_str = ""
            if e[1] and e[2]:
                time_str = f"<t:{e[1]}> -- <t:{e[2]}>"
            else:
                if e[1]:
                    time_str = f"From <t:{e[1]}>"
                if e[2]:
                    time_str = f"At <t:{e[2]}>"
            embed.add_field(name=e[0], value=time_str, inline=False)
        embed.set_footer(text=_("Last updated"))
        embed.timestamp = datetime.fromtimestamp(self.cached_events_at)
        return embed

    def parse_event_time(self, time_string):
        if not time_string:
            return None
        time_string = time_string.casefold()
        time_string = time_string.replace(" eastern", "")
        time_string = time_string.replace("approximately ", "")
        time = dateparser.parse(time_string)
        try:
            time = pytz.timezone("America/New_York").localize(time).timestamp()
        except:
            logger.info(f"Calendar failed to parse: {time_string}")
            return None
        return int(time)

    @app_commands.command(name=_("events"), description=_("Shows upcoming official LotRO events in your local time."))
    @app_commands.guild_only()
    async def events_respond(self, interaction: discord.Interaction):
        await interaction.response.send_message(_("Waiting for lotro.com to respond..."))
        events = await self.events_embed(interaction.guild_id)
        await interaction.edit_original_response(content='', embed=events)


async def setup(bot):
    await bot.add_cog(CalendarCog(bot))
    logger.info("Loaded Calendar Cog.")
