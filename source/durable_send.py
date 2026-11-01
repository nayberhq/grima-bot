"""Recover text sends accepted by Discord before the local completion commit."""
import asyncio
from datetime import datetime, timezone
import time
from types import SimpleNamespace
import discord

class DurableSender:
    def __init__(self,conn,bot):
        self.conn,self.bot=conn,bot
        self.lock=asyncio.Lock()
        conn.execute('CREATE TABLE IF NOT EXISTS SendIntents(key TEXT PRIMARY KEY,channel_id INTEGER,body TEXT,started REAL,message_id INTEGER)')
        conn.commit()

    async def send(self,channel,key,body):
        async with self.lock:
            row=self.conn.execute('SELECT channel_id,body,started,message_id FROM SendIntents WHERE key=?',(key,)).fetchone()
            if row:
                if row[0]!=channel.id:raise ValueError('Delivery key was reused for a different channel.')
                body=row[1]  # Retry the original snapshot, even if the event was edited.
                if row[3]:return SimpleNamespace(id=row[3])
                # Refuse to resend if history cannot be read. A failed history request
                # is not proof the prior send failed.
                after=discord.Object(id=discord.utils.time_snowflake(datetime.fromtimestamp(row[2],timezone.utc)))
                count=0
                async for message in channel.history(limit=2001,oldest_first=True,after=after):
                    count+=1
                    if message.author.id==self.bot.user.id and message.content==body:
                        self.conn.execute('UPDATE SendIntents SET message_id=? WHERE key=?',(message.id,key));self.conn.commit()
                        return message
                if count>2000:raise ValueError('Delivery recovery history exceeds 2000 messages; operator review required.')
            else:
                self.conn.execute('INSERT INTO SendIntents VALUES (?,?,?,?,NULL)',(key,channel.id,body,time.time()));self.conn.commit()
            message=await channel.send(body,allowed_mentions=discord.AllowedMentions.none())
            self.conn.execute('UPDATE SendIntents SET message_id=? WHERE key=?',(message.id,key));self.conn.commit()
            return message
