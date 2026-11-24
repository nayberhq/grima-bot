import builtins
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source'))
_previous_cwd = os.getcwd()
_previous_translate = getattr(builtins, '_', None)
_scratch = tempfile.TemporaryDirectory()
os.chdir(_scratch.name)
for filename in ('common_timezones.txt', 'timezones.txt', 'list-of-raids.csv'):
    Path(filename).write_text((ROOT / 'source' / filename).read_text())
Path('config.json').write_text((ROOT / 'source/example-config.json').read_text())
builtins._ = lambda value: value

from command_panel import SPEC_CHOICES, CommandPanel, ProfileView
from calendar_cog import CalendarCog
from config_cog import ConfigCog
from events_cog import EventsCog
from network_cog import NetworkCog
from planning_cog import PlanningCog
from raid_cog import RaidCog
from time_cog import TimeCog

os.chdir(_previous_cwd)


def tearDownModule():
    _scratch.cleanup()
    if _previous_translate is None and builtins._ is not None and getattr(builtins._, '__code__', None):
        return


def _names(cog_cls):
    return [command.name for command in cog_cls.__cog_app_commands__]


class CommandListTests(unittest.TestCase):
    def test_only_four_slash_commands_stay_registered(self):
        registered = []
        for cog in (EventsCog, ConfigCog, NetworkCog, CalendarCog, PlanningCog, RaidCog, TimeCog, CommandPanel):
            registered.extend(_names(cog))
        self.assertEqual(sorted(registered), ['commands', 'profile', 'settings', 'watch'])
        twitter = (ROOT / 'source' / 'twitter_cog.py').read_text()
        self.assertNotIn('@app_commands.command', twitter)
        self.assertNotIn('GroupCog', twitter)
        self.assertNotIn('@app_commands.command', (ROOT / 'source' / 'treasure_cog.py').read_text())

    def test_catalog_groups_fit_discord_menus(self):
        panel = CommandPanel(NS(get_cog=lambda name: None))
        catalog = panel.catalog()
        self.assertLessEqual(len(catalog), 25)
        names = []
        for _category, items in catalog:
            self.assertLessEqual(len(items), 25)
            names.extend(name for name, _description, _run in items)
        self.assertIn('setup', names)
        self.assertIn('planner-import', names)
        self.assertIn('event-sync', names)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual([value for _label, value in SPEC_CHOICES], [1, 2, 4, 3, 6, 5, 7, 0])


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE Specs (player_id INTEGER PRIMARY KEY, Hunter INTEGER, Guardian INTEGER)')
        self.conn.execute('CREATE TABLE Timezone (player_id INTEGER PRIMARY KEY, timezone TEXT)')
        self.conn.execute('INSERT INTO Specs VALUES (7, 1, 0)')
        self.conn.commit()
        self.raids = NS(specs_respond=AsyncMock(), roles_respond=AsyncMock())
        self.bot = NS(
            conn=self.conn,
            role_names=['Hunter', 'Guardian'],
            get_cog=lambda name: self.raids if name == 'RaidCog' else None,
        )
        self.view = ProfileView(NS(bot=self.bot), 7, 1)

    async def test_profile_shows_saved_spec_and_saves_the_selected_bits(self):
        self.assertIn('Hunter: Red', self.view.embed().description)
        self.assertIn('Personal timezone: server timezone', self.view.embed().description)
        save = next(child for child in self.view.children if getattr(child, 'label', None) == 'Save specialization')
        interaction = NS(response=NS(is_done=lambda: False, send_message=AsyncMock()), followup=NS(send=AsyncMock()))
        await save.callback(interaction)
        args = self.raids.specs_respond.await_args.args
        self.assertEqual(args[1].name, 'Hunter')
        self.assertEqual(args[2].value, 0b001)
        self.assertEqual(args[3], 0)

    async def test_clear_roles_uses_the_existing_role_remover(self):
        clear = next(child for child in self.view.children if getattr(child, 'label', None) == 'Clear class roles')
        await clear.callback(NS())
        self.raids.roles_respond.assert_awaited()

    async def test_personal_timezone_is_saved_on_the_profile(self):
        zone = next(child for child in self.view.children if getattr(child, 'placeholder', None) == 'Personal timezone (optional)')
        zone._values = ['America/New_York']
        interaction = NS(response=NS(edit_message=AsyncMock()))
        await zone.callback(interaction)
        saved = self.conn.execute('SELECT timezone FROM Timezone WHERE player_id=7').fetchone()[0]
        self.assertEqual(saved, 'America/New_York')
        interaction.response.edit_message.assert_awaited()


class PanelHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_import_waits_for_a_json_file(self):
        panel = CommandPanel(NS(get_cog=lambda name: None))
        panel.pending_imports[(1, 2, 3)] = (4, time.time() + 100)
        message = NS(
            author=NS(bot=False, id=3),
            guild=NS(id=1),
            channel=NS(id=2),
            attachments=[NS(filename='notes.txt')],
            reply=AsyncMock(),
        )
        await panel.on_message(message)
        self.assertIn((1, 2, 3), panel.pending_imports)
        message.reply.assert_awaited()

    async def test_manage_server_is_checked_before_setup(self):
        panel = CommandPanel(NS(get_cog=lambda name: None))
        interaction = NS(
            user=NS(guild_permissions=NS(manage_guild=False)),
            response=NS(send_message=AsyncMock(), is_done=lambda: False),
        )
        await panel._open_settings(interaction)
        interaction.response.send_message.assert_awaited()
