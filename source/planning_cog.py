"""Templates and durable, directly synchronized Vanguard rosters."""
import asyncio
import io
import json
import logging
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
        bot.conn.executescript('''
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
        self.refresh_plans.start()

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
                        row = old.pop(index, None)
                        if row and row['body'] == body:
                            try:
                                await thread.fetch_message(row['message_id'])
                                continue
                            except discord.NotFound:
                                row=None
                        message = None
                        if row:
                            try:
                                await thread.get_partial_message(row['message_id']).edit(content=body, allowed_mentions=discord.AllowedMentions.none())
                                message = row['message_id']
                            except discord.NotFound:
                                pass
                        if message is None:
                            sent = await thread.send(body, allowed_mentions=discord.AllowedMentions.none())
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

    @app_commands.command(name='event-template', description='Set a source-server description or announcement template; omit text to view.')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def template_command(self, interaction: discord.Interaction, kind: Literal['description','announcement'], text: str | None = None, reset: bool = False):
        if text is not None and reset:
            raise ValueError('Choose text or reset.')
        if reset:
            self.store.write('DELETE FROM EventTemplates WHERE guild_id=? AND kind=?', (interaction.guild_id, kind))
        elif text is not None:
            if len(text) > 1000:
                raise ValueError('Templates may contain up to 1000 characters.')
            render_template(text, {tag:'example' for tag in TAGS}, 4096)
            self.store.write('INSERT OR REPLACE INTO EventTemplates VALUES (?,?,?)', (interaction.guild_id, kind, text))
        if text is None and not reset:
            row = self.store.one('SELECT body FROM EventTemplates WHERE guild_id=? AND kind=?', (interaction.guild_id,kind))
            await interaction.response.send_message((row['body'] if row else 'No template set.')+'\nTags: '+', '.join('{'+t+'}' for t in sorted(TAGS)), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)', (interaction.guild_id,))
        await interaction.response.send_message('Template saved. Description updates propagate on the next refresh.', ephemeral=True)

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
        view = PlanReturnView(self,event['raid_id'])
        if url and len(url) <= 512:
            view.add_item(discord.ui.Button(label='Open in Vanguard', url=url))
        elif url:
            # Discord link buttons allow 512 characters; normal message links allow a larger snapshot.
            pass
        else:
            view.add_item(discord.ui.Button(label='Open Vanguard', url=PLANNER_URL))
        text = 'Current signup snapshot. In Vanguard, choose Copy JSON or Load signups. Loading replaces the open plan. Keep this private link private.'
        if url and len(url) > 512:
            text += '\n[Open in Vanguard](' + url + ')'
        if not url:
            text = 'This signup list is too large for a link. Open Vanguard and import the attached session JSON.'
        kwargs = {} if url else dict(file=discord.File(io.BytesIO(json.dumps(data).encode()), filename=f"grima-{event['raid_id']}.json"))
        await interaction.response.send_message(text, view=view, ephemeral=True, suppress_embeds=True, **kwargs)

    @app_commands.command(name='planner-export', description='Export current signups as a Vanguard session JSON.')
    @app_commands.guild_only()
    async def export_command(self, interaction: discord.Interaction):
        event = await self.events.command_event(interaction)
        if not event:
            return
        specs = {r['player_id']:r for r in self.store.rows('SELECT * FROM Specs')}
        data = export_session(event, self.players(event['raid_id']), specs)
        await interaction.response.send_message('Import this session into Vanguard. Set your composition, generate teams, then export Session JSON. Unspecified specs offer all supported specs; the run starts at relative hour 0.', file=discord.File(io.BytesIO(json.dumps(data).encode()), filename=f"grima-{event['raid_id']}.json"), ephemeral=True)

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
    code=discord.ui.TextInput(label='Paste the Grima plan code',style=discord.TextStyle.paragraph,max_length=4000)
    def __init__(self,cog,raid_id,revision):
        super().__init__();self.cog,self.raid_id,self.revision=cog,raid_id,revision
    async def on_submit(self,i):
        if not await self.cog.events.can_manage(i,self.raid_id):
            await i.response.send_message('You can no longer edit this event.',ephemeral=True);return
        try:
            event=self.cog.store.event(self.raid_id)
            teams=import_plan(decode_plan_code(str(self.code)),event,self.cog.players(self.raid_id),self.cog.specs(),require_identity=True)
            revision=self.cog.save(event,teams,self.revision)
        except ValueError as exc:
            await i.response.send_message(str(exc),ephemeral=True);return
        await i.response.defer(ephemeral=True)
        await self.cog.sync(self.raid_id)
        await i.followup.send(f'Roster revision {revision} synchronized.',ephemeral=True)


async def setup(bot):
    await bot.add_cog(PlanningCog(bot))
