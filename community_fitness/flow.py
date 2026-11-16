import json
from collections import defaultdict
from datetime import datetime, timezone

from .analytics import Analytics
from .errors import DomainError, require
from .storage import Store
from .validation import instant, zone


CAVEATS = (
    'tokens are anonymous: one person may hold several tokens and one token may be '
    'shared by a group, so token counts are not person counts',
    'a row count above one means the token stood for a group; visits sum row counts '
    'and may exceed token counts, while flow is counted per token contact',
    'a token seen on several local dates may be reissued or shared; cross-day repeats '
    'are an upper bound on returning people',
    'transition direction follows record timestamps; a tea-then-tourism sequence does '
    'not by itself prove the tea experience caused the consultation',
    'journeys are built only from records inside the window; contacts outside the '
    'window are not linked',
    'records arriving late can extend or reorder journeys; versioned queries freeze '
    'the record set at the snapshot publication time',
    'replayed batches are deduplicated by encounter id and never create additional '
    'journeys',
)


def _window_bound(value: str, label: str, local) -> datetime:
    require(isinstance(value, str), 'invalid_timestamp', f'{label} must be ISO 8601')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise DomainError('invalid_timestamp', f'{label} must be ISO 8601') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=local)
    return parsed.astimezone(timezone.utc)


def _differences(zones: list[dict], totals: dict, reference: dict) -> list[dict]:
    differences = []
    got_zones = {row['code']: row for row in zones}
    ref_zones = {row['code']: row for row in reference['zones']}
    for code in sorted(set(got_zones) | set(ref_zones)):
        got, want = got_zones.get(code, {}), ref_zones.get(code, {})
        for field in ('encounters', 'visits', 'unique_visitors'):
            if got.get(field, 0) != want.get(field, 0):
                differences.append({'scope': code, 'field': field,
                                    'flow': got.get(field, 0), 'reference': want.get(field, 0)})
    for field, key in (('encounters', 'total_encounters'), ('visits', 'total_visits'),
                       ('unique_visitors', 'unique_visitors')):
        if totals[field] != reference[key]:
            differences.append({'scope': 'totals', 'field': key,
                                'flow': totals[field], 'reference': reference[key]})
    return differences


