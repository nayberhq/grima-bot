"""Creation panels, forum/voice lifecycle and cross-guild projections."""
import asyncio
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from event_store import EventStore, forum_title, mirror_keys, InvalidShare
from time_cog import Time

log = logging.getLogger(__name__)
PRESETS = json.loads(Path(__file__).with_name('event_panels.json').read_text())


class EventsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = EventStore(bot.conn)
        self.locks = defaultdict(asyncio.Lock)
        self.delivery_lock = asyncio.Lock()
        self.watch_lock = asyncio.Lock()
        from durable_send import DurableSender
        self.sender=DurableSender(bot.conn,bot)

    @property
    def raids(self):
        return self.bot.get_cog('RaidCog')

    async def cog_load(self):
        for row in self.store.rows('SELECT * FROM CreationPanels'):
            if row['preset'] in PRESETS:
                self.bot.add_view(CreationView(self, row['preset']), message_id=row['message_id'])
        self.lifecycle.start()

    async def cog_unload(self):
        self.lifecycle.cancel()

    async def channel(self, channel_id):
        return self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)

    def title(self, event, guild_id):
        timezone = self.bot.get_cog('TimeCog').get_server_timezone(guild_id)
        title = ' '.join(filter(None, [event['name'], event['tier']]))
        return forum_title(title, event['time'], timezone)

    async def can_manage(self, interaction, raid_id):
        event = self.store.event(raid_id)
        if not event:
            return False
        if interaction.user.id == event['organizer_id']:
            return True
        # A destination's administrator cannot acquire authority over the source raid.
        guild = self.bot.get_guild(event['guild_id'])
        if not guild:
            return False
        try:
            member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
        except (discord.NotFound, discord.Forbidden):
            return False
        return self.raids.calendar_cog.is_raid_leader(member, guild)

    async def command_event(self, interaction, manage=True):
        raid_id = self.store.resolve(interaction.channel_id)
        event = self.store.event(raid_id)
        if not event:
            await interaction.response.send_message('Use this command inside an event forum post.', ephemeral=True)
            return None
        if manage and not await self.can_manage(interaction, raid_id):
            await interaction.response.send_message('Only the organizer or a source-server raid leader can do that.', ephemeral=True)
            return None
        return event

    async def validate_destinations(self, settings, require_voice=True):
        forum = await self.channel(settings['forum_id'])
        if require_voice and not settings['voice_category_id']:
            raise ValueError('Hosting events requires a voice category. Set it with /event-setup.')
        category = await self.channel(settings['voice_category_id']) if require_voice else None
        if not isinstance(forum, discord.ForumChannel) or (require_voice and not isinstance(category, discord.CategoryChannel)):
            raise ValueError('Configure an existing forum and voice category with /event-setup.')
        if forum.guild.id != settings['guild_id'] or (category and category.guild.id != settings['guild_id']):
            raise ValueError('Forum and voice category must belong to this server.')
        perms = forum.permissions_for(forum.guild.me)
        if not all((perms.view_channel, perms.send_messages, perms.send_messages_in_threads,
                    perms.embed_links, perms.read_message_history, perms.manage_threads)):
            raise ValueError('The bot needs View Channel, Send Messages, Send Messages in Threads, Embed Links, Read Message History and Manage Threads in the forum.')
        if category and not category.permissions_for(category.guild.me).manage_channels:
            raise ValueError('The bot needs Manage Channels in the voice category.')
        if forum.flags.require_tag:
            raise ValueError('Disable required tags on the event forum before using it.')
        return forum

    async def create_origin(self, guild_id, name, tier, timestamp):
        settings = self.store.settings(guild_id)
        if not settings:
            return None
        forum = await self.validate_destinations(settings)
        event = dict(name=name, tier=tier, time=timestamp)
        result = await forum.create_thread(name=self.title(event, guild_id),
            content='Preparing event…', allowed_mentions=discord.AllowedMentions.none(),
            auto_archive_duration=10080, reason='Create raid forum')
        return result.thread, result.message, settings

    async def refresh(self, raid_id):
        async with self.locks[raid_id]:
            await self.refresh_locked(raid_id)

    async def refresh_locked(self, raid_id, now=None):
        event = self.store.event(raid_id)
        if not event:
            return
        from raid_cog import RaidView, CreepView
        embed = self.raids.build_raid_message(raid_id,
            self.raids.build_raid_players(raid_id), self.raids.build_raid_players(raid_id, available=False))
        if not embed:
            return
        ended = self.store.ended(event, time.time() if now is None else now)
        embed.add_field(name='Ends', value=f"<t:{event['time'] + event['duration']}:F>", inline=False)
        embed.set_footer(text='Cancelled' if event['cancelled'] else ('Event ended' if ended else 'Shared roster • Sign up from any linked server'))
        for copy in self.store.copies(raid_id):
            if copy['locked'] or copy['revision'] == event['revision']:
                continue
            try:
                thread = await self.channel(copy['thread_id'])
                await thread.edit(name=self.title(event, copy['guild_id']), archived=False)
                local_embed = embed.copy()
                planning = self.bot.get_cog('PlanningCog')
                if planning:
                    template = planning.template(event, 'description')
                    if template:
                        try:
                            local_embed.description = planning.render(event, template['body'], min(4096, 6000-len(local_embed)+(len(local_embed.description or ''))))
                        except ValueError:
                            log.warning('Description template exceeds embed budget for event %s', raid_id)
                owner = next((c for c in self.store.copies(raid_id) if c['guild_id']==event['guild_id']), None)
                if owner and owner['voice_id']:
                    local_embed.add_field(name='Event voice channel', value=f"https://discord.com/channels/{event['guild_id']}/{owner['voice_id']}", inline=False)
                elif not ended:
                    local_embed.add_field(name='Event voice channel', value='Created in the owning server when the event starts.', inline=False)
                if copy['guild_id'] != event['guild_id']:
                    invite=self.store.one('SELECT url FROM SourceInvites WHERE guild_id=?',(event['guild_id'],))
                    local_embed.add_field(name='Join the event server',value=invite['url'] if invite else 'Invite unavailable. Ask the organizer to set /event-invite in the owning server.',inline=False)
                view = None if ended else (CreepView(self.raids) if event['creep'] else RaidView(self.raids))
                await thread.get_partial_message(copy['message_id']).edit(
                    content=None, embed=local_embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                if copy['voice_id'] and copy['guild_id']==event['guild_id']:
                    try:
                        voice = await self.channel(copy['voice_id'])
                    except discord.NotFound:
                        self.store.write('UPDATE EventCopies SET voice_id=NULL WHERE thread_id=?', (copy['thread_id'],))
                    else:
                        await voice.edit(name=self.title(event, copy['guild_id']))
                self.store.write('UPDATE EventCopies SET revision=?, ended_rendered=? WHERE thread_id=?',
                                 (event['revision'], int(ended), copy['thread_id']))
            except discord.HTTPException:
                log.exception('Could not refresh event %s in server %s; will retry', raid_id, copy['guild_id'])

    async def cancel(self, raid_id):
        self.store.write('UPDATE SharedEvents SET cancelled=1, share_token=NULL, revision=revision+1 WHERE raid_id=?', (raid_id,))
        await self.reconcile(raid_id)

    async def reconcile(self, raid_id, now=None):
        now = time.time() if now is None else now
        async with self.locks[raid_id]:
            event = self.store.event(raid_id)
            if not event:
                return
            ended = self.store.ended(event, now)
            if not ended and any(c['guild_id']!=event['guild_id'] and not c['locked'] for c in self.store.copies(raid_id)):
                if not self.store.one('SELECT 1 FROM SourceInvites WHERE guild_id=?',(event['guild_id'],)):
                    try:
                        settings=self.store.settings(event['guild_id'])
                        forum=await self.channel(settings['forum_id'])
                        invite=await forum.create_invite(max_age=0,max_uses=0,unique=False,reason='Join the owning server for shared events')
                        self.store.write('INSERT OR REPLACE INTO SourceInvites VALUES (?,?)',(event['guild_id'],str(invite.url)))
                        self.store.touch(raid_id)
                    except discord.HTTPException:
                        log.warning('Cannot create owner-server invite for event %s; set /event-invite or grant Create Invite',raid_id)
            # Update the final embed before locking; the renderer removes controls.
            if ended:
                for copy in self.store.copies(raid_id):
                    if not copy['locked'] and not copy['ended_rendered']:
                        self.store.write('UPDATE EventCopies SET revision=0 WHERE thread_id=?', (copy['thread_id'],))
                await self.refresh_locked(raid_id, now)
            for copy in self.store.copies(raid_id):
                try:
                    guild = self.bot.get_guild(copy['guild_id'])
                    if not guild:
                        continue
                    if not ended and not copy['locked']:
                        thread = await self.channel(copy['thread_id'])
                        if thread.archived and not thread.locked:
                            await thread.edit(archived=False)
                    if copy['guild_id']==event['guild_id'] and not ended and not copy['locked'] and now >= event['time'] and not copy['voice_id']:
                        category = await self.channel(copy['voice_category_id'])
                        # A deterministic marker recovers a successful Discord creation
                        # if the process died before saving its ID.
                        marker = f'event-{raid_id}'
                        voice = discord.utils.get(category.voice_channels, name=marker)
                        if voice is None:
                            voice = await guild.create_voice_channel(marker, category=category,
                                reason=f'Raid {raid_id} started')
                        self.store.write('UPDATE EventCopies SET voice_id=?, revision=0 WHERE thread_id=?',
                                         (voice.id, copy['thread_id']))
                        copy['voice_id'] = voice.id
                        self.store.touch(raid_id)
                    if copy['voice_id'] and (copy['guild_id']!=event['guild_id'] or (not ended and now < event['time'])):
                        # A running event may be rescheduled; retire the early channel
                        # when empty, and recreate it at the new start.
                        try:
                            voice = await self.channel(copy['voice_id'])
                        except discord.NotFound:
                            voice = None
                        if voice is None or not voice.members:
                            if voice:
                                await voice.delete(reason='Voice belongs only to the owning event server, or event was rescheduled')
                            self.store.write('UPDATE EventCopies SET voice_id=NULL, revision=0 WHERE thread_id=?', (copy['thread_id'],))
                            self.store.touch(raid_id)
                            copy['voice_id']=None
                    if ended:
                        if copy['voice_id']:
                            try:
                                voice = await self.channel(copy['voice_id'])
                            except discord.NotFound:
                                voice = None
                            if voice is None or not voice.members:
                                if voice:
                                    await voice.delete(reason='Event ended and voice channel is empty')
                                self.store.write('UPDATE EventCopies SET voice_id=NULL, revision=0 WHERE thread_id=?',
                                                 (copy['thread_id'],))
                                self.store.touch(raid_id)
                    if self.store.lock_due(event, now) and not copy['locked']:
                        thread = await self.channel(copy['thread_id'])
                        await thread.edit(locked=True, archived=True, reason='Event finished')
                        self.store.write('UPDATE EventCopies SET locked=1 WHERE thread_id=?', (copy['thread_id'],))
                except discord.HTTPException:
                    log.exception('Event lifecycle failed for %s/%s; will retry', raid_id, copy['guild_id'])
            await self.refresh_locked(raid_id, now)

    async def sync_event(self, guild_id, code):
        """One entry point for manual sync and watcher delivery. Idempotent per guild."""
        event = self.store.by_token(code.strip())
        settings = self.store.settings(guild_id)
        if event and self.store.one('SELECT 1 FROM EventBlocks WHERE raid_id=? AND guild_id=?', (event['raid_id'],guild_id)):
            raise InvalidShare('This server was disconnected by the organizer.')
        if not event or self.store.ended(event, time.time()):
            raise InvalidShare('Invalid sharing code or the event has ended.')
        if not settings:
            raise ValueError('Run /event-setup in the destination server first.')
        raid_id = event['raid_id']
        async with self.locks[raid_id]:
            event = self.store.by_token(code.strip())
            if not event or self.store.ended(event, time.time()):
                raise InvalidShare('This sharing code is no longer active.')
            existing = self.store.one('SELECT * FROM EventCopies WHERE raid_id=? AND guild_id=?', (raid_id, guild_id))
            if existing:
                try:
                    thread = await self.channel(existing['thread_id'])
                    await thread.fetch_message(existing['message_id'])
                    await thread.edit(locked=False,archived=False)
                    self.store.write('UPDATE EventCopies SET revision=0,locked=0 WHERE raid_id=? AND guild_id=?', (raid_id,guild_id))
                    await self.refresh_locked(raid_id)
                    return existing['thread_id'], False
                except discord.NotFound:
                    # Preserve the canonical raid and any surviving voice channel.
                    pass
            forum = await self.validate_destinations(settings, require_voice=False)
            result = await forum.create_thread(name=self.title(event, guild_id), content='Preparing shared raid…',
                auto_archive_duration=10080, allowed_mentions=discord.AllowedMentions.none())
            if existing:
                self.store.write('UPDATE EventCopies SET thread_id=?,message_id=?,revision=0,locked=0 WHERE raid_id=? AND guild_id=?', (result.thread.id,result.message.id,raid_id,guild_id))
                if guild_id == event['guild_id']:
                    self.store.write('UPDATE Raids SET channel_id=? WHERE raid_id=?', (result.thread.id,raid_id))
            else:
                self.store.add_copy(raid_id, guild_id, result.thread.id, result.message.id, settings['voice_category_id'])
            await self.refresh_locked(raid_id)
        await self.reconcile(raid_id)
        await self.raids.calendar_cog.update_calendar(guild_id)
        return result.thread.id, True

    async def scan_watches(self):
        # A durable cursor also catches messages sent while the process was offline.
        async with self.watch_lock:
            for watch in self.store.rows('SELECT * FROM EventWatches'):
                try:
                    channel = await self.channel(watch['source_channel_id'])
                    async for message in channel.history(limit=100, oldest_first=True,
                            after=discord.Object(id=watch['last_message_id'])):
                        # Announcement drafts are not discovery authorization until manually published.
                        if isinstance(channel,discord.TextChannel) and channel.is_news() and not message.flags.crossposted:
                            if mirror_keys(message.content):
                                self.store.write('INSERT OR IGNORE INTO PendingPublications VALUES (?,?,?)',(watch['guild_id'],watch['source_channel_id'],message.id))
                            self.store.write('UPDATE EventWatches SET last_message_id=? WHERE guild_id=? AND source_channel_id=?',(message.id,watch['guild_id'],watch['source_channel_id']))
                            continue
                        # Only literal message text is parsed; no embeds or quoted URLs.
                        for code in mirror_keys(message.content):
                            try:
                                await self.sync_event(watch['guild_id'], code)
                            except InvalidShare:
                                # Invalid/expired markers are permanent failures, not a backlog blocker.
                                log.info('Ignored invalid mirrorkey in message %s', message.id)
                            except (ValueError, discord.HTTPException) as exc:
                                self.store.write('INSERT OR REPLACE INTO WatchFailures VALUES (?,?,?,?,?)', (watch['guild_id'],watch['source_channel_id'],message.id,code,str(exc)))
                        self.store.write('''UPDATE EventWatches SET last_message_id=?
                            WHERE guild_id=? AND source_channel_id=?''',
                            (message.id, watch['guild_id'], watch['source_channel_id']))
                except discord.HTTPException:
                    # Do not advance past a transient failure; already-created copies deduplicate.
                    log.exception('Watcher failed for server %s channel %s; will retry',
                                  watch['guild_id'], watch['source_channel_id'])

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild and self.store.one('SELECT 1 FROM EventWatches WHERE source_channel_id=?', (message.channel.id,)):
            await self.scan_watches()

    async def deliver_announcements(self):
        async with self.delivery_lock:
            await self._deliver_announcements()

    async def _deliver_announcements(self):
        for delivery in self.store.pending_announcements():
            try:
                thread = await self.channel(delivery['thread_id'])
                # Do not resurrect finished threads to retry a failed announcement.
                if thread.locked:
                    continue
                if thread.archived:
                    await thread.edit(archived=False)
                await self.sender.send(thread,f"announcement:{delivery['id']}:{delivery['thread_id']}",f"**Announcement from <@{delivery['author_id']}>**\n{delivery['body']}")
                self.store.write('UPDATE AnnouncementDeliveries SET sent=1 WHERE announcement_id=? AND thread_id=?',
                                 (delivery['id'], delivery['thread_id']))
            except discord.HTTPException:
                log.exception('Announcement %s failed in %s; will retry', delivery['id'], delivery['thread_id'])

    @tasks.loop(seconds=30)
    async def lifecycle(self):
        for row in self.store.active_events():
            try:
                await self.reconcile(row['raid_id'])
            except Exception:
                log.exception('Event reconciliation failed; continuing other events')
        await self.deliver_announcements()
        await self.scan_watches()
        network = self.bot.get_cog('NetworkCog')
        if network:
            await network.tick()

    @lifecycle.before_loop
    async def before_lifecycle(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='event-setup', description='Choose the event forum, temporary voice category and timings.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def setup_command(self, interaction: discord.Interaction, forum: discord.ForumChannel,
                            voice_category: discord.CategoryChannel | None = None,
                            duration_minutes: app_commands.Range[int, 15, 1440] = 180,
                            lock_delay_minutes: app_commands.Range[int, 0, 10080] = 60):
        await interaction.response.defer(ephemeral=True)
        settings = dict(guild_id=interaction.guild_id, forum_id=forum.id, voice_category_id=voice_category.id if voice_category else 0)
        try:
            await self.validate_destinations(settings, require_voice=voice_category is not None)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.store.configure(interaction.guild_id, forum.id, voice_category.id if voice_category else 0,
                             duration_minutes * 60, lock_delay_minutes * 60)
        await interaction.followup.send(f'Events will use {forum.mention}, last {duration_minutes} minutes, and lock {lock_delay_minutes} minutes after ending.', ephemeral=True)

    @app_commands.command(name='event-panel', description='Post and pin a persistent event creation panel.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.choices(preset=[app_commands.Choice(name=p['title'], value=k) for k, p in PRESETS.items()])
    async def panel_command(self, interaction: discord.Interaction, preset: app_commands.Choice[str]):
        if not self.store.settings(interaction.guild_id):
            await interaction.response.send_message('Run /event-setup first.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        network=self.bot.get_cog('NetworkCog')
        network.queue_panel(f'panel:{interaction.id}',interaction.channel_id,interaction.guild_id,preset.value)
        await network.process_panels()
        await interaction.followup.send('Creation panel queued. Failed deliveries retry automatically; panels move below new conversation.',ephemeral=True)

    @app_commands.command(name='event-share', description='Create or revoke the code other servers use to sync this raid.')
    @app_commands.guild_only()
    async def share_command(self, interaction: discord.Interaction, revoke: bool = False):
        event = await self.command_event(interaction)
        if not event:
            return
        if self.store.ended(event, time.time()):
            await interaction.response.send_message('This event has ended.', ephemeral=True)
            return
        if revoke:
            self.store.write('UPDATE SharedEvents SET share_token=NULL WHERE raid_id=?', (event['raid_id'],))
            message = 'Sharing code revoked. Existing synchronized servers remain connected.'
        else:
            token = event['share_token'] or self.store.share(event['raid_id'])
            message = f'Give this code to a partner server’s raid leader: `/event-sync code:{token}`\nOr post this keyword in a watched channel: `mirrorkey:{token}`\nIt grants access to this event’s roster and announcements. This code stays valid until explicitly revoked.'
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name='event-sync', description='Sync a shared raid into this server’s event forum.')
    @app_commands.guild_only()
    async def sync_command(self, interaction: discord.Interaction, code: str):
        if not self.raids.calendar_cog.is_raid_leader(interaction.user, interaction.guild):
            await interaction.response.send_message('A server administrator or configured raid leader must sync events.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            thread_id, created = await self.sync_event(interaction.guild_id, code)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f'Could not sync event: {exc}', ephemeral=True)
            return
        await interaction.followup.send(f"{'Synced' if created else 'Already synced'}: <#{thread_id}>", ephemeral=True)

    @app_commands.command(name='event-watch', description='Auto-sync mirrorkey codes posted in a source channel.')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def watch_command(self, interaction: discord.Interaction, source_channel_id: str, enabled: bool = True):
        if not source_channel_id.isdecimal():
            await interaction.response.send_message('Paste the numeric source channel ID (Developer Mode → Copy Channel ID).', ephemeral=True)
            return
        source_id = int(source_channel_id)
        if not enabled:
            self.store.write('DELETE FROM EventWatches WHERE guild_id=? AND source_channel_id=?', (interaction.guild_id, source_id))
            self.store.write('DELETE FROM WatchFailures WHERE guild_id=? AND source_id=?',(interaction.guild_id,source_id))
            self.store.write('DELETE FROM PendingPublications WHERE guild_id=? AND source_id=?',(interaction.guild_id,source_id))
            await interaction.response.send_message('Watcher removed. Existing synced events remain.', ephemeral=True)
            return
        if not self.store.settings(interaction.guild_id):
            await interaction.response.send_message('Run /event-setup first.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await self.channel(source_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                raise ValueError('Watch a text channel or an individual thread.')
            member = channel.guild.get_member(interaction.user.id) or await channel.guild.fetch_member(interaction.user.id)
            for principal in (member, channel.guild.me):
                perms = channel.permissions_for(principal)
                if not perms.view_channel or not perms.read_message_history:
                    raise ValueError('You and the bot both need access and Read Message History in the source channel.')
            latest = [m async for m in channel.history(limit=1)]
            cursor = latest[0].id if latest else discord.utils.time_snowflake(discord.utils.utcnow())
            self.store.write('INSERT OR IGNORE INTO EventWatches VALUES (?,?,?)', (interaction.guild_id, source_id, cursor))
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f'Could not add watcher: {exc}', ephemeral=True)
            return
        await interaction.followup.send(f'Watching <#{source_id}> for new `mirrorkey:<key>` messages. Repeated keys will not duplicate events.', ephemeral=True)

    @app_commands.command(name='event-announce', description='Send an announcement to every synchronized event thread.')
    @app_commands.guild_only()
    async def announce_command(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 1800] | None = None):
        event = await self.command_event(interaction)
        if not event:
            return
        if self.store.lock_due(event, time.time()):
            await interaction.response.send_message('This event is closed.', ephemeral=True)
            return
        planning = self.bot.get_cog('PlanningCog')
        if message is None:
            template = planning.template(event, 'announcement') if planning else None
            if not template:
                await interaction.response.send_message('Supply a message or set an announcement template.', ephemeral=True)
                return
            try:
                message = planning.render(event, template['body'])
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        self.store.queue_announcement(event['raid_id'], message, interaction.user.id)
        await self.deliver_announcements()
        await interaction.followup.send('Announcement queued for all linked threads; failed deliveries retry automatically.', ephemeral=True)

    @app_commands.command(name='event-duration', description='Set this event’s duration in minutes.')
    @app_commands.guild_only()
    async def duration_command(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 15, 1440]):
        event = await self.command_event(interaction)
        if not event:
            return
        if self.store.ended(event, time.time()):
            await interaction.response.send_message('Ended events cannot be extended. Create a new event.', ephemeral=True)
            return
        self.store.write('UPDATE SharedEvents SET duration=?, revision=revision+1 WHERE raid_id=?', (minutes * 60, event['raid_id']))
        await interaction.response.defer(ephemeral=True)
        await self.reconcile(event['raid_id'])
        guild = self.bot.get_guild(event['guild_id'])
        if guild:
            await self.raids.calendar_cog.modify_guild_event(guild, event['raid_id'])
        await interaction.followup.send(f'Event duration is now {minutes} minutes.', ephemeral=True)

    async def cog_app_command_error(self, interaction, error):
        log.error('Event command failed', exc_info=error)
        text = 'The command could not complete. Check permissions and the bot logs.'
        if isinstance(error, app_commands.MissingPermissions):
            text = 'You need Manage Server permission for that command.'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class CreationView(discord.ui.View):
    def __init__(self, cog, preset):
        super().__init__(timeout=None)
        for index, template in enumerate(PRESETS[preset]['buttons']):
            self.add_item(CreateButton(cog, preset, index, template))


class CreateButton(discord.ui.Button):
    def __init__(self, cog, preset, index, template):
        super().__init__(label=template['label'], style=discord.ButtonStyle.primary,
                         custom_id=f'event-create:{preset}:{index}')
        self.cog, self.template = cog, template

    async def callback(self, interaction):
        if not self.cog.raids.calendar_cog.is_raid_leader(interaction.user, interaction.guild):
            await interaction.response.send_message('Ask an administrator to give you the configured raid leader role.', ephemeral=True)
            return
        await interaction.response.send_modal(CreateEventModal(self.cog, self.template))


class CreateEventModal(discord.ui.Modal):
    def __init__(self, cog, template):
        super().__init__(title='Create event')
        self.cog, self.template = cog, template
        self.name = discord.ui.TextInput(label='Title', default=template['name'], max_length=80)
        self.when = discord.ui.TextInput(label='Start time', placeholder='Friday 8pm (your configured timezone)', max_length=64)
        self.tier = discord.ui.TextInput(label='Tier (optional)', placeholder='T1–T5', required=False, max_length=3)
        self.duration = discord.ui.TextInput(label='Duration in minutes (blank = server default)', required=False, max_length=4)
        self.aim = discord.ui.TextInput(label='Description (optional)', required=False, max_length=1000, style=discord.TextStyle.paragraph)
        for field in (self.name, self.when, self.tier, self.duration, self.aim):
            self.add_item(field)

    async def on_submit(self, interaction):
        if not self.cog.raids.calendar_cog.is_raid_leader(interaction.user, interaction.guild):
            await interaction.response.send_message('You no longer have permission to create events.', ephemeral=True)
            return
        try:
            timestamp = Time().converter(self.cog.bot, interaction.guild_id, interaction.user.id, str(self.when))
            if not time.time() < timestamp <= time.time() + 31536000:
                raise ValueError('Choose a future start within one year.')
            tier = str(self.tier).strip().upper()
            if tier and tier not in ('T1', 'T2', 'T2C', 'T3', 'T4', 'T5'):
                raise ValueError('Tier must be T1, T2, T2c, T3, T4 or T5.')
            minutes = int(str(self.duration)) if str(self.duration).strip() else None
            if minutes is not None and not 15 <= minutes <= 1440:
                raise ValueError('Duration must be 15–1440 minutes.')
        except (ValueError, commands.BadArgument) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            raid_id = await self.cog.raids.post_raid(str(self.name), tier, str(self.aim), timestamp,
                bool(tier and int(tier[1]) > 2), interaction.guild_id, interaction.channel, interaction.user.id,
                size=self.template['size'], duration=minutes * 60 if minutes else None)
            event = self.cog.store.event(raid_id)
            await interaction.followup.send(f"Created <#{event['channel_id']}>", ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f'Could not create event: {exc}', ephemeral=True)


async def setup(bot):
    await bot.add_cog(EventsCog(bot))
