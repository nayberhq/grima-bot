import asyncio
import builtins
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source'))
# Upstream modules read configuration and translation globals at import time.
previous_cwd = os.getcwd()
previous_translate = getattr(builtins, '_', None)
scratch = tempfile.TemporaryDirectory()
os.chdir(scratch.name)
for filename in ('common_timezones.txt', 'timezones.txt', 'list-of-raids.csv'):
    Path(filename).write_text((ROOT / 'source' / filename).read_text())
Path('config.json').write_text((ROOT / 'source/example-config.json').read_text())
builtins._ = lambda value: value
import discord
from database import create_table
from event_store import EventStore, forum_title, mirror_keys
from events_cog import EventsCog, CreationView
from raid_cog import RaidCog, RaidView
from calendar_cog import CalendarCog


def tearDownModule():
    os.chdir(previous_cwd)
    scratch.cleanup()
    if previous_translate is None:
        del builtins._
    else:
        builtins._ = previous_translate


def forbidden():
    return discord.Forbidden(NS(status=403, reason='Forbidden'), 'test denial')


def database():
    conn = sqlite3.connect(':memory:')
    for table in ('raid', 'player', 'assign', 'settings', 'specs'):
        create_table(conn, table)
    return conn


def insert_raid(store, raid_id=100, start=None):
    start = int(time.time()) + 600 if start is None else start
    store.write('''INSERT INTO Raids
        (raid_id,channel_id,guild_id,organizer_id,name,time,roster,size)
        VALUES (?,100,1,9,'Folly',?,0,12)''', (raid_id, start))
    store.configure(1, 10, 11, 3600, 600)
    store.add_event(raid_id, store.settings(1))
    store.add_copy(raid_id, 1, 100, 100, 11)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()
        self.addCleanup(self.conn.close)
        self.store = EventStore(self.conn)
        insert_raid(self.store, start=1000)

    def test_copy_maps_to_single_event_and_duplicate_rejected(self):
        self.store.add_copy(100, 2, 200, 201, 22)
        self.assertEqual(self.store.resolve(201), 100)
        self.assertEqual(self.store.resolve(200), 100)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_copy(100, 2, 202, 203, 22)

    def test_end_and_delayed_lock_boundaries(self):
        event = self.store.event(100)
        self.assertFalse(self.store.ended(event, 4599))
        self.assertTrue(self.store.ended(event, 4600))
        self.assertFalse(self.store.lock_due(event, 5199))
        self.assertTrue(self.store.lock_due(event, 5200))

    def test_tokens_rotate_and_revoke(self):
        first = self.store.share(100)
        second = self.store.share(100)
        self.assertIsNone(self.store.by_token(first))
        self.assertEqual(self.store.by_token(second)['raid_id'], 100)
        self.store.write('UPDATE SharedEvents SET share_token=NULL')
        self.assertIsNone(self.store.by_token(second))

    def test_marker_parser(self):
        key = 'aB0_' * 8
        self.assertEqual(mirror_keys(f'mirrorkey:{key}\nmirrorkey:{key}'), [key])
        self.assertEqual(mirror_keys(f'notmirrorkey:{key} mirrorkey:short mirrorkey:{key}x'), [])

    def test_title_uses_destination_timezone_and_truncates(self):
        # 2026-11-02 01:00 UTC is still Sunday on the Pacific coast.
        from datetime import datetime, timezone
        timestamp = int(datetime(2026, 11, 2, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(forum_title('Folly', timestamp, 'America/Los_Angeles'), 'Sunday Folly')
        self.assertEqual(forum_title('Folly', timestamp, 'UTC'), 'Monday Folly')
        self.assertEqual(len(forum_title('x' * 200, timestamp, 'UTC')), 100)

    def test_migrations_preserve_existing_data(self):
        second = EventStore(self.conn)
        self.assertEqual(second.event(100)['name'], 'Folly')

    def test_outbox_snapshots_destinations(self):
        self.store.add_copy(100, 2, 200, 200, 22)
        self.store.queue_announcement(100, 'Starting soon', 9)
        self.store.add_copy(100, 3, 300, 300, 33)
        self.assertEqual({x['thread_id'] for x in self.store.pending_announcements()}, {100, 200})


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = database()
        self.addCleanup(self.conn.close)
        self.channels = {}
        self.guilds = {1: NS(id=1), 2: NS(id=2)}
        self.raids = RaidCog.__new__(RaidCog)
        self.raids.conn = self.conn
        self.raids.role_names = ('Warden',)
        self.raids.creep_names = ()
        self.raids.class_emojis = []
        self.raids.slots_class_names = [('Warden',)] * 12
        self.raids.spec_emojis = []
        self.raids.emojis_dict = {'Warden': 'W'}
        self.raids.specs = ['', 'Red', 'Blue', 'Red_Blue', 'Yellow']
        self.raids.calendar_cog = NS(update_calendar=AsyncMock(),
            is_raid_leader=lambda member, guild: getattr(member, 'leader', False))
        cogs = {'RaidCog': self.raids, 'TimeCog': NS(get_server_timezone=lambda guild_id: 'UTC')}
        self.bot = NS(conn=self.conn, get_channel=lambda cid: self.channels.get(cid),
            get_guild=lambda gid: self.guilds.get(gid), get_cog=lambda name: cogs.get(name), add_view=Mock())
        self.cog = EventsCog(self.bot)
        cogs['EventsCog'] = self.cog
        self.raids.bot = self.bot
        self.raids.event_store = self.cog.store
        insert_raid(self.cog.store)
        self.cog.store.configure(2, 20, 22, 10800, 3600)
        self.thread = self.make_thread(100)
        self.channels[10]=NS(create_invite=AsyncMock(return_value=NS(url='https://discord.gg/owner')))

    def make_thread(self, thread_id):
        post = NS(edit=AsyncMock())
        thread = NS(id=thread_id, locked=False, archived=False,
            edit=AsyncMock(), fetch_message=AsyncMock(), get_partial_message=Mock(return_value=post), send=AsyncMock())
        self.channels[thread_id] = thread
        return thread

    async def test_persistent_creation_buttons(self):
        panel = CreationView(self.cog, 'cluster')
        self.assertTrue(panel.is_persistent())
        self.assertEqual([button.label for button in panel.children], ['Folly', 'Wailing Hold', 'Palace', 'Garden', 'Hunter Dailies'])

    async def test_duplicate_concurrent_sync_creates_one_post(self):
        token = self.cog.store.share(100)
        target = self.make_thread(200)
        forum = NS(create_thread=AsyncMock(return_value=NS(thread=target, message=NS(id=200))))
        self.cog.validate_destinations = AsyncMock(return_value=forum)
        results = await asyncio.gather(self.cog.sync_event(2, token), self.cog.sync_event(2, token))
        self.assertEqual(sorted(created for _, created in results), [False, True])
        forum.create_thread.assert_awaited_once()
        self.assertEqual(self.cog.store.resolve(200), 100)
        self.assertEqual(len(self.cog.store.copies(100)), 2)

    async def test_signup_and_cancel_from_mirror_use_canonical_roster(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        self.make_thread(200)
        view = RaidView(self.raids)
        role = NS(id=80, name='Warden')
        user = NS(id=99, roles=[role], display_name='Remote Warden')
        interaction = NS(message=NS(id=200), user=user,
            guild=NS(id=2, roles=[role]), channel=self.channels[200],
            response=NS(send_message=AsyncMock(), defer=AsyncMock()))
        await view.sign_up_class(interaction, 'Warden')
        rows = self.conn.execute('SELECT raid_id,player_id,Warden FROM Players').fetchall()
        self.assertEqual(rows, [(100, 99, 1)])
        for thread_id in (100, 200):
            self.channels[thread_id].get_partial_message.return_value.edit.assert_awaited()
        await view.sign_up_cancel(interaction)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM Players').fetchone()[0], 0)

    async def test_partner_admin_cannot_edit_source_event(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        self.guilds[1].get_member = lambda uid: NS(id=uid, leader=False)
        user = NS(id=88, guild_permissions=NS(administrator=True))
        interaction = NS(user=user, guild=self.guilds[2])
        self.assertFalse(await self.cog.can_manage(interaction, 100))
        self.assertFalse(await self.raids.has_raid_permission(user, self.guilds[2], 200))

    async def test_ended_event_rejects_signup(self):
        self.cog.store.write('UPDATE Raids SET time=?', (int(time.time())-4000,))
        interaction = NS(message=NS(id=100), guild=NS(id=1), response=NS(send_message=AsyncMock()))
        self.assertFalse(await self.raids.check_signup(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_voice_created_at_start_then_locked_after_delay(self):
        now = int(time.time())
        self.cog.store.write('UPDATE Raids SET time=?', (now,))
        voice = NS(id=500, name='event-100', members=[NS(id=99)], delete=AsyncMock(), edit=AsyncMock())
        self.channels[500] = voice
        self.channels[11] = NS(voice_channels=[])
        self.guilds[1].create_voice_channel = AsyncMock(return_value=voice)
        await self.cog.reconcile(100, now-1)
        self.guilds[1].create_voice_channel.assert_not_awaited()
        await self.cog.reconcile(100, now)
        await self.cog.reconcile(100, now+1)
        self.guilds[1].create_voice_channel.assert_awaited_once()
        await self.cog.reconcile(100, now+3600)
        voice.delete.assert_not_awaited()
        self.assertFalse(self.cog.store.copies(100)[0]['locked'])
        await self.cog.reconcile(100, now+4200)
        self.assertTrue(self.cog.store.copies(100)[0]['locked'])
        voice.members = []
        await self.cog.reconcile(100, now+4201)
        voice.delete.assert_awaited_once()
        self.assertIsNone(self.cog.store.copies(100)[0]['voice_id'])

    async def test_mirrors_link_owner_voice_and_invite_without_creating_voice(self):
        now=int(time.time())
        self.cog.store.write('UPDATE Raids SET time=?',(now,))
        self.cog.store.add_copy(100,2,200,200,0)
        mirror=self.make_thread(200)
        voice=NS(id=500,members=[],delete=AsyncMock(),edit=AsyncMock())
        self.channels[500]=voice;self.channels[11]=NS(voice_channels=[])
        self.guilds[1].create_voice_channel=AsyncMock(return_value=voice)
        self.guilds[2].create_voice_channel=AsyncMock()
        await self.cog.reconcile(100,now)
        self.guilds[1].create_voice_channel.assert_awaited_once()
        self.guilds[2].create_voice_channel.assert_not_awaited()
        embed=mirror.get_partial_message.return_value.edit.call_args.kwargs['embed']
        values=[f.value for f in embed.fields]
        self.assertIn('https://discord.com/channels/1/500',values)
        self.assertIn('https://discord.gg/owner',values)
        self.assertIsNone(self.cog.store.copies(100)[1]['voice_id'])

    async def test_old_mirror_voice_waits_until_empty_then_retires(self):
        self.cog.store.add_copy(100,2,200,200,22);self.make_thread(200)
        self.cog.store.write('UPDATE EventCopies SET voice_id=600 WHERE guild_id=2')
        voice=NS(id=600,members=[NS(id=99)],delete=AsyncMock(),edit=AsyncMock())
        self.channels[600]=voice
        await self.cog.reconcile(100)
        voice.delete.assert_not_awaited()
        voice.members=[]
        await self.cog.reconcile(100)
        voice.delete.assert_awaited_once()
        self.assertIsNone(self.cog.store.copies(100)[1]['voice_id'])

    async def test_restart_after_end_does_not_create_voice(self):
        self.guilds[1].create_voice_channel = AsyncMock()
        self.cog.store.write('UPDATE Raids SET time=?', (int(time.time())-5000,))
        await self.cog.reconcile(100)
        self.guilds[1].create_voice_channel.assert_not_awaited()
        self.assertTrue(self.cog.store.copies(100)[0]['locked'])

    async def test_announcement_failure_does_not_block_other_servers(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        other = self.make_thread(200)
        self.thread.send.side_effect = forbidden()
        self.cog.store.queue_announcement(100, 'Hello', 9)
        with self.assertLogs('events_cog', level='ERROR'):
            await self.cog.deliver_announcements()
        other.send.assert_awaited_once()
        self.assertEqual(len(self.cog.store.pending_announcements()), 1)
        self.thread.send.side_effect = None
        await asyncio.gather(self.cog.deliver_announcements(), self.cog.deliver_announcements())
        self.assertEqual(other.send.await_count, 1)
        self.assertEqual(self.cog.store.pending_announcements(), [])

    async def test_watcher_cursor_retries_network_failure(self):
        key = self.cog.store.share(100)
        self.cog.store.write('INSERT INTO EventWatches VALUES (2,900,0)')
        messages = [NS(id=1, content='no marker'), NS(id=2, content=f'mirrorkey:{key}')]
        async def history(*, limit, oldest_first, after):
            for message in messages:
                if message.id > after.id:
                    yield message
        self.channels[900] = NS(history=history)
        self.cog.sync_event = AsyncMock(side_effect=forbidden())
        await self.cog.scan_watches()
        self.assertEqual(self.cog.store.one('SELECT * FROM EventWatches')['last_message_id'], 2)
        self.assertEqual(len(self.cog.store.rows('SELECT * FROM WatchFailures')),1)
        self.cog.sync_event.side_effect = None
        from network_cog import NetworkCog
        await NetworkCog(self.bot).tick()
        self.assertEqual(self.cog.store.rows('SELECT * FROM WatchFailures'),[])
        await self.cog.scan_watches()
        self.assertEqual(self.cog.store.one('SELECT * FROM EventWatches')['last_message_id'], 2)
        self.cog.sync_event.assert_awaited_with(2, key)
        count = self.cog.sync_event.await_count
        await self.cog.scan_watches()
        self.assertEqual(self.cog.sync_event.await_count, count)

    async def test_fixable_watch_setup_failure_is_durable(self):
        key=self.cog.store.share(100)
        self.cog.store.write('INSERT INTO EventWatches VALUES (2,900,0)')
        async def history(**kwargs):
            if kwargs['after'].id<1:yield NS(id=1,content='mirrorkey:'+key)
        self.channels[900]=NS(history=history)
        self.cog.sync_event=AsyncMock(side_effect=ValueError('Configure forum'))
        await self.cog.scan_watches()
        self.assertEqual(len(self.cog.store.rows('SELECT * FROM WatchFailures')),1)
        from network_cog import NetworkCog
        await NetworkCog(self.bot).tick()
        self.assertEqual(len(self.cog.store.rows('SELECT * FROM WatchFailures')),1)
        self.cog.sync_event.side_effect=None
        await NetworkCog(self.bot).tick()
        self.assertEqual(self.cog.store.rows('SELECT * FROM WatchFailures'),[])

    async def test_push_publishes_once_and_only_discovery(self):
        from network_cog import NetworkCog
        self.channels[800]=NS(send=AsyncMock(return_value=NS(id=801)))
        self.cog.store.write('INSERT INTO NetworkSettings VALUES (1,800)')
        network=NetworkCog(self.bot)
        await network.tick();await network.tick()
        self.channels[800].send.assert_awaited_once()
        self.assertIn('mirrorkey:',self.channels[800].send.call_args.args[0])
        self.cog.store.touch(100)
        await network.tick()
        self.channels[800].send.assert_awaited_once()

    async def test_blocked_server_cannot_resync(self):
        from event_store import InvalidShare
        self.cog.store.write('INSERT INTO EventBlocks VALUES (100,2)')
        with self.assertRaises(InvalidShare):await self.cog.sync_event(2,self.cog.store.share(100))

    async def test_missing_mirror_is_recreated(self):
        self.cog.store.add_copy(100,2,200,200,22)
        old=self.make_thread(200)
        old.fetch_message.side_effect=discord.NotFound(NS(status=404,reason='Not Found'),'missing')
        new=self.make_thread(201)
        forum=NS(create_thread=AsyncMock(return_value=NS(thread=new,message=NS(id=201))))
        self.cog.validate_destinations=AsyncMock(return_value=forum)
        thread,created=await self.cog.sync_event(2,self.cog.store.share(100))
        self.assertEqual(thread,201)
        self.assertTrue(created)
        self.assertEqual(len(self.cog.store.copies(100)),2)
        self.assertEqual(self.cog.store.resolve(201),100)

    async def test_inline_signup_saves_spec_and_canonical_player(self):
        from signup_picker import SignupPicker
        picker=SignupPicker(self.raids,100,99,'Warden');picker.spec=2
        i=NS(user=NS(id=99,display_name='Warden',roles=[]),guild=NS(id=2),channel=self.thread,response=NS(edit_message=AsyncMock(),send_message=AsyncMock()))
        await picker.submit.callback(i)
        self.assertEqual(self.cog.store.one('SELECT raid_id,player_id,Warden FROM Players'),dict(raid_id=100,player_id=99,Warden=1))
        self.assertEqual(self.conn.execute('SELECT Warden FROM Specs WHERE player_id=99').fetchone()[0],2)
        i.response.edit_message.assert_awaited_once()

    async def test_calendar_includes_mirror_and_omits_ended_events(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        calendar = CalendarCog(self.bot)
        embed = calendar.calendar_embed(2)
        self.assertEqual(len(embed.fields), 1)
        self.assertIn('/2/200/200', embed.fields[0].value)
        self.cog.store.write('UPDATE Raids SET time=?', (int(time.time())-5000,))
        self.assertEqual(len(calendar.calendar_embed(2).fields), 0)


if __name__ == '__main__':
    unittest.main()
