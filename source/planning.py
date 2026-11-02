"""Pure template rendering and Vanguard session interchange (versions 6/7)."""
import re
import math
from string import Formatter

CLASSES = {
    'Beorning': [('Blue','Tank'),('Yellow','Healer'),('Red','DPS')],
    'Brawler': [('Blue','Tank'),('Red','DPS')],
    'Burglar': [('Yellow','Debuffer'),('Red','DPS'),('Blue','DPS')],
    'Captain': [('Yellow','Tank'),('Blue','Healer'),('Red','Buffer')],
    'Champion': [('Blue','Tank'),('Red','DPS'),('Yellow','DPS')],
    'Guardian': [('Blue','Tank'),('Red','DPS')],
    'Hunter': [('Blue','DPS'),('Red','DPS'),('Yellow','Debuffer')],
    'Lore-master': [('Blue','Buffer'),('Red','DPS'),('Yellow','Debuffer')],
    'Mariner': [('Blue','Buffer'),('Yellow','Debuffer'),('Red','DPS')],
    'Minstrel': [('Blue','Healer'),('Red','DPS')],
    'Rune-keeper': [('Blue','Healer'),('Red','DPS'),('Yellow','DPS')],
    'Warden': [('Blue','Tank'),('Red','DPS')],
}
TAGS = {'title', 'tier', 'description', 'start', 'end', 'organizer', 'signup_count'}


def render_template(template, context, limit=1800):
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None and (field not in TAGS or spec or conversion):
                raise ValueError('Use only these tags: ' + ', '.join(sorted(TAGS)))
        result = template.format_map(context)
    except (KeyError, IndexError) as exc:
        raise ValueError('Invalid placeholder.') from exc
    if len(result) > limit:
        raise ValueError(f'Rendered text exceeds {limit} characters.')
    return result


def context_for(event, count):
    return dict(title=event['name'], tier=event.get('tier') or '',
                description=event.get('boss') or '', start=f"<t:{event['time']}:F>",
                end=f"<t:{event['time'] + event['duration']}:F>",
                organizer=f"<@{event['organizer_id']}>", signup_count=str(count))


def signup_choices(player, specs, tier):
    result = []
    for c, choices in CLASSES.items():
        key = next((k for k in player if k.replace('-', '').lower() == c.replace('-', '').lower()), None)
        if not key or not player[key]:
            continue
        mask = (int(specs.get(key) or 0) >> (max(tier, 1)-1)*3) & 7
        for spec, role in choices:
            # No saved spec means every supported specialization is offered to the planner.
            if not mask or mask & {'Red':1, 'Blue':2, 'Yellow':4}[spec]:
                result.append(dict(c=c, spec=spec, role=role))
    return result


def planner_identity(player):
    """Network kin markers become planner priority, never part of the name."""
    raw = str(player['byname'])
    primary = bool(re.search(r'<a?:net_[a-z0-9_]+:\d+>', raw, re.I))
    name = re.sub(r'<a?:[a-z0-9_]+:\d+>', ' ', raw, flags=re.I)
    name = ' '.join(name.split()) or str(player['player_id'])
    return dict(name=name, primary=primary)


def export_session(event, players, specs):
    if event.get('creep'):
        raise ValueError('Vanguard supports free-people classes only.')
    if not 3 <= event['size'] <= 24 or not .1 <= event['duration']/3600 <= 6:
        raise ValueError('Vanguard requires size 3–24 and duration up to six hours.')
    tier = int(''.join(re.findall(r'\d', event.get('tier') or '')) or 1)
    roster = []
    for p in players:
        choices = signup_choices(p, specs.get(p['player_id'], {}), tier)
        if not choices:
            raise ValueError(f"No supported class choices for {p['byname']}.")
        roster.append(dict(id=str(p['player_id']), **planner_identity(p), choices=choices,
                           available=[True], max=1))
    return dict(format='vanguard-raid-planner-session', version=6, players=roster,
                runs=[dict(id=1, time=0, label=event['name'], source=0)],
                template={r:[0,0] for r in ('Tank','Healer','Debuffer','Buffer')},
                settings=dict(size=event['size'], duration=event['duration']/3600,
                              fair=True, suggestSchedule=False, lockPriority='gearing', recommendationRunLimit=1),
                teams=[], recommendations=[], hasGenerated=False, dirty=False,
                message='', grimaEventId=str(event['raid_id']))