class Flow:
    def __init__(self, store: Store):
        self.store = store
        self.analytics = Analytics(store)

    def zone_flow(self, event_id: str, start: str | None = None, end: str | None = None,
                  version: int | None = None) -> dict:
        with self.store.connection() as db:
            event = self.store.one(db, 'SELECT * FROM events WHERE id=?', (event_id,))
            local = zone(event['timezone'])
            start_utc = _window_bound(start, 'start', local) if start is not None else None
            end_utc = _window_bound(end, 'end', local) if end is not None else None
            if start_utc is not None and end_utc is not None:
                require(start_utc < end_utc, 'invalid_interval', 'Window start must precede end')
            snapshot = None
            cutoff = None
            if version is not None:
                require(type(version) is int and version > 0, 'invalid_version',
                        'Version must be positive')
                row = db.execute('SELECT payload FROM snapshots WHERE event_id=? AND version=? '
                                 'AND status=?', (event_id, version, 'published')).fetchone()
                if not row:
                    raise DomainError('snapshot_not_found', 'Published snapshot does not exist', 404)
                snapshot = json.loads(row['payload'])
                cutoff = instant(snapshot['published_at'])
            rows = self.store.many(db, 'SELECT * FROM encounters WHERE event_id=? ORDER BY id',
                                   (event_id,))
            total_records = len(rows)
            if cutoff is not None:
                rows = [row for row in rows if instant(row['recorded_at']) <= cutoff]
            excluded = total_records - len(rows)
            if start_utc is not None or end_utc is not None:
                rows = [row for row in rows
                        if (start_utc is None or instant(row['occurred_at']) >= start_utc)
                        and (end_utc is None or instant(row['occurred_at']) < end_utc)]

            zones = self.store.many(db, 'SELECT * FROM service_zones WHERE event_id=? '
                                    'ORDER BY code', (event_id,))
            zone_encounters: dict[str, int] = defaultdict(int)
            zone_visits: dict[str, int] = defaultdict(int)
            zone_tokens: dict[str, set] = defaultdict(set)
            zone_token_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            by_token: dict[str, list] = defaultdict(list)
            for row in rows:
                zone_encounters[row['zone_code']] += 1
                zone_visits[row['zone_code']] += row['count']
                zone_tokens[row['zone_code']].add(row['visitor_token'])
                zone_token_counts[row['zone_code']][row['visitor_token']] += 1
                by_token[row['visitor_token']].append(row)
            for token_rows in by_token.values():
                token_rows.sort(key=lambda row: (instant(row['occurred_at']), row['id']))

            rendered_zones = []
            for item in zones:
                code = item['code']
                tokens = zone_tokens.get(code, set())
                rendered_zones.append({
                    'code': code, 'label': item['label'], 'kind': item['kind'],
                    'encounters': zone_encounters[code], 'visits': zone_visits[code],
                    'unique_visitors': len(tokens),
                    'repeat_tokens': sum(1 for token in tokens
                                         if zone_token_counts[code][token] >= 2)})

            transitions: dict[tuple, dict] = {}
            for token, sequence in by_token.items():
                for previous, current in zip(sequence, sequence[1:]):
                    if previous['zone_code'] == current['zone_code']:
                        continue
                    key = (previous['zone_code'], current['zone_code'])
                    entry = transitions.setdefault(key, {'tokens': set(), 'pairs': 0,
                                                         'ambiguous_pairs': 0})
                    entry['tokens'].add(token)
                    entry['pairs'] += 1
                    if instant(previous['occurred_at']) == instant(current['occurred_at']):
                        entry['ambiguous_pairs'] += 1
            rendered_transitions = [
                {'from_zone': pair[0], 'to_zone': pair[1], 'tokens': len(entry['tokens']),
                 'pairs': entry['pairs'], 'ambiguous_pairs': entry['ambiguous_pairs']}
                for pair, entry in sorted(transitions.items())]

            repeat_tokens = same_zone = multi_zone = cross_day = 0
            for sequence in by_token.values():
                if len(sequence) >= 2:
                    repeat_tokens += 1
                counts = defaultdict(int)
                days = set()
                for row in sequence:
                    counts[row['zone_code']] += 1
                    days.add(instant(row['occurred_at']).astimezone(local).date())
                if any(count >= 2 for count in counts.values()):
                    same_zone += 1
                if len(counts) >= 2:
                    multi_zone += 1
                if len(days) >= 2:
                    cross_day += 1

            event_end = instant(event['ends_at'])
            late = sum(1 for row in rows if instant(row['recorded_at']) > event_end)
            totals = {'encounters': len(rows),
                      'visits': sum(row['count'] for row in rows),
                      'unique_visitors': len(by_token)}

            windowed = start_utc is not None or end_utc is not None
            if snapshot is not None:
                basis, reference = 'snapshot', snapshot['services']
            else:
                basis, reference = 'live', self.analytics._services(db, event)
            differences = _differences(rendered_zones, totals, reference) if not windowed else []
            reconciliation = {'basis': basis,
                              'version': version if snapshot is not None else None,
                              'compared': not windowed,
                              'matches': (not differences) if not windowed else None,
                              'differences': differences}
            if windowed:
                reconciliation['note'] = ('windowed subsets are not reconciled against '
                                          'full-event statistics')

            return {'event_id': event_id, 'timezone': event['timezone'],
                    'window': {'start': start, 'end': end,
                               'start_local': start_utc.astimezone(local).isoformat()
                               if start_utc is not None else None,
                               'end_local': end_utc.astimezone(local).isoformat()
                               if end_utc is not None else None,
                               'semantics': 'half-open [start, end) on occurred_at; '
                                            'naive times use the event timezone'},
                    'as_of': {'version': version, 'published_at': snapshot['published_at'],
                              'source_revision': snapshot['event']['revision'],
                              'newer_records_excluded': excluded} if snapshot is not None else None,
                    'zones': rendered_zones, 'totals': totals,
                    'transitions': rendered_transitions,
                    'repeat': {'tokens': len(by_token),
                               'single_contact_tokens': len(by_token) - repeat_tokens,
                               'repeat_tokens': repeat_tokens,
                               'same_zone_repeat_tokens': same_zone,
                               'multi_zone_tokens': multi_zone,
                               'cross_day_tokens': cross_day},
                    'late_records': {'recorded_after_event_end': late},
                    'reconciliation': reconciliation,
                    'caveats': list(CAVEATS)}
