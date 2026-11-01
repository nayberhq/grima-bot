import sqlite3
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'source'))
from durable_send import DurableSender

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_accepted_send_after_restart(self):
        conn=sqlite3.connect(':memory:');bot=NS(user=NS(id=9))
        sender=DurableSender(conn,bot)
        conn.execute("INSERT INTO SendIntents VALUES ('event:1',10,'body',100,NULL)");conn.commit()
        message=NS(id=20,author=NS(id=9),content='body')
        async def history(**kwargs):yield message
        channel=NS(id=10,history=history,send=AsyncMock())
        result=await DurableSender(conn,bot).send(channel,'event:1','edited body')
        self.assertEqual(result.id,20);channel.send.assert_not_awaited()
        self.assertEqual(conn.execute('SELECT message_id FROM SendIntents').fetchone()[0],20)
        conn.close()

    async def test_history_failure_does_not_duplicate(self):
        conn=sqlite3.connect(':memory:');sender=DurableSender(conn,NS(user=NS(id=9)))
        conn.execute("INSERT INTO SendIntents VALUES ('event:1',10,'body',100,NULL)");conn.commit()
        async def history(**kwargs):
            raise RuntimeError('history unavailable')
            yield
        channel=NS(id=10,history=history,send=AsyncMock())
        with self.assertRaises(RuntimeError):await sender.send(channel,'event:1','body')
        channel.send.assert_not_awaited();conn.close()
