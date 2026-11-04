import builtins
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
builtins._ = getattr(builtins, '_', lambda value: value)


def emoji(name):
    return NS(name=name, delete=AsyncMock())


class SpecEmojiTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_only_changed_single_spec_art(self):
        import sqlite3
        from raid_cog import RaidCog
        with tempfile.TemporaryDirectory() as folder:
            for stem in ('Warden_Blue', 'Lore-master_Red', 'Warden_Red_Blue', 'Warden'):
                (Path(folder) / f'{stem}.webp').write_bytes(b'img')
            old_blue, old_combo = emoji('Warden_Blue'), emoji('Warden_Red_Blue')
            raids = RaidCog.__new__(RaidCog)
            raids.role_names = ['Warden', 'Loremaster']
            raids.conn = sqlite3.connect(':memory:')
            raids.bot = NS(fetch_application_emojis=AsyncMock(return_value=[old_blue, old_combo]),
                           create_application_emoji=AsyncMock())
            replaced = await raids.sync_spec_emojis(folder)
            self.assertEqual(sorted(replaced), ['Loremaster_Red', 'Warden_Blue'])
            old_blue.delete.assert_awaited_once()
            old_combo.delete.assert_not_awaited()
            raids.bot.fetch_application_emojis.return_value = [emoji('Warden_Blue'), emoji('Loremaster_Red')]
            self.assertEqual(await raids.sync_spec_emojis(folder), [])

    async def test_reload_emojis_prefers_application_emojis(self):
        from raid_cog import RaidCog
        raids = RaidCog.__new__(RaidCog)
        raids.role_names = ('Warden',)
        raids.creep_names = ()
        raids.class_emojis, raids.creep_emojis = [], []
        raids.spec_emojis = [NS(name='Warden_Blue', __str__=lambda self: 'old')]
        new = NS(name='Warden_Blue')
        raids.bot = NS(fetch_application_emojis=AsyncMock(return_value=[new]))
        self.assertTrue(await raids.reload_emojis())
        self.assertEqual(raids.emojis_dict['Warden_Blue'], str(new))


if __name__ == '__main__':
    unittest.main()