def import_plan(data, event, players, specs=None, require_identity=False):
    if not isinstance(data, dict) or data.get('format') != 'vanguard-raid-planner-session' or data.get('version') not in (6,7):
        raise ValueError('Upload a Vanguard session JSON (version 6 or 7).')
    if require_identity and data.get('grimaEventId') != str(event['raid_id']):
        raise ValueError('This plan lacks the matching Grima event identity. Export fresh signups and load them in the updated planner.')
    if data.get('grimaEventId', str(event['raid_id'])) != str(event['raid_id']):
        raise ValueError('This export belongs to another event.')
    if data.get('dirty') or data.get('hasGenerated') is False:
        raise ValueError('Regenerate the plan in Vanguard before exporting it.')
    teams = data.get('teams')
    if not isinstance(teams, list) or not 1 <= len(teams) <= 16:
        raise ValueError('Generate a plan with 1–16 teams before importing.')
    known = {str(p['player_id']): p for p in players}
    result = []
    for team in teams:
        if not isinstance(team, dict) or not isinstance(team.get('members'), list) or len(team['members']) > event['size']:
            raise ValueError('Invalid team or too many members.')
        seen, members = set(), []
        for member in team['members']:
            if not isinstance(member, dict):
                raise ValueError('Invalid member.')
            pid, c, spec = member.get('id'), member.get('c'), member.get('spec')
            if not isinstance(pid, str) or pid not in known or pid in seen:
                raise ValueError('Every member must be a current signup, with no duplicates within a team.')
            if not isinstance(c, str) or not isinstance(spec, str):
                raise ValueError('Invalid class or specialization.')
            role = dict(CLASSES.get(c, [])).get(spec)
            if not role or member.get('role') != role:
                raise ValueError('Invalid class, specialization or role.')
            p = known[pid]
            if not any(v for k,v in p.items() if k.replace('-', '').lower() == c.replace('-', '').lower()):
                raise ValueError(f"{p['byname']} did not sign up for {c}.")
            if specs is not None:
                tier=int(''.join(re.findall(r'\d',event.get('tier') or '')) or 1)
                offered=signup_choices(p,specs.get(p['player_id'],{}),tier)
                if not any(o['c']==c and o['spec']==spec for o in offered):
                    raise ValueError(f"{p['byname']} did not offer {c} {spec} for this tier.")
            seen.add(pid)
            members.append(dict(id=pid, name=p['byname'], c=c, spec=spec, role=role))
        label = team.get('label')
        if not isinstance(label, str) or len(label) > 100:
            raise ValueError('Team labels must be at most 100 characters.')
        groups = team.get('grimaFellowships')
        if groups is not None:
            ids = [p['id'] for p in members]
            if not isinstance(groups,list) or any(not isinstance(g,list) or not 1<=len(g)<=6 or any(not isinstance(x,str) for x in g) for g in groups):
                raise ValueError('Invalid fellowship groups.')
            flat = [pid for group in groups for pid in group]
            if sorted(flat)!=sorted(ids):
                raise ValueError('Fellowships must contain every team member exactly once.')
            lookup={p['id']:p for p in members}
            members=[lookup[pid] for pid in flat]
        hour = team.get('time')
        if hour is not None and (type(hour) not in (int, float) or not math.isfinite(hour) or not 0 <= hour < 48):
            raise ValueError('Invalid planner time.')
        result.append(dict(label=label, members=members, time=hour, fellowship_sizes=[len(g) for g in groups] if groups is not None else None))
    return result


PLANNER_URL = 'https://nayberhq.github.io/vanguard-raid-planner/'


# GS1 bit positions are a wire contract: append/change only under a new version.
SIGNUP_CHOICES = [('Beorning', 'Blue', 'Tank'), ('Beorning', 'Yellow', 'Healer'), ('Beorning', 'Red', 'DPS'), ('Brawler', 'Blue', 'Tank'), ('Brawler', 'Red', 'DPS'), ('Burglar', 'Yellow', 'Debuffer'), ('Burglar', 'Red', 'DPS'), ('Burglar', 'Blue', 'DPS'), ('Captain', 'Yellow', 'Tank'), ('Captain', 'Blue', 'Healer'), ('Captain', 'Red', 'Buffer'), ('Champion', 'Blue', 'Tank'), ('Champion', 'Red', 'DPS'), ('Champion', 'Yellow', 'DPS'), ('Guardian', 'Blue', 'Tank'), ('Guardian', 'Red', 'DPS'), ('Hunter', 'Blue', 'DPS'), ('Hunter', 'Red', 'DPS'), ('Hunter', 'Yellow', 'Debuffer'), ('Lore-master', 'Blue', 'Buffer'), ('Lore-master', 'Red', 'DPS'), ('Lore-master', 'Yellow', 'Debuffer'), ('Mariner', 'Blue', 'Buffer'), ('Mariner', 'Yellow', 'Debuffer'), ('Mariner', 'Red', 'DPS'), ('Minstrel', 'Blue', 'Healer'), ('Minstrel', 'Red', 'DPS'), ('Rune-keeper', 'Blue', 'Healer'), ('Rune-keeper', 'Red', 'DPS'), ('Rune-keeper', 'Yellow', 'DPS'), ('Warden', 'Blue', 'Tank'), ('Warden', 'Red', 'DPS')]


