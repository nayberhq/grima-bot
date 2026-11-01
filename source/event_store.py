"""Durable forum projections of Saruman's existing canonical raid records."""
import secrets
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def forum_title(title, timestamp, timezone):
    day = datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).strftime('%A')
    return f'{day} {title}'[:100]


def mirror_keys(text):
    """Extract bearer share codes, preserving order and ignoring duplicate markers."""
    return list(dict.fromkeys(re.findall(r'(?<![\w-])mirrorkey:([A-Za-z0-9_-]{32})(?![\w-])', text)))


class InvalidShare(ValueError):
    """A permanently invalid discovery code."""


class EventStore:
    def __init__(self, conn):
        self.conn = conn
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS EventSettings (
                guild_id INTEGER PRIMARY KEY, forum_id INTEGER NOT NULL,
                voice_category_id INTEGER NOT NULL, duration INTEGER NOT NULL DEFAULT 10800,
                lock_delay INTEGER NOT NULL DEFAULT 3600);
            CREATE TABLE IF NOT EXISTS SharedEvents (
                raid_id INTEGER PRIMARY KEY, duration INTEGER NOT NULL,
                lock_delay INTEGER NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
                share_token TEXT UNIQUE, revision INTEGER NOT NULL DEFAULT 1,
                creep INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS EventCopies (
                raid_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL UNIQUE, message_id INTEGER NOT NULL UNIQUE,
                voice_category_id INTEGER NOT NULL, voice_id INTEGER,
                locked INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                ended_rendered INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (raid_id, guild_id));
            CREATE TABLE IF NOT EXISTS CreationPanels (
                message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL, preset TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS EventAnnouncements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, raid_id INTEGER NOT NULL,
                body TEXT NOT NULL, author_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS AnnouncementDeliveries (
                announcement_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (announcement_id, thread_id));
            CREATE TABLE IF NOT EXISTS EventWatches (
                guild_id INTEGER NOT NULL, source_channel_id INTEGER NOT NULL,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id,source_channel_id));
        ''')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS NetworkSettings(guild_id INTEGER PRIMARY KEY,push_id INTEGER);
            CREATE TABLE IF NOT EXISTS NetworkPush(raid_id INTEGER PRIMARY KEY,message_id INTEGER,error TEXT);
            CREATE TABLE IF NOT EXISTS WatchFailures(guild_id INTEGER,source_id INTEGER,message_id INTEGER,code TEXT,error TEXT,PRIMARY KEY(guild_id,source_id,message_id,code));
            CREATE TABLE IF NOT EXISTS EventBlocks(raid_id INTEGER,guild_id INTEGER,PRIMARY KEY(raid_id,guild_id));
            CREATE TABLE IF NOT EXISTS EventMeeting(raid_id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL);
        ''')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='SourceInvites'").fetchone():
            conn.execute('CREATE TABLE SourceInvites(guild_id INTEGER PRIMARY KEY,url TEXT NOT NULL)')
            conn.execute('UPDATE SharedEvents SET revision=revision+1')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(EventCopies)')}
        if 'ended_rendered' not in columns:
            conn.execute('ALTER TABLE EventCopies ADD COLUMN ended_rendered INTEGER NOT NULL DEFAULT 0')
        conn.commit()

    def rows(self, sql, args=()):
        cursor = self.conn.execute(sql, args)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def write(self, sql, args=()):
        cursor = self.conn.execute(sql, args)
        self.conn.commit()
        return cursor.lastrowid

    def settings(self, guild_id):
        return self.one('SELECT * FROM EventSettings WHERE guild_id=?', (guild_id,))

    def configure(self, guild_id, forum_id, category_id, duration, lock_delay):
        if not 900 <= duration <= 86400 or not 0 <= lock_delay <= 604800:
            raise ValueError('Duration must be 15–1440 minutes; lock delay 0–10080 minutes.')
        self.write('INSERT OR REPLACE INTO EventSettings VALUES (?,?,?,?,?)',
                   (guild_id, forum_id, category_id, duration, lock_delay))

    def event(self, raid_id):
        return self.one('''SELECT r.*, e.duration, e.lock_delay, e.cancelled,
            e.share_token, e.revision, e.creep FROM SharedEvents e
            JOIN Raids r USING (raid_id) WHERE raid_id=?''', (raid_id,))

    def active_events(self):
        return self.rows('''SELECT e.raid_id FROM SharedEvents e WHERE EXISTS
            (SELECT 1 FROM EventCopies c WHERE c.raid_id=e.raid_id
            AND (c.locked=0 OR c.voice_id IS NOT NULL))''')

    def add_event(self, raid_id, settings, creep=False, duration=None):
        self.write('INSERT INTO SharedEvents (raid_id,duration,lock_delay,creep) VALUES (?,?,?,?)',
                   (raid_id, duration or settings['duration'], settings['lock_delay'], int(creep)))

    def add_copy(self, raid_id, guild_id, thread_id, message_id, category_id):
        self.write('''INSERT INTO EventCopies
            (raid_id,guild_id,thread_id,message_id,voice_category_id) VALUES (?,?,?,?,?)''',
                   (raid_id, guild_id, thread_id, message_id, category_id))

    def copies(self, raid_id):
        return self.rows('SELECT * FROM EventCopies WHERE raid_id=?', (raid_id,))

    def resolve(self, message_id):
        row = self.one('SELECT raid_id FROM EventCopies WHERE message_id=? OR thread_id=?',
                       (message_id, message_id))
        return row['raid_id'] if row else message_id

    def touch(self, raid_id):
        self.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id=?', (raid_id,))

    def share(self, raid_id):
        token = secrets.token_urlsafe(24)
        self.write('UPDATE SharedEvents SET share_token=? WHERE raid_id=?', (token, raid_id))
        return token

    def by_token(self, token):
        row = self.one('SELECT raid_id FROM SharedEvents WHERE share_token=?', (token,))
        return self.event(row['raid_id']) if row else None

    @staticmethod
    def ended(event, now):
        return bool(event['cancelled']) or now >= event['time'] + event['duration']

    @staticmethod
    def lock_due(event, now):
        return bool(event['cancelled']) or now >= event['time'] + event['duration'] + event['lock_delay']

    def queue_announcement(self, raid_id, body, author_id):
        # Commit the broadcast and all destinations together, before network I/O.
        with self.conn:
            cursor = self.conn.execute(
                'INSERT INTO EventAnnouncements (raid_id,body,author_id) VALUES (?,?,?)',
                (raid_id, body, author_id))
            self.conn.execute('''INSERT INTO AnnouncementDeliveries (announcement_id,thread_id)
                SELECT ?,thread_id FROM EventCopies WHERE raid_id=? AND locked=0''',
                (cursor.lastrowid, raid_id))
        return cursor.lastrowid

    def pending_announcements(self):
        return self.rows('''SELECT a.*, d.thread_id FROM EventAnnouncements a
            JOIN AnnouncementDeliveries d ON a.id=d.announcement_id WHERE d.sent=0''')
