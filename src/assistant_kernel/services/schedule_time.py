"""Pure, timezone-explicit schedule arithmetic; no sleeping or business effects."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re
from croniter import croniter

UTC = timezone.utc


def instant(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('An explicit UTC offset is required for an instant')
    return parsed.astimezone(UTC)


def validate_rule(rule, zone):
    ZoneInfo(zone)
    kind = rule.get('kind')
    fields = {'once': {'kind', 'at'}, 'interval': {'kind', 'anchor', 'seconds'}, 'cron': {'kind', 'expression'}}
    if kind not in fields or set(rule) != fields[kind]:
        raise ValueError('Schedule rule has missing or unsupported fields')
    if kind == 'once':
        instant(rule['at'])
    elif kind == 'interval':
        instant(rule['anchor'])
        if type(rule['seconds']) is not int or rule['seconds'] < 1:
            raise ValueError('Interval must be an integer of at least one second')
    elif (len(rule['expression'].split()) != 5
          or re.search(r'(?<![a-z])[rh](?![a-z])', rule['expression'], re.I)
          or not croniter.is_valid(rule['expression'], strict=True)):
        raise ValueError('A valid five-field cron expression is required')


def next_time(rule, zone, after):
    """First strictly later UTC instant. Cron gaps skip; folds choose fold=0."""
    validate_rule(rule, zone)
    after = instant(after)
    if rule['kind'] == 'once':
        target = instant(rule['at'])
        return target if target > after else None
    if rule['kind'] == 'interval':
        anchor = instant(rule['anchor']); seconds = rule['seconds']
        n = max(0, (after - anchor) // timedelta(seconds=seconds) + 1)
        return anchor + timedelta(seconds=n * seconds)
    tz = ZoneInfo(zone)
    local = after.astimezone(tz).replace(tzinfo=None)
    iterator = croniter(rule['expression'], local, day_or=True, max_years_between_matches=8)
    for _ in range(2000):
        naive = iterator.get_next(datetime)
        target = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
        if target.astimezone(tz).replace(tzinfo=None) == naive and target > after:
            return target
    raise ValueError('No supported future time could be resolved')


def preview(rule, zone='Asia/Shanghai', *, after=None, count=5):
    after = instant(after or datetime.now(UTC))
    validate_rule(rule, zone)
    times = []; cursor = after
    for _ in range(count):
        cursor = next_time(rule, zone, cursor)
        if cursor is None:
            break
        times.append({'utc': cursor.isoformat(), 'local': cursor.astimezone(ZoneInfo(zone)).isoformat()})
    if not times:
        raise ValueError('The schedule has no future occurrence; choose a new time')
    descriptions = {'once': 'runs once at the given time', 'interval': 'runs at a fixed start and interval in seconds',
                    'cron': 'runs by a local calendar rule; when both day of month and weekday are restricted, either matches'}
    return {'timezone': zone, 'rule': rule, 'as_of': after.isoformat(), 'times': times,
            'description': descriptions[rule['kind']], 'misfire_policy': 'skip', 'overlap_policy': 'skip'}
