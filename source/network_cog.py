"""Discovery publication, retries, repair and operator controls."""
import asyncio
import io
import time
import discord
from discord import app_commands
from discord.ext import commands
from event_store import InvalidShare, mirror_keys


class NetworkCog(commands.Cog):
    def __init__(self, bot):
        self.bot=bot
        self.lock=asyncio.Lock()
        self.panel_tasks={}
        self.invite_check_at=0
        from durable_send import DurableSender
        self.sender=DurableSender(bot.conn,bot)
        bot.conn.execute('CREATE TABLE IF NOT EXISTS PanelJobs(key TEXT PRIMARY KEY,channel_id INTEGER,guild_id INTEGER,preset TEXT,old_id INTEGER)')
        bot.conn.commit()

    @property
    def events(self): return self.bot.get_cog('EventsCog')
    @property
    def store(self): return self.events.store

    async def tick(self):
        async with self.lock:
            await self.process_panels()
            if time.time()>=self.invite_check_at:
                self.invite_check_at=time.time()+3600
                for row in self.store.rows('SELECT * FROM SourceInvites'):
                    try:
                        invite=await self.bot.fetch_invite(row['url'])
                        if invite.guild and invite.guild.id==row['guild_id']:continue
                    except discord.NotFound:pass
                    except discord.HTTPException:continue
                    self.store.write('DELETE FROM SourceInvites WHERE guild_id=?',(row['guild_id'],))
                    self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)',(row['guild_id'],))
            for pending in self.store.rows('SELECT * FROM PendingPublications'):
                args=(pending['guild_id'],pending['source_id'],pending['message_id'])
                try:
                    channel=await self.events.channel(pending['source_id'])
                    message=await channel.fetch_message(pending['message_id'])
                    if not message.flags.crossposted:
                        # Expired or revoked codes no longer need publication polling.
                        active=[self.store.by_token(code) for code in mirror_keys(message.content)]
                        if any(e and not self.store.ended(e,time.time()) for e in active):continue
                    else:
                        for code in mirror_keys(message.content):
                            try:await self.events.sync_event(pending['guild_id'],code)
                            except InvalidShare:pass
                            except (ValueError,discord.HTTPException) as exc:
                                self.store.write('INSERT OR REPLACE INTO WatchFailures VALUES (?,?,?,?,?)',(*args,code,str(exc)))
                except discord.NotFound:pass
                except discord.HTTPException:continue
                self.store.write('DELETE FROM PendingPublications WHERE guild_id=? AND source_id=? AND message_id=?',args)
            for row in self.store.rows('SELECT * FROM WatchFailures'):
                try:
                    await self.events.sync_event(row['guild_id'],row['code'])
                except InvalidShare:
                    pass
                except (ValueError,discord.HTTPException) as exc:
                    self.store.write('UPDATE WatchFailures SET error=? WHERE guild_id=? AND source_id=? AND message_id=? AND code=?', (str(exc),row['guild_id'],row['source_id'],row['message_id'],row['code']))
                    continue
                self.store.write('DELETE FROM WatchFailures WHERE guild_id=? AND source_id=? AND message_id=? AND code=?',(row['guild_id'],row['source_id'],row['message_id'],row['code']))
            for row in self.store.rows('''SELECT e.raid_id,n.push_id FROM SharedEvents e JOIN Raids r USING(raid_id)
                JOIN NetworkSettings n ON n.guild_id=r.guild_id LEFT JOIN NetworkPush p USING(raid_id)
                WHERE n.push_id IS NOT NULL AND p.message_id IS NULL'''):
                event=self.store.event(row['raid_id'])
                if self.store.ended(event,time.time()): continue
                try:
                    code=event['share_token'] or self.store.share(row['raid_id'])
                    channel=await self.events.channel(row['push_id'])
                    # Intentionally do not call Message.publish(): crossposting is a human decision.
                    message=await self.sender.send(channel,f"discovery:{row['raid_id']}:{row['push_id']}",f"{event['name']} — <t:{event['time']}:F>\nmirrorkey:{code}")
                    self.store.write('INSERT OR REPLACE INTO NetworkPush VALUES (?,?,NULL)',(row['raid_id'],message.id))
                except (ValueError,discord.HTTPException) as exc:
                    self.store.write('INSERT OR REPLACE INTO NetworkPush VALUES (?,NULL,?)',(row['raid_id'],str(exc)))

    @commands.Cog.listener()
    async def on_message(self,message):
        if message.author.bot or not message.guild:return
        if not self.store.one('SELECT 1 FROM CreationPanels WHERE channel_id=?',(message.channel.id,)):return
        task=self.panel_tasks.get(message.channel.id)
        if task and not task.done():return
        async def bump():
            await asyncio.sleep(5)
            for panel in self.store.rows('SELECT * FROM CreationPanels WHERE channel_id=?',(message.channel.id,)):
                self.queue_panel(f"bump:{panel['message_id']}",message.channel.id,message.guild.id,panel['preset'],panel['message_id'])
            await self.process_panels()
        self.panel_tasks[message.channel.id]=asyncio.create_task(bump())

    def queue_panel(self,key,channel_id,guild_id,preset,old_id=None):
        self.store.write('INSERT OR IGNORE INTO PanelJobs VALUES (?,?,?,?,?)',(key,channel_id,guild_id,preset,old_id))

    async def process_panels(self):
        from events_cog import CreationView, PRESETS
        for job in self.store.rows('SELECT * FROM PanelJobs'):
            try:
                channel=await self.events.channel(job['channel_id']);preset=PRESETS[job['preset']]
                view=CreationView(self.events,job['preset'])
                message=await self.sender.send(channel,job['key'],None,embed=discord.Embed(title=preset['title'],description=preset['description']),view=view)
                with self.bot.conn:
                    if job['old_id']:self.bot.conn.execute('DELETE FROM CreationPanels WHERE message_id=?',(job['old_id'],))
                    self.bot.conn.execute('INSERT OR REPLACE INTO CreationPanels VALUES (?,?,?,?)',(message.id,job['channel_id'],job['guild_id'],job['preset']))
                self.bot.add_view(view,message_id=message.id)
                if job['old_id']:
                    try:await channel.get_partial_message(job['old_id']).delete()
                    except discord.NotFound:pass
                self.store.write('DELETE FROM PanelJobs WHERE key=?',(job['key'],))
            except (discord.HTTPException,ValueError):
                import logging
                logging.getLogger(__name__).exception('Panel job retained for retry')

    async def cog_unload(self):
        for task in self.panel_tasks.values():task.cancel()

    @app_commands.command(name='event-reconnect',description='Allow a disconnected server to sync again using a fresh share code.')
    @app_commands.guild_only()
    async def reconnect(self,interaction:discord.Interaction,guild_id:str):
        event=await self.events.command_event(interaction)
        if not event:return
        if not guild_id.isdecimal():
            await interaction.response.send_message('Use a numeric server ID.',ephemeral=True);return
        self.store.write('DELETE FROM EventBlocks WHERE raid_id=? AND guild_id=?',(event['raid_id'],int(guild_id)))
        code=event['share_token'] or self.store.share(event['raid_id'])
        await interaction.response.send_message(f'Allowed reconnection. Destination administrator: /event-repair code:{code}',ephemeral=True)

    @app_commands.command(name='network-push',description='Set the channel for automatic event discovery announcements, or disable publishing.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def push(self, interaction:discord.Interaction, channel:discord.TextChannel|None=None):
        if channel:
            perms=channel.permissions_for(interaction.guild.me)
            if not perms.view_channel or not perms.send_messages:
                await interaction.response.send_message('Bot needs View Channel and Send Messages.',ephemeral=True);return
        self.store.write('INSERT OR REPLACE INTO NetworkSettings VALUES (?,?)',(interaction.guild_id,channel.id if channel else None))
        await interaction.response.send_message('Push channel saved. Active unpublished events will publish on the next cycle. Configure pull independently with /event-watch.' if channel else 'Automatic publishing disabled.',ephemeral=True)

    @app_commands.command(name='event-status',description='Inspect linked servers, stale updates and pending deliveries; optionally retry.')
    @app_commands.guild_only()
    async def status(self, interaction:discord.Interaction,retry:bool=False):
        event=await self.events.command_event(interaction)
        if not event:return
        await interaction.response.defer(ephemeral=True)
        if retry:
            self.store.write('UPDATE EventCopies SET revision=0 WHERE raid_id=?',(event['raid_id'],))
            await self.events.reconcile(event['raid_id'])
            await self.events.deliver_announcements()
            await self.tick()
            planning=self.bot.get_cog('PlanningCog')
            if planning:await planning.sync(event['raid_id'])
        lines=[f"Event {event['raid_id']}, revision {event['revision']}"]
        for c in self.store.copies(event['raid_id']):
            lines.append(f"Server {c['guild_id']}: thread {c['thread_id']}; revision {c['revision']}; locked {bool(c['locked'])}; voice {c['voice_id']}")
        pending=self.store.one('SELECT count(*) AS n FROM AnnouncementDeliveries d JOIN EventAnnouncements a ON a.id=d.announcement_id WHERE a.raid_id=? AND d.sent=0',(event['raid_id'],))
        lines.append(f"Pending announcement deliveries: {pending['n']}")
        if self.bot.get_cog('PlanningCog'):
            migration=self.store.one('SELECT error FROM LegacyMigration WHERE raid_id=?',(event['raid_id'],))
            if migration:lines.append('Legacy migration needs review: '+migration['error'])
            for delivery in self.store.rows('SELECT d.* FROM RosterDelivery d JOIN EventCopies c USING(thread_id) WHERE c.raid_id=?',(event['raid_id'],)):
                lines.append(f"Roster thread {delivery['thread_id']}: last success {delivery['last_success']}; error {delivery['error'] or 'none'}")
        push=self.store.one('SELECT * FROM NetworkPush WHERE raid_id=?',(event['raid_id'],))
        lines.append('Discovery publication: '+str(push or 'not published'))
        await interaction.followup.send(file=discord.File(io.BytesIO('\n'.join(lines).encode()),filename='event-status.txt'),ephemeral=True)

    @app_commands.command(name='network-status',description='Inspect and retry this server’s failed event discoveries.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def network_status(self,interaction:discord.Interaction,retry:bool=False):
        await interaction.response.defer(ephemeral=True)
        if retry:await self.tick()
        rows=self.store.rows('SELECT source_id,message_id,error FROM WatchFailures WHERE guild_id=?',(interaction.guild_id,))
        pending=self.store.one('SELECT count(*) AS n FROM PendingPublications WHERE guild_id=?',(interaction.guild_id,))
        text=('\n'.join(str(r) for r in rows) or 'No failed discoveries.')+f"\nAwaiting manual announcement publication: {pending['n']}"
        await interaction.followup.send(file=discord.File(io.BytesIO(text.encode()),filename='network-status.txt'),ephemeral=True)

    @app_commands.command(name='event-unlink',description='Disconnect a mirror and block it from automatically rejoining.')
    @app_commands.guild_only()
    async def unlink(self,interaction:discord.Interaction,guild_id:str):
        event=await self.events.command_event(interaction,manage=False)
        if not event:return
        if not guild_id.isdecimal():
            await interaction.response.send_message('Use a numeric server ID.',ephemeral=True);return
        target=int(guild_id)
        manager=await self.events.can_manage(interaction,event['raid_id'])
        if not manager and not (target==interaction.guild_id and interaction.user.guild_permissions.manage_guild):
            await interaction.response.send_message('Only the organizer or that server’s administrator can disconnect it.',ephemeral=True);return
        if target==event['guild_id']:
            await interaction.response.send_message('Cannot disconnect the source server.',ephemeral=True);return
        await interaction.response.defer(ephemeral=True)
        c=self.store.one('SELECT * FROM EventCopies WHERE raid_id=? AND guild_id=?',(event['raid_id'],target))
        # Commit disconnection before best-effort Discord cleanup.
        with self.bot.conn:
            self.bot.conn.execute('INSERT OR IGNORE INTO EventBlocks VALUES (?,?)',(event['raid_id'],target))
            self.bot.conn.execute('UPDATE EventCopies SET locked=1 WHERE raid_id=? AND guild_id=?',(event['raid_id'],target))
        if c:
            try:
                thread=await self.events.channel(c['thread_id'])
                await thread.get_partial_message(c['message_id']).edit(content='Disconnected from shared event.',embed=None,view=None)
                await thread.edit(locked=True,archived=True)
            except discord.HTTPException:
                pass
        await interaction.followup.send('Server disconnected and blocked from rejoining this event.',ephemeral=True)

    @app_commands.command(name='event-repair',description='Repair or reconnect a server’s event copy using its sharing code.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def repair(self,interaction:discord.Interaction,code:str):
        await interaction.response.defer(ephemeral=True)
        try:
            thread,_=await self.events.sync_event(interaction.guild_id,code)
            await interaction.followup.send(f'Repaired <#{thread}>.',ephemeral=True)
        except (ValueError,discord.HTTPException) as exc:
            await interaction.followup.send(str(exc)[:1800],ephemeral=True)

    @app_commands.command(name='event-invite',description='Set a verified invite to this owning server for mirrored events.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def invite(self,interaction:discord.Interaction,url:str):
        await interaction.response.defer(ephemeral=True)
        invite=await self.bot.fetch_invite(url)
        if not invite.guild or invite.guild.id!=interaction.guild_id:
            await interaction.followup.send('The invite must lead to this server.',ephemeral=True);return
        self.store.write('INSERT OR REPLACE INTO SourceInvites VALUES (?,?)',(interaction.guild_id,invite.url))
        self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)',(interaction.guild_id,))
        await interaction.followup.send('Owner-server invite saved. Mirrors update on the next refresh. Use a non-expiring invite.',ephemeral=True)

    async def cog_app_command_error(self,interaction,error):
        send=interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send('Could not complete operation: '+str(getattr(error,'original',error))[:1700],ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

async def setup(bot):await bot.add_cog(NetworkCog(bot))