def compact_signup_bytes(session):
    """Lossless transport for fresh, single-run Grima signup exports only."""
    import struct
    if not isinstance(session, dict) or session.get('format') != 'vanguard-raid-planner-session' or session.get('version') != 6 or session.get('hasGenerated') is not False or session.get('teams') or len(session.get('runs', [])) != 1:
        return None
    # Do not compact arbitrary saved planner sessions or discard their settings.
    allowed = {'format','version','players','runs','template','settings','teams','recommendations','hasGenerated','dirty','message','grimaEventId'}
    if set(session) - allowed or session.get('dirty') or session.get('recommendations') or session.get('message'):
        return None
    event = session['runs'][0]
    settings = session['settings']
    if event != dict(id=1, time=0, label=event['label'], source=0) or session['template'] != {r:[0,0] for r in ('Tank','Healer','Debuffer','Buffer')}:
        return None
    if settings != dict(size=settings['size'], duration=settings['duration'], fair=True, suggestSchedule=False, lockPriority='gearing', recommendationRunLimit=1):
        return None
    rows=session['players']
    if len(rows)>2000:
        raise ValueError('At most 2000 signups are supported.')
    out=bytearray(b'GS1')
    def uint(n):
        if n<0: raise ValueError('Invalid unsigned value.')
        while n>=128:
            out.append((n & 127)|128);n >>= 7
        out.append(n)
    def string(value):
        raw=value.encode('utf-8');uint(len(raw));out.extend(raw)
    try:
        out.extend(struct.pack('<Q',int(session['grimaEventId'])))
        uint(settings['size'])
        seconds=round(settings['duration']*3600)
        if seconds/3600 != settings['duration']: return None
        uint(seconds);string(event['label']);uint(len(rows))
        for p in rows:
            if set(p)-{'id','name','choices','available','max','primary'} or p['available'] != [True] or p['max'] != 1:
                return None
            mask=0
            for choice in p['choices']:
                if set(choice)!={'c','spec','role'}:return None
                mask |= 1 << SIGNUP_CHOICES.index((choice['c'],choice['spec'],choice['role']))
            out.extend(struct.pack('<Q',int(p['id'])))
            string(p['name']);uint(mask);out.append(1 if p.get('primary',False) else 0)
    except (ValueError,KeyError,TypeError,OverflowError,struct.error):
        return None
    return bytes(out)


def planner_link(session, max_length=1650):
    """GS1 binary + zlib + base64url. Full JSON remains the compatibility fallback."""
    import base64,json,zlib
    raw=compact_signup_bytes(session)
    if raw is None:
        raw=json.dumps(session,separators=(',', ':'),ensure_ascii=False).encode()
    token=base64.urlsafe_b64encode(zlib.compress(raw,9)).decode().rstrip('=')
    url=PLANNER_URL+'#grima='+token
    # Vanguard bounds the encoded token independently of the chosen transport.
    return url if len(url)<=max_length and len(token)<=20000 else None


def decode_plan_code(code):
    import base64, json, zlib
    text = (code or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I | re.S).strip()
    if text.startswith('{'):
        if len(text) > 2_000_000:
            raise ValueError('Plan is too large or invalid.')
        try:
            return json.loads(text)
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Invalid session JSON; export a valid Session JSON or Grima plan code from Vanguard.') from exc
    if not text.startswith('grimaplan:') or len(text)>4000:
        raise ValueError('Paste a Vanguard Session JSON or Grima plan code, or use /planner-import for larger plans.')
    try:
        token=text[len('grimaplan:'):].strip()
        packed=base64.b64decode(token+'='*((-len(token))%4),altchars=b'-_',validate=True)
        decoder=zlib.decompressobj()
        raw=decoder.decompress(packed,2_000_001)
        if len(raw)>2_000_000 or not decoder.eof or decoder.unused_data:
            raise ValueError('Plan is too large or invalid.')
        return json.loads(raw)
    except (ValueError,UnicodeError,zlib.error) as exc:
        raise ValueError('Invalid plan code; export Session JSON instead.') from exc
