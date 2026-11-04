"""Creation panels, forum/voice lifecycle and cross-guild projections."""
import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

import discord
import emoji as Emoji
import pytz
from discord import app_commands
from discord.ext import commands, tasks

from thread_follow import unfollow_all
from database import upsert
from event_store import EventStore, forum_title, mirror_keys, extract_origin_badge, InvalidShare
from time_cog import Time, time_zone_autocomplete

log = logging.getLogger(__name__)
PRESETS = json.loads(Path(__file__).with_name('event_panels.json').read_text())
DESCRIPTION_SEP = '----------------------------------------------'


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

    async def validate_destinations(self, settings, require_voice=True, forum_id=None):
        forum = await self.channel(forum_id or settings['forum_id'])
        if require_voice and not settings['voice_category_id']:
            raise ValueError('Hosting events requires a voice category. Set it with /setup.')
        category = await self.channel(settings['voice_category_id']) if require_voice else None
        if not isinstance(forum, discord.ForumChannel) or (require_voice and not isinstance(category, discord.CategoryChannel)):
            raise ValueError('Configure an existing forum and voice category with /setup.')
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

    async def create_origin(self, guild_id, name, tier, timestamp, size=12):
        settings = self.store.settings(guild_id)
        if not settings:
            return None
        target_forum_id = self.store.select_forum_id(settings, size=size, tier=tier)
        forum = await self.validate_destinations(settings, forum_id=target_forum_id)
        event = dict(name=name, tier=tier, time=timestamp)
        result = await forum.create_thread(name=self.title(event, guild_id),
            content='Preparing event…', allowed_mentions=discord.AllowedMentions.none(),
            auto_archive_duration=10080, reason='Create raid forum')
        return result.thread, result.message, settings

    @staticmethod
    def _append_template_section(lines, template_text):
        lines.extend(['', DESCRIPTION_SEP, '', template_text.strip(), '', DESCRIPTION_SEP])

    def format_event_description(self, event, template_text=None, invite_url=None, code=None):
        header = f"{event['name']} {event['tier']}".strip() if event.get('tier') else event['name']
        lines = [header]
        if event.get('boss'):
            lines.append(event['boss'])
        template = (template_text or '').strip()
        if template:
            self._append_template_section(lines, template)
        else:
            lines.extend(['', DESCRIPTION_SEP])
        guild = self.bot.get_guild(event['guild_id'])
        kinship = guild.name if guild and getattr(guild, 'name', None) else 'host'
        lines.append('')
        lines.append(f'Not in the {kinship} discord? join here!')
        lines.append(invite_url if invite_url else 'Invite unavailable. Ask the organizer to set server_invite_url in /setup in the owning server.')
        if code:
            lines.append('')
            lines.append(f'mirrorkey:{code}')
        return '\n'.join(lines)

    async def resolve_template_from_link(self, guild, message_link: str) -> str:
        message_link = message_link.strip()
        m = re.search(r'discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)', message_link)
        if m:
            link_guild_id, link_channel_id, link_message_id = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if link_guild_id != guild.id:
                raise ValueError('The template message link must be from a channel in this server.')
            try:
                channel = await self.channel(link_channel_id)
                msg = await channel.fetch_message(link_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                raise ValueError(
                    'Could not fetch that message. Ensure the bot has View Channel and Read Message History in that channel.'
                ) from exc
            parts = []
            if getattr(msg, 'content', None) and msg.content.strip():
                parts.append(msg.content.strip())
            for emb in getattr(msg, 'embeds', None) or ():
                if getattr(emb, 'description', None) and emb.description.strip():
                    parts.append(emb.description.strip())
            body = '\n\n'.join(parts).strip()
            if not body:
                raise ValueError('The linked message has no text content to copy.')
        else:
            body = message_link
        if len(body) > 2500:
            raise ValueError('The template text must be 2500 characters or fewer.')
        return body

    def guild_description_template_body(self, guild_id):
        row = self.store.one("SELECT body FROM EventTemplates WHERE guild_id=? AND kind='description'", (guild_id,))
        if row:
            return row['body']
        legacy = self.store.one("SELECT body FROM EventTemplates WHERE guild_id=? AND kind='footer'", (guild_id,))
        return legacy['body'] if legacy else None

    async def persist_description_template(self, guild_id, body: str | None):
        """Save or clear the server event description template and refresh active events."""
        if body:
            self.store.write("DELETE FROM EventTemplates WHERE guild_id=? AND kind='footer'", (guild_id,))
            self.store.write(
                'INSERT OR REPLACE INTO EventTemplates VALUES (?,?,?)',
                (guild_id, 'description', body),
            )
        else:
            self.store.write(
                "DELETE FROM EventTemplates WHERE guild_id=? AND kind IN ('description', 'footer')",
                (guild_id,),
            )
        self.store.write(
            'UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)',
            (guild_id,),
        )
        for r in self.store.rows('SELECT raid_id FROM Raids WHERE guild_id=?', (guild_id,)):
            if self.store.event(r['raid_id']):
                await self.refresh(r['raid_id'])

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
        code = event['share_token'] or (None if ended else self.store.share(raid_id))
        if code and not event['share_token']:
            event['share_token'] = code
        embed.add_field(name='Ends', value=f"<t:{event['time'] + event['duration']}:F>", inline=False)
        embed.set_footer(text='Cancelled' if event['cancelled'] else ('Event ended' if ended else 'Shared roster • Sign up from any linked server'))
        roster_cards = []
        planning = self.bot.get_cog('PlanningCog')
        if planning:
            plan = planning.plan(raid_id)
            if plan:
                roster_cards, _ = planning.split_cards(event, plan)
        # Later fields (voice and invite links) are added after the description is sized.
        cards_room = sum(len(card) for card in roster_cards) + (300 if roster_cards else 0)
        for copy in self.store.copies(raid_id):
            if copy['locked'] or copy['revision'] == event['revision']:
                continue
            try:
                thread = await self.channel(copy['thread_id'])
                await thread.edit(name=self.title(event, copy['guild_id']), archived=False)
                local_embed = embed.copy()
                template_body = self.guild_description_template_body(event['guild_id'])
                invite_row = self.store.one('SELECT url FROM SourceInvites WHERE guild_id=?', (event['guild_id'],))
                invite_url = invite_row['url'] if invite_row else None
                budget = min(4096, 6000 - cards_room - len(local_embed) + (len(local_embed.description or '')))
                if template_body:
                    if planning:
                        try:
                            template_body = planning.render(event, template_body, budget)
                        except ValueError:
                            log.warning(
                                'Could not render event description template for guild %s; using raw template',
                                event['guild_id'],
                                exc_info=True,
                            )
                local_embed.description = self.format_event_description(
                    event,
                    template_text=template_body,
                    invite_url=invite_url,
                    code=code,
                )[:budget]
                owner = next((c for c in self.store.copies(raid_id) if c['guild_id']==event['guild_id']), None)
                if owner and owner['voice_id']:
                    local_embed.add_field(name='Event voice channel', value=f"https://discord.com/channels/{event['guild_id']}/{owner['voice_id']}", inline=False)
                elif not ended:
                    local_embed.add_field(name='Event voice channel', value='Created in the owning server when the event starts.', inline=False)
                if copy['guild_id'] != event['guild_id']:
                    local_embed.add_field(name='Join the event server',value=invite_url if invite_url else 'Invite unavailable. Ask the organizer to set server_invite_url in /setup in the owning server.',inline=False)
                view = None if ended else (CreepView(self.raids) if event['creep'] else RaidView(self.raids, raid_id=raid_id))
                await thread.get_partial_message(copy['message_id']).edit(
                    content=None, embeds=[local_embed, *roster_cards], view=view, allowed_mentions=discord.AllowedMentions.none())
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
        await self.refresh_calendars_for_raid(raid_id)

    async def refresh_calendars_for_raid(self, raid_id):
        if not self.raids or not self.raids.calendar_cog:
            return
        event = self.store.event(raid_id)
        guild_ids = set()
        if event:
            guild_ids.add(event['guild_id'])
        for copy in self.store.copies(raid_id):
            guild_ids.add(copy['guild_id'])
        for guild_id in guild_ids:
            await self.raids.calendar_cog.update_calendar(guild_id)

    async def reconcile(self, raid_id, now=None):
        now = time.time() if now is None else now
        async with self.locks[raid_id]:
            event = self.store.event(raid_id)
            if not event:
                return
            ended = self.store.ended(event, now)
            if not ended:
                if not self.store.one('SELECT 1 FROM SourceInvites WHERE guild_id=?',(event['guild_id'],)):
                    try:
                        settings=self.store.settings(event['guild_id'])
                        forum=await self.channel(settings['forum_id'])
                        invite=await forum.create_invite(max_age=0,max_uses=0,unique=False,reason='Join the owning server for shared events')
                        self.store.write('INSERT OR REPLACE INTO SourceInvites VALUES (?,?)',(event['guild_id'],str(invite.url)))
                        self.store.touch(raid_id)
                    except discord.HTTPException:
                        log.warning('Cannot create owner-server invite for event %s; set server_invite_url in /setup or grant Create Invite',raid_id)
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
                        if event['cancelled']:
                            await unfollow_all(thread, bot=self.bot)
                        await thread.edit(locked=True, archived=True, reason='Event finished')
                        self.store.write('UPDATE EventCopies SET locked=1 WHERE thread_id=?', (copy['thread_id'],))
                except discord.HTTPException:
                    log.exception('Event lifecycle failed for %s/%s; will retry', raid_id, copy['guild_id'])
            await self.refresh_locked(raid_id, now)

    async def sync_event(self, guild_id, code, origin_message=None):
        """One entry point for manual sync and watcher delivery. Idempotent per guild."""
        event = self.store.by_token(code.strip())
        if event and origin_message is not None:
            kin_name, emoji_str = extract_origin_badge(origin_message)
            if kin_name or emoji_str:
                self.store.set_guild_identity(event['guild_id'], kin_name=kin_name, emoji=emoji_str)
                await self.raids.calendar_cog.update_calendar(event['guild_id'])
        settings = self.store.settings(guild_id)
        if event and self.store.one('SELECT 1 FROM EventBlocks WHERE raid_id=? AND guild_id=?', (event['raid_id'],guild_id)):
            raise InvalidShare('This server was disconnected by the organizer.')
        if not event or self.store.ended(event, time.time()):
            raise InvalidShare('Invalid sharing code or the event has ended.')
        if not settings:
            raise ValueError('Run /setup in the destination server first.')
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
            target_forum_id = self.store.select_forum_id(settings, size=event.get('size') or 12, tier=event.get('tier'))
            forum = await self.validate_destinations(settings, require_voice=False, forum_id=target_forum_id)
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

    async def ensure_origin_emoji(self, guild_id, kin_name, emoji_str, icon_url):
        if emoji_str:
            m = re.search(r':(\d+)>$', emoji_str)
            if m and hasattr(self.bot, 'get_emoji'):
                found = self.bot.get_emoji(int(m.group(1)))
                if found:
                    emoji_str = str(found)
                elif hasattr(self.bot, 'fetch_application_emojis'):
                    slug = re.sub(r'[^a-z0-9]+', '_', (kin_name or 'kin').lower()).strip('_') or 'kin'
                    prefix = f"net_{slug[:21].rstrip('_')}_"
                    app_name = f"net_{slug[:21].rstrip('_')}_{m.group(1)[-6:]}"[:32]
                    try:
                        app_emojis = await self.bot.fetch_application_emojis()
                        existing = next((e for e in app_emojis if e.name == app_name or e.name.startswith(prefix)), None)
                        if not existing and icon_url and hasattr(self.bot, 'create_application_emoji') and hasattr(self.bot, 'http_session'):
                            async with self.bot.http_session.get(icon_url) as resp:
                                if resp.ok:
                                    existing = await self.bot.create_application_emoji(name=app_name, image=await resp.read())
                        if existing:
                            emoji_str = str(existing)
                    except Exception:
                        pass
        self.store.set_guild_identity(guild_id, kin_name=kin_name, emoji=emoji_str)
        await self.raids.calendar_cog.update_calendar(guild_id)

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
                            if mirror_keys(message):
                                self.store.write('INSERT OR IGNORE INTO PendingPublications VALUES (?,?,?)',(watch['guild_id'],watch['source_channel_id'],message.id))
                            self.store.write('UPDATE EventWatches SET last_message_id=? WHERE guild_id=? AND source_channel_id=?',(message.id,watch['guild_id'],watch['source_channel_id']))
                            continue
                        # Parse both literal message text and embedded text/fields.
                        kin_name, emoji_str, icon_url = extract_origin_badge(message, self.bot)
                        for code in mirror_keys(message):
                            if kin_name or emoji_str:
                                ev = self.store.by_token(code)
                                if ev:
                                    await self.ensure_origin_emoji(ev['guild_id'], kin_name, emoji_str, icon_url)
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
                except discord.NotFound:
                    self.store.write('DELETE FROM EventWatches WHERE guild_id=? AND source_channel_id=?',
                                     (watch['guild_id'], watch['source_channel_id']))
                except discord.HTTPException:
                    # Do not advance past a transient failure; already-created copies deduplicate.
                    log.exception('Watcher failed for server %s channel %s; will retry',
                                  watch['guild_id'], watch['source_channel_id'])

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild and self.store.one('SELECT 1 FROM EventWatches WHERE source_channel_id=?', (message.channel.id,)):
            await self.scan_watches()

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if self.store.one('SELECT 1 FROM EventWatches WHERE source_channel_id=?', (payload.channel_id,)):
            try:
                channel = await self.channel(payload.channel_id)
                message = await channel.fetch_message(payload.message_id)
            except discord.HTTPException:
                return
            codes = mirror_keys(message)
            if not codes:
                return
            kin_name, emoji_str, icon_url = extract_origin_badge(message, self.bot)
            for watch in self.store.rows('SELECT * FROM EventWatches WHERE source_channel_id=?', (payload.channel_id,)):
                for code in codes:
                    if kin_name or emoji_str:
                        ev = self.store.by_token(code)
                        if ev:
                            await self.ensure_origin_emoji(ev['guild_id'], kin_name, emoji_str, icon_url)
                    try:
                        await self.sync_event(watch['guild_id'], code)
                    except InvalidShare:
                        pass
                    except (ValueError, discord.HTTPException) as exc:
                        self.store.write('INSERT OR REPLACE INTO WatchFailures VALUES (?,?,?,?,?)',
                                         (watch['guild_id'], watch['source_channel_id'], message.id, code, str(exc)))

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

    @staticmethod
    def _missing_perms(perms, required):
        return [label for attr, label in required if not getattr(perms, attr, False)]

    async def resolve_kin_icon(self, guild, raw_icon: str) -> str | None:
        icon_str = raw_icon.strip()
        if Emoji.is_emoji(icon_str):
            return icon_str
        partial = discord.PartialEmoji.from_str(icon_str)
        if partial.is_custom_emoji():
            if hasattr(self.bot, 'get_emoji'):
                found = self.bot.get_emoji(partial.id)
                if found:
                    return str(found)
            if hasattr(self.bot, 'fetch_application_emojis'):
                try:
                    app_emojis = await self.bot.fetch_application_emojis()
                    match = next((e for e in app_emojis if e.id == partial.id or e.name.lower() == partial.name.lower()), None)
                    if not match and hasattr(self.bot, 'create_application_emoji') and hasattr(self.bot, 'http_session'):
                        ext = 'gif' if partial.animated else 'png'
                        url = f'https://cdn.discordapp.com/emojis/{partial.id}.{ext}?size=128'
                        async with self.bot.http_session.get(url) as resp:
                            if resp.ok:
                                safe_name = re.sub(r'[^A-Za-z0-9_]', '_', partial.name)[:32] or 'kin_icon'
                                match = await self.bot.create_application_emoji(name=safe_name, image=await resp.read())
                    if match:
                        return str(match)
                except Exception:
                    pass
            return str(partial)
        short = re.fullmatch(r':?([A-Za-z0-9_]{2,32}):?', icon_str)
        if short:
            name_q = short.group(1).lower()
            for e in getattr(guild, 'emojis', None) or ():
                if e.name.lower() == name_q:
                    return str(e)
            if hasattr(self.bot, 'emojis'):
                for e in self.bot.emojis or ():
                    if e.name.lower() == name_q:
                        return str(e)
            if hasattr(self.bot, 'fetch_application_emojis'):
                try:
                    app_emojis = await self.bot.fetch_application_emojis()
                    match = next((e for e in app_emojis if e.name.lower() == name_q), None)
                    if match:
                        return str(match)
                except Exception:
                    pass
        return None

    def _should_redeploy_panels(self, guild_id, panel_channel_id):
        existing = self.store.rows('SELECT * FROM CreationPanels WHERE guild_id=?', (guild_id,))
        if not existing:
            return True
        if existing[0]['channel_id'] != panel_channel_id:
            return True
        if len(existing) < len(PRESETS):
            return True
        return False

    async def resolve_setup_draft(self, guild, draft):
        async def channel(cid, label, types=()):
            if not cid:
                raise ValueError(f'Choose **{label}** on the setup editor (page 1).')
            ch = guild.get_channel(cid) or await self.bot.fetch_channel(cid)
            if not ch or (getattr(ch, 'guild', None) and ch.guild.id != guild.id):
                raise ValueError(f'**{label}** must belong to this server.')
            if types and ch.type not in types:
                raise ValueError(f'**{label}** must be a {types[0].name.replace("_", " ")} channel.')
            return ch

        async def optional_channel(cid, label, types=()):
            if not cid:
                return None
            ch = await channel(cid, label, types)
            return ch

        async def optional_forum(cid, label):
            if not cid:
                return None
            ch = await optional_channel(cid, label, (discord.ChannelType.forum,))
            if ch and not isinstance(ch, discord.ForumChannel):
                raise ValueError(f'**{label}** must be a forum channel.')
            return ch

        async def optional_role(rid):
            if not rid:
                return None
            role = guild.get_role(rid)
            if role is None:
                role = await guild.fetch_role(rid)
            return role

        raid_forum = await channel(draft.raid_forum_id, 'Raid forum', (discord.ChannelType.forum,))
        if not isinstance(raid_forum, discord.ForumChannel):
            raise ValueError('Raid forum must be a forum channel.')
        announcement = await channel(draft.announcement_id, 'Announcement push channel', (discord.ChannelType.text, discord.ChannelType.news))
        if not isinstance(announcement, discord.TextChannel):
            raise ValueError('Announcement channel must be a text channel.')
        panel = await channel(draft.panel_id, 'Event panel channel', (discord.ChannelType.text, discord.ChannelType.news))
        if not isinstance(panel, discord.TextChannel):
            raise ValueError('Event panel channel must be a text channel.')
        voice = await channel(draft.voice_category_id, 'Voice category', (discord.ChannelType.category,))
        if not isinstance(voice, discord.CategoryChannel):
            raise ValueError('Voice category must be a category.')

        watch = None
        if draft.watch_channel_id:
            watch = await optional_channel(
                draft.watch_channel_id,
                'Watch channel',
                (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.public_thread),
            )
            if watch and not isinstance(watch, (discord.TextChannel, discord.Thread)):
                raise ValueError('Watch channel must be a text channel or thread.')

        calendar = await optional_channel(draft.calendar_id, 'Calendar channel', (discord.ChannelType.text, discord.ChannelType.news)) if draft.calendar_id else None
        if calendar and not isinstance(calendar, discord.TextChannel):
            raise ValueError('Calendar channel must be a text channel.')
        rss = await optional_channel(draft.rss_id, 'RSS channel', (discord.ChannelType.text, discord.ChannelType.news)) if draft.rss_id else None
        if rss and not isinstance(rss, discord.TextChannel):
            raise ValueError('RSS channel must be a text channel.')

        return dict(
            raid_forum=raid_forum,
            announcement_channel=announcement,
            panel_channel=panel,
            voice_category=voice,
            instance_forum=await optional_forum(draft.instance_forum_id, 'Instance forum'),
            t1_forum=await optional_forum(draft.t1_forum_id, 'T1 forum'),
            t2_forum=await optional_forum(draft.t2_forum_id, 'T2 forum'),
            t3_forum=await optional_forum(draft.t3_forum_id, 'T3 forum'),
            t4_forum=await optional_forum(draft.t4_forum_id, 'T4 forum'),
            t5_forum=await optional_forum(draft.t5_forum_id, 'T5 forum'),
            watch_channel=watch,
            leader_role=await optional_role(draft.leader_role_id),
            kin_role=await optional_role(draft.kin_role_id),
            kin_icon=draft.kin_icon.strip() or None,
            calendar_channel=calendar,
            discord_events=draft.discord_events,
            rss_channel=rss,
            server_timezone=draft.server_timezone.strip() or None,
            server_invite_url=draft.server_invite_url.strip() or None,
            duration_minutes=draft.duration_minutes,
            lock_delay_minutes=draft.lock_delay_minutes,
            description_template=draft.description_template,
        )

    async def smoke_test_and_apply_setup(
        self,
        interaction: discord.Interaction,
        raid_forum: discord.ForumChannel,
        announcement_channel: discord.TextChannel,
        panel_channel: discord.TextChannel,
        voice_category: discord.CategoryChannel,
        instance_forum: discord.ForumChannel | None = None,
        t1_forum: discord.ForumChannel | None = None,
        t2_forum: discord.ForumChannel | None = None,
        t3_forum: discord.ForumChannel | None = None,
        t4_forum: discord.ForumChannel | None = None,
        t5_forum: discord.ForumChannel | None = None,
        watch_channel: discord.TextChannel | discord.Thread | None = None,
        leader_role: discord.Role | None = None,
        kin_role: discord.Role | None = None,
        kin_icon: str | None = None,
        calendar_channel: discord.TextChannel | None = None,
        discord_events: bool = False,
        rss_channel: discord.TextChannel | None = None,
        server_timezone: str | None = None,
        server_invite_url: str | None = None,
        duration_minutes: int = 180,
        lock_delay_minutes: int = 60,
        description_template: str | None = None,
    ) -> tuple[bool, discord.Embed]:
        guild = interaction.guild
        me = guild.me
        errors = []
        warnings = []
        checks = []

        def check_forum(forum_obj, label):
            if not isinstance(forum_obj, discord.ForumChannel) or forum_obj.guild.id != guild.id:
                errors.append(f"**{label}:** Must be a Forum Channel in this server.")
                return False
            f_perms = forum_obj.permissions_for(me)
            missing = self._missing_perms(f_perms, [
                ('view_channel', 'View Channel'),
                ('send_messages', 'Send Messages (Create Posts)'),
                ('send_messages_in_threads', 'Send Messages in Posts'),
                ('embed_links', 'Embed Links'),
                ('read_message_history', 'Read Post History'),
                ('manage_threads', 'Manage Posts'),
            ])
            if missing:
                errors.append(f"**{label} ({forum_obj.mention}):** Bot is missing: {', '.join(missing)}.")
            if forum_obj.flags.require_tag:
                errors.append(f"**{label} ({forum_obj.mention}):** Disable *'Require people to select tags when posting'* in channel settings.")
            if not missing and not forum_obj.flags.require_tag:
                checks.append(f"**{label}:** {forum_obj.mention} (permissions & tag settings verified)")
                return True
            return False

        # 1. Default raid forum, then any optional instance or tier forums.
        # Routing is implied: matching tier forum, else instance forum for ≤6-player events, else raid forum.
        if check_forum(raid_forum, 'Raid Forum'):
            f_perms = raid_forum.permissions_for(me)
            if voice_category and not getattr(f_perms, 'create_instant_invite', False) and not server_invite_url:
                warnings.append(f"**Raid Forum ({raid_forum.mention}):** Bot lacks `Create Invite` and no `server_invite_url` was provided; mirrored servers will not see a voice invite link until one is granted.")
        checks.append('**Forum routing:** tier forum if set, else instance forum for ≤6-player events, else the raid forum.')

        if instance_forum is not None:
            check_forum(instance_forum, 'Instance Forum (≤6-man when that tier has no forum)')

        for label, f_obj in (
            ('T1 Forum', t1_forum),
            ('T2 Forum', t2_forum),
            ('T3 Forum', t3_forum),
            ('T4 Forum', t4_forum),
            ('T5 Forum', t5_forum),
        ):
            if f_obj is not None:
                check_forum(f_obj, label)

        # 2. Mandatory: Announcement (Outgoing Discovery Push) Channel
        if not isinstance(announcement_channel, discord.TextChannel) or announcement_channel.guild.id != guild.id:
            errors.append('**Announcement Channel:** Must be a Text or Announcement Channel in this server.')
        else:
            a_perms = announcement_channel.permissions_for(me)
            missing = self._missing_perms(a_perms, [
                ('view_channel', 'View Channel'),
                ('send_messages', 'Send Messages'),
                ('read_message_history', 'Read Message History'),
            ])
            if missing:
                errors.append(f"**Announcement Push Channel ({announcement_channel.mention}):** Bot is missing: {', '.join(missing)}.")
            else:
                checks.append(f"**Announcement Push Channel:** {announcement_channel.mention} (permissions verified)")

        # 3. Mandatory: Event Panel Channel
        if not isinstance(panel_channel, discord.TextChannel) or panel_channel.guild.id != guild.id:
            errors.append('**Event Panel Channel:** Must be a Text Channel in this server.')
        else:
            p_perms = panel_channel.permissions_for(me)
            missing = self._missing_perms(p_perms, [
                ('view_channel', 'View Channel'),
                ('send_messages', 'Send Messages'),
                ('embed_links', 'Embed Links'),
                ('read_message_history', 'Read Message History'),
            ])
            if missing:
                errors.append(f"**Event Panel Channel ({panel_channel.mention}):** Bot is missing: {', '.join(missing)}.")
            else:
                checks.append(f"**Event Panel Channel:** {panel_channel.mention} ({len(PRESETS)} cluster panels in release order)")

        # 4. Mandatory: Voice Category
        if not isinstance(voice_category, discord.CategoryChannel) or voice_category.guild.id != guild.id:
            errors.append('**Voice Category:** Must be a Category in this server.')
        else:
            v_perms = voice_category.permissions_for(me)
            missing = self._missing_perms(v_perms, [
                ('view_channel', 'View Channel'),
                ('manage_channels', 'Manage Channels'),
            ])
            if missing:
                errors.append(f"**Voice Category ({voice_category.name}):** Bot is missing: {', '.join(missing)}.")
            else:
                checks.append(f"**Voice Category:** {voice_category.name} (permissions verified)")

        # 5. Optional: Watch (Incoming Discovery Pull) Channel
        watch_target = None
        if watch_channel is not None:
            if not isinstance(watch_channel, (discord.TextChannel, discord.Thread)) or watch_channel.guild.id != guild.id:
                errors.append('**Event Watch Channel:** Must be a Text/Announcement Channel or Thread in this server.')
            else:
                w_perms = watch_channel.permissions_for(me)
                missing = self._missing_perms(w_perms, [
                    ('view_channel', 'View Channel'),
                    ('read_message_history', 'Read Message History'),
                ])
                if missing:
                    errors.append(f"**Event Watch Channel ({watch_channel.mention}):** Bot is missing: {', '.join(missing)}.")
                else:
                    watch_target = watch_channel
                    checks.append(f"**Event Watch (Pull) Channel:** {watch_channel.mention} (permissions verified)")

        # 6. Optional: Raid Leader Role & Kinship Role / Icon
        if leader_role is not None:
            checks.append(f"**Raid Leader Role:** {leader_role.mention}")
        resolved_kin_icon = None
        if kin_icon is not None and kin_icon.strip():
            resolved_kin_icon = await self.resolve_kin_icon(guild, kin_icon)
            if not resolved_kin_icon:
                errors.append(f"**Kin Icon (`{kin_icon.strip()}`):** Not a valid Unicode or accessible custom emoji.")
            elif not kin_role:
                checks.append(f"**Kin Icon:** {resolved_kin_icon}")
        if kin_role is not None:
            icon_suffix = f" ({resolved_kin_icon})" if resolved_kin_icon else ""
            checks.append(f"**Kinship Role:** {kin_role.mention}{icon_suffix}")

        # 7. Optional: Calendar Channel & Native Discord Events
        if calendar_channel is not None:
            if not isinstance(calendar_channel, discord.TextChannel) or calendar_channel.guild.id != guild.id:
                errors.append('**Calendar Channel:** Must be a Text Channel in this server.')
            else:
                c_perms = calendar_channel.permissions_for(me)
                missing = self._missing_perms(c_perms, [
                    ('view_channel', 'View Channel'),
                    ('send_messages', 'Send Messages'),
                    ('embed_links', 'Embed Links'),
                ])
                if missing:
                    errors.append(f"**Calendar Channel ({calendar_channel.mention}):** Bot is missing: {', '.join(missing)}.")
                else:
                    checks.append(f"**Calendar Channel:** {calendar_channel.mention} (permissions verified)")
        if discord_events:
            if not getattr(me.guild_permissions, 'manage_events', False):
                errors.append('**Discord Scheduled Events (`discord_events=True`):** Bot is missing the server-wide `Manage Events` permission.')
            else:
                checks.append('**Discord Scheduled Events:** Enabled (`Manage Events` verified)')

        # 8. Optional: RSS Channel
        if rss_channel is not None:
            if not isinstance(rss_channel, discord.TextChannel) or rss_channel.guild.id != guild.id:
                errors.append('**RSS Channel:** Must be a Text Channel in this server.')
            else:
                r_perms = rss_channel.permissions_for(me)
                missing = self._missing_perms(r_perms, [
                    ('view_channel', 'View Channel'),
                    ('send_messages', 'Send Messages'),
                    ('embed_links', 'Embed Links'),
                ])
                if missing:
                    errors.append(f"**RSS Channel ({rss_channel.mention}):** Bot is missing: {', '.join(missing)}.")
                else:
                    checks.append(f"**RSS Channel:** {rss_channel.mention} (permissions verified)")

        # 9. Optional: Server Timezone
        resolved_tz = None
        if server_timezone is not None and server_timezone.strip():
            try:
                resolved_tz = str(pytz.timezone(server_timezone.strip()))
                checks.append(f"**Server Timezone:** `{resolved_tz}`")
            except pytz.UnknownTimeZoneError:
                errors.append(f"**Server Timezone (`{server_timezone}`):** Invalid timezone name. Choose a valid IANA timezone (e.g. `America/New_York`).")

        # 10. Optional: Server Invite URL
        verified_invite_url = None
        if server_invite_url is not None and server_invite_url.strip():
            try:
                invite = await self.bot.fetch_invite(server_invite_url.strip())
                if not invite.guild or invite.guild.id != guild.id:
                    errors.append('**Server Invite URL:** The invite must belong to this server.')
                else:
                    verified_invite_url = invite.url
                    checks.append(f"**Server Voice Invite:** {verified_invite_url}")
            except (discord.NotFound, discord.HTTPException):
                errors.append('**Server Invite URL:** Could not verify that invite link. Use a valid, non-expiring Discord invite to this server.')

        if errors:
            embed = discord.Embed(
                title='Setup Smoke Test Failed — Missing Permissions or Invalid Settings',
                colour=discord.Colour(0xe74c3c),
                description=(
                    "Fix the following items in Discord and run `/setup` or `/settings` again:\n\n"
                    + '\n'.join(f"• {e}" for e in errors)
                ),
            )
            if checks:
                embed.add_field(name='Passed Checks', value='\n'.join(f"• {c}" for c in checks)[:1024], inline=False)
            if warnings:
                embed.add_field(name='Notes', value='\n'.join(f"• {w}" for w in warnings)[:1024], inline=False)
            return False, embed

        # All smoke tests passed — persist settings and deploy panels/calendar.
        self.store.configure(
            guild.id,
            raid_forum.id,
            voice_category.id if voice_category else 0,
            duration_minutes * 60,
            lock_delay_minutes * 60,
            instance_forum_id=instance_forum.id if instance_forum else 0,
            t1_forum_id=t1_forum.id if t1_forum else 0,
            t2_forum_id=t2_forum.id if t2_forum else 0,
            t3_forum_id=t3_forum.id if t3_forum else 0,
            t4_forum_id=t4_forum.id if t4_forum else 0,
            t5_forum_id=t5_forum.id if t5_forum else 0,
        )
        self.store.write('INSERT OR REPLACE INTO NetworkSettings VALUES (?,?)', (guild.id, announcement_channel.id))

        prev_watch = self.store.one('SELECT source_channel_id, last_message_id FROM EventWatches WHERE guild_id=?', (guild.id,))
        if watch_target is None:
            self.store.write('DELETE FROM EventWatches WHERE guild_id=?', (guild.id,))
        elif prev_watch and prev_watch['source_channel_id'] == watch_target.id:
            checks.append(f"**Event Watch (Pull) Channel:** {watch_target.mention} (unchanged cursor)")
        else:
            latest = [m async for m in watch_target.history(limit=1)]
            cursor = latest[0].id if latest else discord.utils.time_snowflake(discord.utils.utcnow())
            self.store.write('DELETE FROM EventWatches WHERE guild_id=?', (guild.id,))
            self.store.write('INSERT OR REPLACE INTO EventWatches VALUES (?,?,?)', (guild.id, watch_target.id, cursor))

        if leader_role is not None:
            upsert(self.bot.conn, 'Settings', ['raid_leader'], [leader_role.id], ['guild_id'], [guild.id])
        if kin_role is not None:
            upsert(self.bot.conn, 'Settings', ['priority'], [kin_role.id], ['guild_id'], [guild.id])
        if resolved_kin_icon is not None:
            old_icon = self.store.one('SELECT icon FROM Settings WHERE guild_id=?', (guild.id,))
            upsert(self.bot.conn, 'Settings', ['icon'], [resolved_kin_icon], ['guild_id'], [guild.id])
            if old_icon and old_icon.get('icon') and old_icon['icon'] != resolved_kin_icon:
                for p in self.store.rows('SELECT raid_id, player_id, byname FROM Players WHERE byname LIKE ?', (f"{old_icon['icon']}%",)):
                    new_byname = resolved_kin_icon + p['byname'][len(old_icon['icon']):]
                    self.bot.conn.execute('UPDATE Players SET byname=? WHERE raid_id=? AND player_id=?', (new_byname, p['raid_id'], p['player_id']))
        elif kin_role is not None:
            existing_icon = self.store.one('SELECT icon FROM Settings WHERE guild_id=?', (guild.id,))
            if not existing_icon or not existing_icon.get('icon'):
                ident = self.store.one('SELECT emoji FROM GuildIdentity WHERE guild_id=?', (guild.id,))
                if ident and ident.get('emoji'):
                    upsert(self.bot.conn, 'Settings', ['icon'], [ident['emoji']], ['guild_id'], [guild.id])
        if rss_channel is not None:
            upsert(self.bot.conn, 'Settings', ['rss'], [rss_channel.id], ['guild_id'], [guild.id])
        else:
            upsert(self.bot.conn, 'Settings', ['rss'], [None], ['guild_id'], [guild.id])
        if resolved_tz is not None:
            upsert(self.bot.conn, 'Settings', ['server'], [resolved_tz], ['guild_id'], [guild.id])
        upsert(self.bot.conn, 'Settings', ['guild_events'], [discord_events], ['guild_id'], [guild.id])
        if verified_invite_url is not None:
            self.store.write('INSERT OR REPLACE INTO SourceInvites VALUES (?,?)', (guild.id, verified_invite_url))
            self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)', (guild.id,))
        if description_template is not None:
            await self.persist_description_template(guild.id, description_template or None)
            if description_template:
                checks.append('**Event description template:** updated from message link')
            else:
                checks.append('**Event description template:** cleared')
        self.bot.conn.commit()

        network = self.bot.get_cog('NetworkCog')
        if network and self._should_redeploy_panels(guild.id, panel_channel.id):
            await network.deploy_all_panels(guild.id, panel_channel.id)
        elif network:
            checks.append(f"**Event Panel Channel:** {panel_channel.mention} (panels unchanged)")

        prev_cal = self.store.one('SELECT calendar FROM Settings WHERE guild_id=?', (guild.id,))
        old_cal_channel = None
        if prev_cal and prev_cal.get('calendar') and '/' in str(prev_cal['calendar']):
            old_cal_channel = int(str(prev_cal['calendar']).split('/')[0])
        if calendar_channel is None:
            if old_cal_channel:
                upsert(self.bot.conn, 'Settings', ['calendar'], [None], ['guild_id'], [guild.id])
        elif self.raids and self.raids.calendar_cog:
            if old_cal_channel != calendar_channel.id:
                await self.raids.calendar_cog.post_calendar(guild.id, calendar_channel)
            else:
                checks.append(f"**Calendar Channel:** {calendar_channel.mention} (unchanged)")

        checks.append(f"**Event Timings:** {duration_minutes}m default duration, {lock_delay_minutes}m post-event lock delay")
        embed = discord.Embed(
            title='Server Setup Complete — All Permission Smoke Tests Passed',
            colour=discord.Colour(0x2ecc71),
            description='\n'.join(f"• {c}" for c in checks),
        )
        if warnings:
            embed.add_field(name='Notes', value='\n'.join(f"• {w}" for w in warnings)[:1024], inline=False)
        return True, embed

    @app_commands.command(name='setup', description='Configure the raid forum, optional instance and tier forums, panels, and roles.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(server_timezone=time_zone_autocomplete)
    @app_commands.describe(
        raid_forum='Default forum. Used when no matching tier forum is set, and for raids when no instance forum applies.',
        announcement_channel='Mandatory: Outgoing channel where mirrorkey discovery codes are posted.',
        panel_channel='Mandatory: Channel where sticky cluster raid creation panels are posted.',
        voice_category='Mandatory: Category where temporary raid voice channels are created at start time.',
        instance_forum='Optional: ≤6-player events whose tier has no forum of its own.',
        t1_forum='Optional: Tier 1 events (instances and raids) go here instead of the instance or default forum.',
        t2_forum='Optional: Tier 2 / T2c events.',
        t3_forum='Optional: Tier 3 events.',
        t4_forum='Optional: Tier 4 events.',
        t5_forum='Optional: Tier 5 events.',
        watch_channel='Optional: Channel to watch for incoming mirrorkey codes (e.g. a channel following partner announcements).',
        leader_role='Optional: Role permitted to create events, edit raids, and manage rosters.',
        kin_role='Optional: Role used to distinguish kinship members on raid signups.',
        kin_icon='Optional: Emoji badge displayed next to kinship members on raid signups.',
        calendar_channel='Optional: Channel where the live auto-updating raid calendar embed is posted.',
        discord_events='Optional: Also create native Discord Scheduled Events for hosted raids.',
        rss_channel='Optional: Channel where official LOTRO forum RSS announcements are posted.',
        server_timezone='Optional: Default server timezone (e.g. America/New_York).',
        server_invite_url='Optional: Non-expiring invite URL to this server for cross-server voice links.',
        duration_minutes='Optional: Default raid duration in minutes (15–1440, default 180).',
        lock_delay_minutes='Optional: Minutes after event end before locking forum threads (0–10080, default 60).',
        description_message_link='Optional: Discord message link copied into the reusable event description template (see `/settings`).',
    )
    async def setup_command(
        self,
        interaction: discord.Interaction,
        raid_forum: discord.ForumChannel,
        announcement_channel: discord.TextChannel,
        panel_channel: discord.TextChannel,
        voice_category: discord.CategoryChannel,
        instance_forum: discord.ForumChannel | None = None,
        t1_forum: discord.ForumChannel | None = None,
        t2_forum: discord.ForumChannel | None = None,
        t3_forum: discord.ForumChannel | None = None,
        t4_forum: discord.ForumChannel | None = None,
        t5_forum: discord.ForumChannel | None = None,
        watch_channel: discord.TextChannel | discord.Thread | None = None,
        leader_role: discord.Role | None = None,
        kin_role: discord.Role | None = None,
        kin_icon: str | None = None,
        calendar_channel: discord.TextChannel | None = None,
        discord_events: bool = False,
        rss_channel: discord.TextChannel | None = None,
        server_timezone: str | None = None,
        server_invite_url: str | None = None,
        duration_minutes: app_commands.Range[int, 15, 1440] = 180,
        lock_delay_minutes: app_commands.Range[int, 0, 10080] = 60,
        description_message_link: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        description_template = None
        if description_message_link is not None and description_message_link.strip():
            description_template = await self.resolve_template_from_link(interaction.guild, description_message_link)
        _, embed = await self.smoke_test_and_apply_setup(
            interaction=interaction,
            raid_forum=raid_forum,
            announcement_channel=announcement_channel,
            panel_channel=panel_channel,
            voice_category=voice_category,
            instance_forum=instance_forum,
            t1_forum=t1_forum,
            t2_forum=t2_forum,
            t3_forum=t3_forum,
            t4_forum=t4_forum,
            t5_forum=t5_forum,
            watch_channel=watch_channel,
            leader_role=leader_role,
            kin_role=kin_role,
            kin_icon=kin_icon,
            calendar_channel=calendar_channel,
            discord_events=discord_events,
            rss_channel=rss_channel,
            server_timezone=server_timezone,
            server_invite_url=server_invite_url,
            duration_minutes=duration_minutes,
            lock_delay_minutes=lock_delay_minutes,
            description_template=description_template,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name='settings', description='Open the paged server settings editor (prefilled from saved configuration).')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def settings_command(self, interaction: discord.Interaction):
        from setup_editor import SetupEditorView, load_setup_draft
        draft = load_setup_draft(self.store, self.bot.conn, interaction.guild_id)
        view = SetupEditorView(self, interaction.user.id, draft)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

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
                         custom_id=f'event-create:{preset}:{index}', row=index // 5)
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
            creep = bool(self.template.get('creep', False))
            raid_id = await self.cog.raids.post_raid(str(self.name), tier, str(self.aim), timestamp,
                bool(tier and int(tier[1]) > 2) and not creep, interaction.guild_id, interaction.channel, interaction.user.id,
                creep=creep, size=self.template['size'], duration=minutes * 60 if minutes else None)
            event = self.cog.store.event(raid_id)
            await interaction.followup.send(f"Created <#{event['channel_id']}>", ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f'Could not create event: {exc}', ephemeral=True)


async def setup(bot):
    await bot.add_cog(EventsCog(bot))
