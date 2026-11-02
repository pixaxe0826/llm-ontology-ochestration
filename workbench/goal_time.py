"""Goal display time is configurable; deadline comparisons use UTC epochs only."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(name='America/New_York'):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('Use a valid IANA timezone, e.g. America/New_York.') from None


def stamp(epoch, name):
    utc = datetime.fromtimestamp(epoch, timezone.utc)
    return {'utc':utc.isoformat(), 'local':utc.astimezone(zone(name)).isoformat(), 'timezone':name}


def deadline(value, name, now):
    selected = zone(name)
    if value in (None, ''):
        epoch = now + 6*3600
    elif isinstance(value, (int,float)) and not isinstance(value,bool):
        epoch = float(value)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        except ValueError:
            raise ValueError('Use an ISO deadline with an offset, or local time in the selected timezone.') from None
        if dt.tzinfo is None:
            aware = dt.replace(tzinfo=selected,fold=0)
            if aware.astimezone(timezone.utc).astimezone(selected).replace(tzinfo=None) != dt:
                raise ValueError('This local time does not exist due to DST. Choose another time.')
            if dt.replace(tzinfo=selected,fold=1).utcoffset() != aware.utcoffset():
                raise ValueError('This local time is ambiguous due to DST. Include the UTC offset explicitly.')
            dt = aware
        epoch = dt.astimezone(timezone.utc).timestamp()
    try:
        result = stamp(epoch,name)
    except (OverflowError, OSError, ValueError):
        raise ValueError('Invalid deadline.') from None
    return {'deadline_epoch':epoch,'deadline_utc':result['utc'],'deadline_local':result['local']}
