"""Templates and durable, directly synchronized Vanguard rosters."""
import asyncio
import hashlib
import io
import json
import logging
import re
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from planning import CLASSES, TAGS, context_for, render_template, export_session, import_plan, planner_link, PLANNER_URL, decode_plan_code

log = logging.getLogger(__name__)


class PlanningCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        from durable_send import DurableSender
        self.sender=DurableSender(bot.conn,bot)
        bot.conn.executescript('''
            CREATE TABLE IF NOT EXISTS LegacyMigration(raid_id INTEGER PRIMARY KEY,error TEXT);
            CREATE TABLE IF NOT EXISTS RosterDelivery(thread_id INTEGER PRIMARY KEY,last_success INTEGER,error TEXT);
            CREATE TABLE IF NOT EXISTS EventTemplates (
                guild_id INTEGER, kind TEXT, body TEXT NOT NULL, PRIMARY KEY(guild_id,kind));
            CREATE TABLE IF NOT EXISTS EventPlans (
                raid_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS PlanMessages (
                thread_id INTEGER, page INTEGER, message_id INTEGER, body TEXT NOT NULL,
                PRIMARY KEY(thread_id,page));
        ''')
        bot.conn.commit()

    @property
    def events(self):
        return self.bot.get_cog('EventsCog')

    @property
    def store(self):
        return self.events.store

    async def cog_load(self):
        self.migrate_legacy()
        self.refresh_plans.start()

    def migrate_legacy(self):
        # Never overwrite an existing plan or delete the legacy source rows.
        for row in self.store.rows('SELECT raid_id FROM SharedEvents WHERE raid_id NOT IN (SELECT raid_id FROM EventPlans)'):
            raid_id=row['raid_id'];event=self.store.event(raid_id)
            assigned=self.store.rows('SELECT * FROM Assignment WHERE raid_id=? AND player_id IS NOT NULL ORDER BY slot_id',(raid_id,))
            if not assigned:continue
            try:
                members=[]
                for a in assigned:
                    c=next((c for c in CLASSES if c.replace('-','').lower()==(a['class_name'] or '').replace('-','').lower()),None)
                    spec={1:'Red',2:'Blue',4:'Yellow'}.get(a['spec'])
                    role=dict(CLASSES.get(c,[])).get(spec)
                    if not role:raise ValueError('Legacy assignment has unspecified/ambiguous class or specialization; manual migration required.')
                    members.append(dict(id=str(a['player_id']),name=a['byname'] or str(a['player_id']),c=c,spec=spec,role=role))
                ids=[m['id'] for m in members]
                if len(ids)!=len(set(ids)) or len(ids)>event['size']:raise ValueError('Legacy lineup contains duplicate players or exceeds event size.')
                self.store.write('INSERT OR IGNORE INTO EventPlans VALUES (?,1,?)',(raid_id,json.dumps([dict(label=event['name'],members=members)])))
                self.store.write('DELETE FROM LegacyMigration WHERE raid_id=?',(raid_id,))
            except ValueError as exc:
                self.store.write('INSERT OR REPLACE INTO LegacyMigration VALUES (?,?)',(raid_id,str(exc)))

    async def cog_unload(self):
        self.refresh_plans.cancel()

    def players(self, raid_id):
        return self.store.rows('SELECT * FROM Players WHERE raid_id=? AND unavailable=0 ORDER BY timestamp,player_id', (raid_id,))

    def specs(self):
        return {r['player_id']:r for r in self.store.rows('SELECT * FROM Specs')}

    def template(self, event, kind):
        return self.store.one('SELECT body FROM EventTemplates WHERE guild_id=? AND kind=?', (event['guild_id'], kind))

    def render(self, event, body, limit=1800):
        return render_template(body, context_for(event, len(self.players(event['raid_id']))), limit)

    def plan(self, raid_id):
        return self.store.one('SELECT * FROM EventPlans WHERE raid_id=?', (raid_id,))

    def save(self, event, teams, expected):
        current = self.plan(event['raid_id'])
        if (current['revision'] if current else 0) != expected:
            raise ValueError('Roster changed. Use /roster-show to get the latest revision, then retry.')
        latest = self.store.event(event['raid_id'])
        if not latest or self.store.lock_due(latest, time.time()):
            raise ValueError('This event is closed.')
        self.store.write('INSERT OR REPLACE INTO EventPlans VALUES (?,?,?)',
                         (event['raid_id'], expected+1, json.dumps(teams)))
        return expected+1

    def pages(self, event, plan):
        teams = json.loads(plan['body'])
        signed = {str(p['player_id']):p for p in self.players(event['raid_id'])}
        pages = []
        for n, team in enumerate(teams, 1):
            members = team['members']
            sizes=team.get('fellowship_sizes')
            starts=[]
            if sizes and sum(sizes)==len(members):
                offset=0
                for size in sizes:starts.append((offset,size));offset+=size
            else:starts=[(offset,6) for offset in range(0,max(1,len(members)),6)]
            for group_index,(start,group_size) in enumerate(starts,1):
                lines = [f"**Planned roster · revision {plan['revision']} · team {n}**",
                         discord.utils.escape_markdown(team['label']),
                         f'Fellowship {group_index}' if sizes else f'Group {group_index} (list order)']
                if team.get('time') is not None:
                    lines.append(f"Planner time: {team['time']:g}h")
                for slot, p in enumerate(members[start:start+group_size], start+1):
                    status = '' if p['id'] in signed else ' — LEFT SIGNUPS'
                    if p['id'] in signed and not any(v for k,v in signed[p['id']].items() if k.replace('-','').lower()==p['c'].replace('-','').lower()):
                        status = ' — CLASS WITHDRAWN'
                    lines.append(f"{slot}. <@{p['id']}> · {p['c']} / {p['spec']} · {p['role']}{status}")
                pages.append('\n'.join(lines))
        return pages

    async def sync(self, raid_id):
        async with self.lock:
            event, plan = self.store.event(raid_id), self.plan(raid_id)
            if not event or not plan:
                return
            pages = self.pages(event, plan)
            for copy in self.store.copies(raid_id):
                if copy['locked']:
                    continue
                try:
                    thread = await self.events.channel(copy['thread_id'])
                    if getattr(thread, 'locked', False):
                        continue
                    if thread.archived:
                        await thread.edit(archived=False)
                    old = {r['page']:r for r in self.store.rows('SELECT * FROM PlanMessages WHERE thread_id=?', (copy['thread_id'],))}
                    for index, body in enumerate(pages):
                        key=f"roster:{copy['thread_id']}:{index}:{hashlib.sha256(body.encode()).hexdigest()}"
                        row = old.pop(index, None)
                        if row and row['body'] == body:
                            try:
                                await thread.fetch_message(row['message_id'])
                                continue
                            except discord.NotFound:
                                self.store.write('DELETE FROM SendIntents WHERE key=?',(key,))
                                row=None
                        message = None
                        if row:
                            try:
                                await thread.get_partial_message(row['message_id']).edit(content=body, allowed_mentions=discord.AllowedMentions.none())
                                message = row['message_id']
                            except discord.NotFound:
                                self.store.write('DELETE FROM SendIntents WHERE key=?',(key,))
                        if message is None:
                            sent = await self.sender.send(thread,key,body)
                            message = sent.id
                        self.store.write('INSERT OR REPLACE INTO PlanMessages VALUES (?,?,?,?)', (copy['thread_id'], index, message, body))
                    for index, row in old.items():
                        try:
                            await thread.get_partial_message(row['message_id']).delete()
                        except discord.NotFound:
                            pass
                        self.store.write('DELETE FROM PlanMessages WHERE thread_id=? AND page=?', (copy['thread_id'], index))
                    self.store.write('INSERT OR REPLACE INTO RosterDelivery VALUES (?,?,NULL)',(copy['thread_id'],int(time.time())))
                except discord.HTTPException as exc:
                    self.store.write('INSERT INTO RosterDelivery VALUES (?,NULL,?) ON CONFLICT(thread_id) DO UPDATE SET error=excluded.error',(copy['thread_id'],str(exc)))
                    log.exception('Roster delivery failed; retrying next cycle')

    @tasks.loop(seconds=30)
    async def refresh_plans(self):
        for row in self.store.rows('SELECT raid_id FROM EventPlans'):
            try:
                await self.sync(row['raid_id'])
            except Exception:
                log.exception('Roster refresh failed; continuing other events')

    @refresh_plans.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='description', description='Set the default event footer text by copying a Discord message link (or view/clear it).')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        message_link='Link to a Discord message (or raw text) to copy into all events between the dividers.',
        clear='Remove the saved event description footer.',
    )
    async def description_command(self, interaction: discord.Interaction, message_link: str | None = None, clear: bool = False):
        if message_link is not None and clear:
            raise ValueError('Provide a message_link or set clear=True, not both.')
        if clear:
            self.store.write("DELETE FROM EventTemplates WHERE guild_id=? AND kind IN ('footer', 'description')", (interaction.guild_id,))
            self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)', (interaction.guild_id,))
            await interaction.response.send_message('Event description footer cleared. Active events will update on the next refresh.', ephemeral=True)
            return
        if message_link is None:
            row = self.store.one("SELECT body FROM EventTemplates WHERE guild_id=? AND kind='footer'", (interaction.guild_id,))
            if not row:
                row = self.store.one("SELECT body FROM EventTemplates WHERE guild_id=? AND kind='description'", (interaction.guild_id,))
            await interaction.response.send_message(
                f"**Current event description footer:**\n{row['body']}" if row else 'No event description footer set. Pass a Discord `message_link` to `/description` to set one.',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        m = re.search(r'discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)', message_link.strip())
        await interaction.response.defer(ephemeral=True)
        if m:
            link_guild_id, link_channel_id, link_message_id = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if link_guild_id != interaction.guild_id:
                raise ValueError('The template message link must be from a channel in this server.')
            try:
                channel = await self.events.channel(link_channel_id)
                msg = await channel.fetch_message(link_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                raise ValueError('Could not fetch that message. Ensure the bot has View Channel and Read Message History in that channel.') from exc
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
            body = message_link.strip()
        if len(body) > 2500:
            raise ValueError('The template text must be 2500 characters or fewer.')
        self.store.write("DELETE FROM EventTemplates WHERE guild_id=? AND kind='description'", (interaction.guild_id,))
        self.store.write('INSERT OR REPLACE INTO EventTemplates VALUES (?,?,?)', (interaction.guild_id, 'footer', body))
        self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)', (interaction.guild_id,))
        for r in self.store.rows('SELECT raid_id FROM Raids WHERE guild_id=?', (interaction.guild_id,)):
            if self.store.event(r['raid_id']):
                await self.events.refresh(r['raid_id'])
        await interaction.followup.send(
            f"Saved event description footer and updated active events:\n\n{body[:1500]}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name='event-template', description='Set a source-server description or announcement template; omit text to view.')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def template_command(self, interaction: discord.Interaction, kind: Literal['description','announcement'], text: str | None = None, reset: bool = False):
        target_kind = 'footer' if kind == 'description' else kind
        if text is not None and reset:
            raise ValueError('Choose text or reset.')
        if reset:
            self.store.write('DELETE FROM EventTemplates WHERE guild_id=? AND kind IN (?,?)', (interaction.guild_id, kind, target_kind))
        elif text is not None:
            if len(text) > 1000:
                raise ValueError('Templates may contain up to 1000 characters.')
            render_template(text, {tag:'example' for tag in TAGS}, 4096)
            self.store.write('DELETE FROM EventTemplates WHERE guild_id=? AND kind=?', (interaction.guild_id, kind))
            self.store.write('INSERT OR REPLACE INTO EventTemplates VALUES (?,?,?)', (interaction.guild_id, target_kind, text))
        if text is None and not reset:
            row = self.store.one('SELECT body FROM EventTemplates WHERE guild_id=? AND kind IN (?,?)', (interaction.guild_id, target_kind, kind))
            await interaction.response.send_message((row['body'] if row else 'No template set.')+'\nTags: '+', '.join('{'+t+'}' for t in sorted(TAGS)), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)', (interaction.guild_id,))
        await interaction.response.send_message('Template saved. Description updates propagate on the next refresh.', ephemeral=True)

    def raid_planner_url(self, raid_id):
        event = self.store.event(raid_id)
        if not event or event.get('creep'):
            return PLANNER_URL
        try:
            data = export_session(event, self.players(raid_id), self.specs())
            return planner_link(data, max_length=512)
        except Exception:
            return PLANNER_URL

    async def open_import_modal(self, interaction, raid_id=None):
        if raid_id is None:
            event = await self.events.command_event(interaction)
            if not event:
                return
            raid_id = event['raid_id']
        else:
            event = self.store.event(raid_id)
            if not event:
                await interaction.response.send_message('Use this button inside an event forum post.', ephemeral=True)
                return
            if not await self.events.can_manage(interaction, raid_id):
                await interaction.response.send_message('Only the organizer or source raid leader can import.', ephemeral=True)
                return
        plan = self.plan(raid_id)
        await interaction.response.send_modal(PlanCodeModal(self, raid_id, plan['revision'] if plan else 0))

    async def open_planner(self, interaction):
        event = await self.events.command_event(interaction)
        if not event:
            return
        try:
            specs = {r['player_id']:r for r in self.store.rows('SELECT * FROM Specs')}
            data = export_session(event, self.players(event['raid_id']), specs)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        url = planner_link(data)
        if url:
            await interaction.response.send_message(f"[📋 Open Raid Planner]({url})", ephemeral=True, suppress_embeds=True)
        else:
            files = [discord.File(io.BytesIO(json.dumps(data).encode()), filename=f"grima-{event['raid_id']}.json")]
            long_url = planner_link(data, max_length=20000)
            instructions = 'Import the attached session JSON.'
            if long_url:
                files.append(discord.File(io.BytesIO(long_url.encode()), filename='planner-link.txt'))
                instructions = 'This roster exceeds Discord’s link limit. Open the URL in planner-link.txt, paste it into the planner URL field, or import the attached JSON.'
            await interaction.response.send_message(
                f"[📋 Open Raid Planner]({PLANNER_URL}) — {instructions}",
                files=files,
                ephemeral=True,
                suppress_embeds=True,
            )

    @app_commands.command(name='planner-export', description='Open current signups in Vanguard, with JSON fallback for larger rosters.')
    @app_commands.guild_only()
    async def export_command(self, interaction: discord.Interaction):
        await self.open_planner(interaction)

    @app_commands.command(name='planner-import', description='Replace this event’s planned roster from Vanguard Session JSON.')
    @app_commands.guild_only()
    async def import_command(self, interaction: discord.Interaction, session: discord.Attachment, expected_revision: app_commands.Range[int,0]):
        event = await self.events.command_event(interaction)
        if not event:
            return
        if session.size > 2_000_000:
            raise ValueError('Session must be at most 2 MB.')
        await interaction.response.defer(ephemeral=True)
        try:
            data = json.loads(await session.read())
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Upload valid session JSON.') from exc
        teams = import_plan(data, event, self.players(event['raid_id']),self.specs(),require_identity=True)
        revision = self.save(event, teams, expected_revision)
        await self.sync(event['raid_id'])
        await interaction.followup.send(f'Roster revision {revision} saved ({len(teams)} teams). Linked threads update directly; failed deliveries retry.', ephemeral=True)

    @app_commands.command(name='roster-create',description='Start the authoritative shared roster without using Vanguard.')
    @app_commands.guild_only()
    async def create_roster(self,interaction:discord.Interaction):
        event=await self.events.command_event(interaction)
        if not event:return
        self.save(event,[dict(label=event['name'],members=[])],0)
        await interaction.response.defer(ephemeral=True)
        await self.sync(event['raid_id'])
        await interaction.followup.send('Roster revision 1 created. Add players with /roster-edit.',ephemeral=True)

    async def open_editor(self,interaction):
        event=await self.events.command_event(interaction)
        if not event:return
        if not self.plan(event['raid_id']) and self.store.one('SELECT 1 FROM LegacyMigration WHERE raid_id=?',(event['raid_id'],)):
            await interaction.response.send_message('Legacy assignments need review before creating a replacement. Use /event-status for details; original Assignment rows are retained.',ephemeral=True);return
        if not self.plan(event['raid_id']):
            self.save(event,[dict(label=event['name'],members=[])],0)
        from roster_editor import RosterEditor
        await interaction.response.send_message('Choose team, slot, player and class/spec, then Save.',view=RosterEditor(self,event,interaction.user.id),ephemeral=True)

    @app_commands.command(name='roster-manage',description='Edit the roster with player and slot dropdowns.')
    @app_commands.guild_only()
    async def manage_command(self,interaction:discord.Interaction):
        await self.open_editor(interaction)

    @app_commands.command(name='roster-show', description='Show the current planned roster and revision.')
    @app_commands.guild_only()
    async def show_command(self, interaction: discord.Interaction):
        event = await self.events.command_event(interaction, manage=False)
        if not event:
            return
        plan = self.plan(event['raid_id'])
        if not plan:
            await interaction.response.send_message('No plan yet. Use expected_revision:0 for your first import.', ephemeral=True)
            return
        await interaction.response.send_message(f"Revision {plan['revision']}", file=discord.File(io.BytesIO('\n\n'.join(self.pages(event, plan)).encode()), filename='roster.txt'), ephemeral=True)

    @app_commands.command(name='roster-edit', description='Replace/remove a slot or append a signup; team and slot numbers start at 1.')
    @app_commands.guild_only()
    async def edit_command(self, interaction: discord.Interaction, expected_revision: app_commands.Range[int,1], team: app_commands.Range[int,1,16], slot: app_commands.Range[int,1,25], player_id: str | None = None, class_name: str | None = None, specialization: Literal['Red','Blue','Yellow'] | None = None):
        event = await self.events.command_event(interaction)
        if not event:
            return
        plan = self.plan(event['raid_id'])
        if not plan:
            raise ValueError('Use /roster-create to start a plan or import one from Vanguard.')
        teams = json.loads(plan['body'])
        if team > len(teams) or slot > len(teams[team-1]['members'])+1:
            raise ValueError('Choose an existing slot or the next slot to append.')
        members = teams[team-1]['members']
        if player_id is None:
            if slot > len(members):
                raise ValueError('That slot is empty.')
            members.pop(slot-1)
            teams[team-1]['fellowship_sizes']=None
        else:
            role = dict(CLASSES.get(class_name, [])).get(specialization)
            if not role:
                raise ValueError('Supply a Vanguard class name and supported specialization.')
            member = dict(id=player_id, c=class_name, spec=specialization, role=role)
            checked = import_plan(dict(format='vanguard-raid-planner-session', version=6,
                teams=[dict(label='', members=[member])]), event, self.players(event['raid_id']),self.specs())
            member = checked[0]['members'][0]
            if slot > len(members):
                members.append(member)
                teams[team-1]['fellowship_sizes']=None
            else:
                members[slot-1] = member
        ids = [m['id'] for m in members]
        if len(ids) != len(set(ids)) or len(ids) > event['size']:
            raise ValueError('Duplicate player or team is full. Remove or swap a slot first.')
        revision = self.save(event, teams, expected_revision)
        await interaction.response.defer(ephemeral=True)
        await self.sync(event['raid_id'])
        await interaction.followup.send(f'Roster revision {revision} saved and synchronization queued.', ephemeral=True)

    @app_commands.command(name='roster-swap', description='Swap two planned roster slots, within or between teams.')
    @app_commands.guild_only()
    async def swap_command(self, interaction: discord.Interaction, expected_revision: app_commands.Range[int,1], team_a: app_commands.Range[int,1,16], slot_a: app_commands.Range[int,1,24], team_b: app_commands.Range[int,1,16], slot_b: app_commands.Range[int,1,24]):
        event = await self.events.command_event(interaction)
        if not event:
            return
        plan = self.plan(event['raid_id'])
        if not plan:
            raise ValueError('Use /roster-create to start a plan or import one from Vanguard.')
        teams = json.loads(plan['body'])
        try:
            a, b = teams[team_a-1]['members'], teams[team_b-1]['members']
            a[slot_a-1], b[slot_b-1] = b[slot_b-1], a[slot_a-1]
        except IndexError as exc:
            raise ValueError('Choose existing teams and slots.') from exc
        for members in (a,b):
            ids = [p['id'] for p in members]
            if len(ids) != len(set(ids)):
                raise ValueError('This swap would duplicate a player within a team.')
        revision = self.save(event, teams, expected_revision)
        await interaction.response.defer(ephemeral=True)
        await self.sync(event['raid_id'])
        await interaction.followup.send(f'Roster revision {revision} saved and synchronization queued.', ephemeral=True)

    async def cog_app_command_error(self, interaction, error):
        original = getattr(error, 'original', error)
        message = str(original) if isinstance(original, (ValueError, app_commands.MissingPermissions)) else 'Could not complete command. Check bot logs.'
        if not isinstance(original, ValueError):
            log.error('Planning command failed', exc_info=error)
        sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await sender(message[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class PlanReturnView(discord.ui.View):
    def __init__(self,cog,raid_id):
        super().__init__(timeout=900)
        self.cog,self.raid_id=cog,raid_id

    @discord.ui.button(label='Import plan code',style=discord.ButtonStyle.primary)
    async def import_code(self,i,button):
        if not await self.cog.events.can_manage(i,self.raid_id):
            await i.response.send_message('Only the organizer or source raid leader can import.',ephemeral=True);return
        plan=self.cog.plan(self.raid_id)
        await i.response.send_modal(PlanCodeModal(self.cog,self.raid_id,plan['revision'] if plan else 0))

class PlanCodeModal(discord.ui.Modal,title='Import Vanguard plan'):
    code=discord.ui.TextInput(
        label='Paste exported Session JSON or plan code',
        placeholder='Paste JSON or grimaplan:... exported from Raid Planner',
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )
    def __init__(self,cog,raid_id,revision):
        super().__init__();self.cog,self.raid_id,self.revision=cog,raid_id,revision
    async def on_submit(self,i):
        if not await self.cog.events.can_manage(i,self.raid_id):
            await i.response.send_message('You can no longer edit this event.',ephemeral=True);return
        try:
            event=self.cog.store.event(self.raid_id)
            teams=import_plan(decode_plan_code(str(self.code)),event,self.cog.players(self.raid_id),self.cog.specs(),require_identity=False)
            revision=self.cog.save(event,teams,self.revision)
        except ValueError as exc:
            await i.response.send_message(str(exc),ephemeral=True);return
        await i.response.defer(ephemeral=True)
        await self.cog.sync(self.raid_id)
        await i.followup.send(f'Roster revision {revision} synchronized.',ephemeral=True)


async def setup(bot):
    await bot.add_cog(PlanningCog(bot))
