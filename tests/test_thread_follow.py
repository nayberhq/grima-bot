import sys
from pathlib import Path
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from thread_follow import follow_signup, unfollow_all


class ThreadFollowTests(unittest.IsolatedAsyncioTestCase):
    async def test_signup_uses_silent_mention_then_deletes(self):
        msg = NS(delete=AsyncMock())
        channel = Mock(spec=discord.Thread, id=123, send=AsyncMock(return_value=msg))
        i = NS(channel=channel, user=NS(id=9), followup=NS(send=AsyncMock()))
        await follow_signup(i, True)
        channel.send.assert_awaited_once()
        self.assertIn('9', channel.send.call_args.args[0])
        self.assertTrue(channel.send.call_args.kwargs.get('silent'))
        msg.delete.assert_awaited_once()

    async def test_signup_http_failure_warns_without_raising(self):
        channel = Mock(spec=discord.Thread, id=123, send=AsyncMock(
            side_effect=discord.Forbidden(NS(status=403, reason='Forbidden'), 'denied')))
        i = NS(channel=channel, user=NS(id=9), followup=NS(send=AsyncMock()))
        with self.assertLogs('thread_follow', level='WARNING'):
            await follow_signup(i, True)
        self.assertIn('signup change was saved', i.followup.send.call_args.args[0])
        self.assertTrue(i.followup.send.call_args.kwargs['ephemeral'])

    async def test_unsignup_does_not_remove_from_thread(self):
        channel = Mock(spec=discord.Thread, id=123, remove_user=AsyncMock())
        await follow_signup(NS(channel=channel, user=NS(id=9)), False)
        channel.remove_user.assert_not_called()

    async def test_ordinary_channel_is_untouched(self):
        channel = Mock(spec=discord.TextChannel)
        await follow_signup(NS(channel=channel), True)
        self.assertEqual(channel.mock_calls, [])

    async def test_unfollow_all_removes_every_member_except_bot(self):
        thread = Mock(spec=discord.Thread, id=123, remove_user=AsyncMock())
        thread.fetch_members = AsyncMock(return_value=[
            NS(id=1), NS(id=2), NS(id=99),
        ])
        bot = NS(id=99)
        await unfollow_all(thread, bot=bot)
        thread.fetch_members.assert_awaited_once()
        self.assertEqual(thread.remove_user.await_count, 2)
        thread.remove_user.assert_any_await(NS(id=1))
        thread.remove_user.assert_any_await(NS(id=2))

    async def test_unfollow_all_skips_non_threads(self):
        channel = Mock(spec=discord.TextChannel, fetch_members=AsyncMock())
        await unfollow_all(channel)
        channel.fetch_members.assert_not_called()
