import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from planning import export_session, import_plan, render_template, context_for, planner_link, decode_plan_code
from planning_cog import PlanningCog

EVENT = dict(raid_id=100, guild_id=1, name='Folly', tier='T2', boss='Win', time=1000, duration=3600, size=12, organizer_id=9)
PLAYERS = [dict(player_id=9007199254740993, byname='Alice', Guardian=1)]
TEAM = dict(label='Folly', members=[dict(id=str(PLAYERS[0]['player_id']), c='Guardian', spec='Blue', role='Tank')])

class InterchangeTests(unittest.TestCase):
    def test_planner_link_roundtrip_and_limit(self):
        import base64, zlib
        data=export_session(EVENT, PLAYERS, {})
        token=planner_link(data).split('#grima=')[1]
        unpacked=json.loads(zlib.decompress(base64.urlsafe_b64decode(token+'='*((-len(token))%4))))
        self.assertEqual(unpacked,data)
        import random,string
        rng=random.Random(1)
        self.assertIsNone(planner_link({'text': ''.join(rng.choices(string.ascii_letters,k=10000))}))

    def test_plan_paste_code_limits(self):
        import base64,zlib
        encode=lambda raw:'grimaplan:'+base64.urlsafe_b64encode(zlib.compress(raw)).decode().rstrip('=')
        data=dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM])
        self.assertEqual(decode_plan_code(encode(json.dumps(data).encode())),data)
        for code in ('bad', 'grimaplan:bad',encode(b'x'*2000001)):
            with self.assertRaises(ValueError):decode_plan_code(code)

    def test_identity_and_offered_spec_validation(self):
        data=dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM])
        with self.assertRaises(ValueError):import_plan(data,EVENT,PLAYERS,{},require_identity=True)
        data['grimaEventId']=str(EVENT['raid_id'])
        self.assertTrue(import_plan(data,EVENT,PLAYERS,{},require_identity=True))
        with self.assertRaises(ValueError):import_plan(data,EVENT,PLAYERS,{PLAYERS[0]['player_id']:{'Guardian':1<<3}},require_identity=True)

    def test_templates(self):
        self.assertEqual(render_template('{title} {{literal}} {description} {signup_count}', context_for(EVENT, 3)), 'Folly {literal} Win 3')
        for text in ('{title.__class__}', '{title[0]}', '{title!r}', '{title:>90}', '{}', '{bad}', '{'):
            with self.assertRaises(ValueError):
                render_template(text, context_for(EVENT, 3))
        with self.assertRaises(ValueError):
            render_template('{title}', context_for(EVENT, 3), 2)

    def test_export_spec_bits_and_snowflake(self):
        data = export_session(EVENT, PLAYERS, {PLAYERS[0]['player_id']:dict(Guardian=2 << 3)})
        self.assertEqual(data['players'][0]['id'], '9007199254740993')
        self.assertEqual(data['players'][0]['choices'], [dict(c='Guardian',spec='Blue',role='Tank')])
        self.assertEqual(data['players'][0]['available'], [True])
        self.assertEqual(len(export_session(EVENT, PLAYERS, {})['players'][0]['choices']), 2)

    def test_import_compact_and_full(self):
        for version in (6,7):
            data = dict(format='vanguard-raid-planner-session', version=version, teams=[TEAM])
            teams = import_plan(data, EVENT, PLAYERS)
            self.assertEqual(teams[0]['members'][0]['name'], 'Alice')
            self.assertEqual(teams[0]['members'][0]['id'], '9007199254740993')

    def test_actual_vanguard_compact_export(self):
        data = json.loads((Path(__file__).parent / 'fixtures/vanguard-compact-roundtrip.json').read_text())
        teams = import_plan(data, EVENT, PLAYERS)
        self.assertEqual(teams[0]['members'][0]['id'], '9007199254740993')
        self.assertEqual(teams[0]['members'][0]['c'], 'Guardian')

    def test_explicit_fellowships_are_preserved_and_validated(self):
        team=copy.deepcopy(TEAM);team['grimaFellowships']=[[str(PLAYERS[0]['player_id'])]]
        data=dict(format='vanguard-raid-planner-session',version=7,teams=[team])
        self.assertEqual(import_plan(data,EVENT,PLAYERS)[0]['fellowship_sizes'],[1])
        team['grimaFellowships']=[['unknown']]
        with self.assertRaises(ValueError):import_plan(data,EVENT,PLAYERS)

    def test_invalid_plans(self):
        base = dict(format='vanguard-raid-planner-session', version=7, teams=[TEAM])
        cases = []
        for key,value in [('version',8), ('grimaEventId','101'), ('teams',[]), ('dirty',True)]:
            data=copy.deepcopy(base); data[key]=value; cases.append(data)
        for key,value in [('id','unknown'),('id',9007199254740993),('c','Hunter'),('spec','Yellow'),('role','DPS')]:
            data=copy.deepcopy(base); data['teams'][0]['members'][0][key]=value; cases.append(data)
        data=copy.deepcopy(base);data['teams'][0]['members']*=2;cases.append(data)
        for data in cases:
            with self.assertRaises(ValueError):
                import_plan(data, EVENT, PLAYERS)

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import sqlite3
        from event_store import EventStore
        self.conn=sqlite3.connect(':memory:')
        self.store=EventStore(self.conn)
        self.store.event=Mock(return_value={**EVENT, 'cancelled':0, 'lock_delay':600, 'time':9999999999})
        self.store.copies=Mock(return_value=[dict(thread_id=1,locked=0),dict(thread_id=2,locked=0),dict(thread_id=3,locked=1)])
        self.channels={i:NS(archived=False,fetch_message=AsyncMock(),send=AsyncMock(return_value=NS(id=i*10)),get_partial_message=Mock(return_value=NS(edit=AsyncMock(),delete=AsyncMock()))) for i in (1,2,3)}
        events=NS(store=self.store,channel=AsyncMock(side_effect=lambda i:self.channels[i]))
        self.cog=PlanningCog(NS(conn=self.conn,get_cog=lambda _:events))
        self.cog.players=Mock(return_value=PLAYERS)
        self.cog.specs=Mock(return_value={})
        self.event=self.store.event(100)

    async def asyncTearDown(self):
        self.conn.close()

    async def test_revision_and_all_mirrors(self):
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.assertEqual(self.cog.save(self.event,teams,0),1)
        with self.assertRaises(ValueError): self.cog.save(self.event,teams,0)
        await self.cog.sync(100)
        for i in (1,2): self.channels[i].send.assert_awaited_once()
        self.channels[3].send.assert_not_awaited()
        await self.cog.sync(100)
        self.channels[1].send.assert_awaited_once()
        self.cog.players.return_value=[]
        await self.cog.sync(100)
        self.assertIn('LEFT SIGNUPS', self.channels[1].get_partial_message.return_value.edit.call_args.kwargs['content'])

    async def test_failed_delivery_retries(self):
        import discord
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        self.channels[1].send.side_effect=discord.Forbidden(NS(status=403,reason='Forbidden'),'denied')
        with self.assertLogs('planning_cog',level='ERROR'): await self.cog.sync(100)
        self.channels[2].send.assert_awaited_once()
        self.channels[1].send.side_effect=None
        await self.cog.sync(100)
        self.assertEqual(self.channels[1].send.await_count,2)
        self.channels[2].send.assert_awaited_once()

    async def test_restart_uses_saved_messages(self):
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        await self.cog.sync(100)
        restarted=PlanningCog(self.cog.bot)
        restarted.players=self.cog.players
        await restarted.sync(100)
        self.channels[1].send.assert_awaited_once()

    async def test_edit_and_authority(self):
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        interaction=NS(response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        self.cog.events.command_event=AsyncMock(return_value=None)
        await PlanningCog.edit_command.callback(self.cog,interaction,1,1,1)
        self.assertEqual(self.cog.plan(100)['revision'],1)
        self.cog.events.command_event.return_value=self.event
        await PlanningCog.edit_command.callback(self.cog,interaction,1,1,1,str(PLAYERS[0]['player_id']),'Guardian','Red')
        self.assertEqual(json.loads(self.cog.plan(100)['body'])[0]['members'][0]['spec'],'Red')
        self.assertEqual(self.cog.plan(100)['revision'],2)
        self.cog.players.return_value=[]
        await PlanningCog.edit_command.callback(self.cog,interaction,2,1,1)
        self.assertEqual(json.loads(self.cog.plan(100)['body'])[0]['members'],[])

    async def test_closed_event_does_not_mutate(self):
        self.store.event.return_value={**self.event,'cancelled':1}
        with self.assertRaises(ValueError): self.cog.save(self.event,[TEAM],0)
        self.assertIsNone(self.cog.plan(100))

    async def test_withdrawn_class_is_flagged(self):
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        self.cog.players.return_value=[dict(player_id=PLAYERS[0]['player_id'],byname='Alice',Guardian=0,Hunter=1)]
        self.assertIn('CLASS WITHDRAWN',self.cog.pages(self.event,self.cog.plan(100))[0])

    async def test_deleted_unchanged_roster_is_recreated(self):
        import discord
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        await self.cog.sync(100)
        self.channels[1].fetch_message.side_effect=discord.NotFound(NS(status=404,reason='missing'),'missing')
        await self.cog.sync(100)
        self.assertEqual(self.channels[1].send.await_count,2)
