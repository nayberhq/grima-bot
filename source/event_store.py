"""Durable forum projections of Saruman's existing canonical raid records."""
import secrets
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def forum_title(title, timestamp, timezone):
    day = datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).strftime('%A')
    return f'{day} {title}'[:100]


def mirror_keys(source):
    """Extract bearer share codes from a string or Discord Message (including embeds), preserving order and ignoring duplicates."""
    if isinstance(source, str):
        text = source
    else:
        parts = [getattr(source, 'content', '') or '']
        for embed in getattr(source, 'embeds', None) or ():
            for attr in ('title', 'description', 'url'):
                val = getattr(embed, attr, None)
                if val:
                    parts.append(str(val))
            footer = getattr(embed, 'footer', None)
            if footer and getattr(footer, 'text', None):
                parts.append(str(footer.text))
            author = getattr(embed, 'author', None)
            if author and getattr(author, 'name', None):
                parts.append(str(author.name))
            for field in getattr(embed, 'fields', None) or ():
                if getattr(field, 'name', None):
                    parts.append(str(field.name))
                if getattr(field, 'value', None):
                    parts.append(str(field.value))
        text = '\n'.join(parts)
    return list(dict.fromkeys(re.findall(r'(?<![\w-])mirrorkey:([A-Za-z0-9_-]{32})(?![\w-])', text)))


def extract_origin_badge(message, bot=None):
    """Extract (kin_name, emoji_mention, icon_url) from a relayed network embed message."""
    for embed in getattr(message, 'embeds', None) or ():
        author = getattr(embed, 'author', None)
        if not author:
            continue
        kin_name = (getattr(author, 'name', None) or '').strip()
        icon_url = (getattr(author, 'icon_url', None) or getattr(author, 'proxy_icon_url', None) or '').strip()
        emoji_str = None
        m = re.search(r'/emojis/(\d+)\.(png|webp|gif|jpg)', icon_url, re.I)
        if m:
            emoji_id = int(m.group(1))
            ext = m.group(2).lower()
            found_emoji = bot.get_emoji(emoji_id) if (bot and hasattr(bot, 'get_emoji')) else None
            if found_emoji:
                emoji_str = str(found_emoji)
            else:
                slug = re.sub(r'[^a-z0-9]+', '_', kin_name.lower()).strip('_') or 'kin'
                safe_name = f"net_{slug}_{str(emoji_id)[-6:]}"[:32]
                prefix = 'a' if ext == 'gif' else ''
                emoji_str = f'<{prefix}:{safe_name}:{emoji_id}>'
        if kin_name or emoji_str:
            return kin_name or None, emoji_str, icon_url or None
    return None, None, None


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
            CREATE TABLE IF NOT EXISTS EventTemplates(guild_id INTEGER,kind TEXT,body TEXT NOT NULL,PRIMARY KEY(guild_id,kind));
            CREATE TABLE IF NOT EXISTS NetworkSettings(guild_id INTEGER PRIMARY KEY,push_id INTEGER);
            CREATE TABLE IF NOT EXISTS NetworkPush(raid_id INTEGER PRIMARY KEY,message_id INTEGER,error TEXT);
            CREATE TABLE IF NOT EXISTS PendingPublications(guild_id INTEGER,source_id INTEGER,message_id INTEGER,PRIMARY KEY(guild_id,source_id,message_id));
            CREATE TABLE IF NOT EXISTS WatchFailures(guild_id INTEGER,source_id INTEGER,message_id INTEGER,code TEXT,error TEXT,PRIMARY KEY(guild_id,source_id,message_id,code));
            CREATE TABLE IF NOT EXISTS EventBlocks(raid_id INTEGER,guild_id INTEGER,PRIMARY KEY(raid_id,guild_id));
            CREATE TABLE IF NOT EXISTS EventMeeting(raid_id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL);
        ''')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='SourceInvites'").fetchone():
            conn.execute('CREATE TABLE SourceInvites(guild_id INTEGER PRIMARY KEY,url TEXT NOT NULL)')
            conn.execute('UPDATE SharedEvents SET revision=revision+1')
        conn.execute('CREATE TABLE IF NOT EXISTS GuildIdentity(guild_id INTEGER PRIMARY KEY,kin_name TEXT,emoji TEXT)')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(EventCopies)')}
        if 'ended_rendered' not in columns:
            conn.execute('ALTER TABLE EventCopies ADD COLUMN ended_rendered INTEGER NOT NULL DEFAULT 0')
        es_cols = {row[1] for row in conn.execute('PRAGMA table_info(EventSettings)')}
        for col, col_def in (
            ('forum_mode', "TEXT NOT NULL DEFAULT 'single'"),
            ('instance_forum_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('t1_forum_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('t2_forum_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('t3_forum_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('t4_forum_id', 'INTEGER NOT NULL DEFAULT 0'),
            ('t5_forum_id', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if col not in es_cols:
                conn.execute(f'ALTER TABLE EventSettings ADD COLUMN {col} {col_def}')
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

    def set_guild_identity(self, guild_id, kin_name=None, emoji=None):
        existing = self.one('SELECT * FROM GuildIdentity WHERE guild_id=?', (guild_id,))
        new_name = kin_name if kin_name else (existing['kin_name'] if existing else None)
        new_emoji = emoji if emoji else (existing['emoji'] if existing else None)
        if new_name or new_emoji:
            self.write('INSERT OR REPLACE INTO GuildIdentity VALUES (?,?,?)', (guild_id, new_name, new_emoji))

    def guild_badge(self, guild_id, fallback_name=None):
        ident = self.one('SELECT kin_name, emoji FROM GuildIdentity WHERE guild_id=?', (guild_id,))
        if ident and ident.get('emoji'):
            return ident['emoji']
        settings_icon = self.one('SELECT icon FROM Settings WHERE guild_id=?', (guild_id,))
        if settings_icon and settings_icon.get('icon'):
            return settings_icon['icon']
        if ident and ident.get('kin_name'):
            return f"**[{ident['kin_name']}]**"
        if fallback_name:
            return f"**[{fallback_name}]**"
        return None

    def configure(self, guild_id, forum_id, category_id, duration, lock_delay,
                  forum_mode='single', instance_forum_id=0,
                  t1_forum_id=0, t2_forum_id=0, t3_forum_id=0, t4_forum_id=0, t5_forum_id=0):
        if not 900 <= duration <= 86400 or not 0 <= lock_delay <= 604800:
            raise ValueError('Duration must be 15–1440 minutes; lock delay 0–10080 minutes.')
        self.write('''INSERT OR REPLACE INTO EventSettings
            (guild_id, forum_id, voice_category_id, duration, lock_delay,
             forum_mode, instance_forum_id, t1_forum_id, t2_forum_id, t3_forum_id, t4_forum_id, t5_forum_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (guild_id, forum_id, category_id, duration, lock_delay,
             forum_mode, instance_forum_id, t1_forum_id, t2_forum_id, t3_forum_id, t4_forum_id, t5_forum_id))

    @staticmethod
    def select_forum_id(settings, size=12, tier=None):
        mode = settings.get('forum_mode') or 'single'
        if mode == 'instance_raid':
            if (size or 12) <= 6 and settings.get('instance_forum_id'):
                return settings['instance_forum_id']
            return settings['forum_id']
        if mode == 'tiers':
            digits = ''.join(filter(str.isdigit, str(tier or '')))
            tier_num = int(digits) if digits else 0
            col = f't{tier_num}_forum_id'
            if 1 <= tier_num <= 5 and settings.get(col):
                return settings[col]
            return settings['forum_id']
        return settings['forum_id']

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
