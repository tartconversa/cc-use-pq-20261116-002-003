import json

from community_fitness.errors import DomainError
from community_fitness.flow import Flow
from tests.helpers import Scenario, START, END, TEN, ELEVEN

NINE30 = '2026-10-26T09:30:00+08:00'
NOON = '2026-10-26T12:00:00+08:00'
HALFNOON = '2026-10-26T12:30:00+08:00'
ONE = '2026-10-26T13:00:00+08:00'
FIVE30 = '2026-10-26T17:30:00+08:00'
ZONES = [('tea', 'tea', '2026-10-26T09:00:00+08:00', '2026-10-26T13:00:00+08:00'),
         ('tour', 'tourism', '2026-10-26T12:00:00+08:00', '2026-10-26T18:00:00+08:00')]


class FlowTests(Scenario):
    def setUp(self):
        super().setUp()
        self.flow = Flow(self.store)

    def setup_event(self, start=START, end=END, zones=ZONES):
        self.competition.create_event('evt', 'Showcase', start, end, 'Asia/Shanghai')
        self.competition.add_group('evt', 'dance', 'Dance', {'form': 60, 'rhythm': 40}, 2, 3)
        for number in range(2):
            self.competition.register_team('evt', f't{number}', 'dance', f'Team {number}',
                                           'Qingpu', 3)
            self.competition.assign_judge('evt', f'j{number}', f'Judge {number}', 'dance')
        for code, kind, opens, closes in zones:
            self.services.add_zone('evt', code, code, kind, opens, closes)
        self.competition.change_state('evt', 'open', self.revision())
        self.sessions.open('evt', 'dance', self.revision())

    def test_transitions_and_repeats(self):
        self.setup_event()
        self.services.record_encounter('evt', 'e1', 'tea', TEN, 'v1', 2)
        self.services.record_encounter('evt', 'e2', 'tour', HALFNOON, 'v1', 1)
        self.services.record_encounter('evt', 'e3', 'tea', TEN, 'v2', 1)
        self.services.record_encounter('evt', 'e4', 'tea', ELEVEN, 'v2', 1)
        self.services.record_encounter('evt', 'e5', 'tour', ONE, 'v3', 1)
        result = self.flow.zone_flow('evt')
        self.assertEqual(result['transitions'], [
            {'from_zone': 'tea', 'to_zone': 'tour', 'tokens': 1, 'pairs': 1,
             'ambiguous_pairs': 0}])
        self.assertEqual(result['repeat'], {'tokens': 3, 'single_contact_tokens': 1,
                                            'repeat_tokens': 2, 'same_zone_repeat_tokens': 1,
                                            'multi_zone_tokens': 1, 'cross_day_tokens': 0})
        tea, tour = result['zones']
        self.assertEqual((tea['encounters'], tea['visits'], tea['unique_visitors'],
                          tea['repeat_tokens']), (3, 4, 2, 1))
        self.assertEqual((tour['encounters'], tour['visits'], tour['unique_visitors'],
                          tour['repeat_tokens']), (2, 2, 2, 0))
        self.assertEqual(result['totals'], {'encounters': 5, 'visits': 6, 'unique_visitors': 3})
        self.assertEqual(result['reconciliation']['basis'], 'live')
        self.assertTrue(result['reconciliation']['compared'])
        self.assertTrue(result['reconciliation']['matches'])
        self.assertIsNone(result['as_of'])
        self.assertEqual(len(result['caveats']), 7)

    def test_window_in_local_time(self):
        self.setup_event()
        self.services.record_encounter('evt', 'e1', 'tea', NINE30, 'v1', 1)
        self.services.record_encounter('evt', 'e2', 'tour', NOON, 'v1', 1)
        self.services.record_encounter('evt', 'e3', 'tour', FIVE30, 'v2', 1)
        windowed = self.flow.zone_flow('evt', start='2026-10-26T12:00',
                                       end='2026-10-26T17:30')
        self.assertEqual(windowed['totals']['encounters'], 1)
        self.assertEqual(windowed['window']['start_local'], '2026-10-26T12:00:00+08:00')
        self.assertFalse(windowed['reconciliation']['compared'])
        self.assertIsNone(windowed['reconciliation']['matches'])
        open_ended = self.flow.zone_flow('evt', start='2026-10-26T12:00')
        self.assertEqual(open_ended['totals']['encounters'], 2)
        by_date = self.flow.zone_flow('evt', start='2026-10-26', end='2026-10-27')
        self.assertEqual(by_date['totals']['encounters'], 3)
        aware = self.flow.zone_flow('evt', start='2026-10-26T04:00:00+00:00',
                                    end='2026-10-26T10:00:00+00:00')
        self.assertEqual(aware['totals']['encounters'], 2)

    def test_boundary_cross_zone_same_instant(self):
        self.setup_event()
        self.services.record_encounter('evt', 'e1', 'tea', NOON, 'v1', 1)
        self.services.record_encounter('evt', 'e2', 'tour', NOON, 'v1', 1)
        self.services.record_encounter('evt', 'e3', 'tour', NOON, 'v2', 1)
        self.services.record_encounter('evt', 'e4', 'tea', NOON, 'v2', 1)
        result = self.flow.zone_flow('evt')
        self.assertEqual(result['transitions'], [
            {'from_zone': 'tea', 'to_zone': 'tour', 'tokens': 1, 'pairs': 1,
             'ambiguous_pairs': 1},
            {'from_zone': 'tour', 'to_zone': 'tea', 'tokens': 1, 'pairs': 1,
             'ambiguous_pairs': 1}])
        self.assertEqual(result['repeat']['multi_zone_tokens'], 2)

    def test_batch_replay_does_not_create_paths(self):
        self.setup_event()
        rows = [{'id': 'e1', 'zone_code': 'tea', 'occurred_at': TEN,
                 'visitor_token': 'v1', 'count': 1},
                {'id': 'e2', 'zone_code': 'tour', 'occurred_at': HALFNOON,
                 'visitor_token': 'v1', 'count': 1},
                {'id': 'e3', 'zone_code': 'tea', 'occurred_at': ELEVEN,
                 'visitor_token': 'v2', 'count': 2}]
        first = self.services.ingest_batch('evt', 'batch1', rows)
        self.assertFalse(first['replayed'])
        before = self.flow.zone_flow('evt')
        replay = self.services.ingest_batch('evt', 'batch1', rows)
        self.assertTrue(replay['replayed'])
        other_key = self.services.ingest_batch('evt', 'batch2', rows)
        self.assertTrue(all(row['duplicate'] for row in other_key['accepted']))
        self.services.record_encounter('evt', 'e1', 'tea', TEN, 'v1', 1)
        after = self.flow.zone_flow('evt')
        self.assertEqual(after['totals'], before['totals'])
        self.assertEqual(after['transitions'], before['transitions'])
        self.assertEqual(after['repeat'], before['repeat'])

    def test_versioned_review_and_reconciliation(self):
        self.setup_event()
        self.services.record_encounter('evt', 'e1', 'tea', TEN, 'v1', 2)
        self.services.record_encounter('evt', 'e2', 'tour', HALFNOON, 'v1', 1)
        self.score_all()
        self.close()
        first = self.publish()
        frozen = self.flow.zone_flow('evt', version=1)
        self.assertEqual(frozen['as_of']['version'], 1)
        self.assertEqual(frozen['as_of']['newer_records_excluded'], 0)
        self.assertEqual(frozen['reconciliation']['basis'], 'snapshot')
        self.assertTrue(frozen['reconciliation']['matches'])
        self.assertEqual(frozen['totals'], {'encounters': 2, 'visits': 3, 'unique_visitors': 1})
        self.services.record_encounter('evt', 'e3', 'tour', ONE, 'v2', 1)
        self.assertEqual(self.flow.zone_flow('evt')['totals']['encounters'], 3)
        frozen = self.flow.zone_flow('evt', version=1)
        self.assertEqual(frozen['totals']['encounters'], 2)
        self.assertEqual(frozen['as_of']['newer_records_excluded'], 1)
        self.assertTrue(frozen['reconciliation']['matches'])
        second = self.publish()
        self.assertTrue(self.flow.zone_flow('evt', version=2)['reconciliation']['matches'])
        self.assertEqual(self.publication.get('evt', 1), first)
        self.assertEqual(self.publication.get('evt', 1)['services']['total_visits'],
                         frozen['totals']['visits'])
        self.assertEqual(second['services']['total_visits'], 4)

    def test_reconciliation_detects_tampering(self):
        self.setup_event()
        self.services.record_encounter('evt', 'e1', 'tea', TEN, 'v1', 2)
        self.score_all()
        self.close()
        self.publish()
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM snapshots WHERE event_id='evt' AND version=1")
            payload = json.loads(row.fetchone()['payload'])
            payload['services']['zones'][0]['visits'] += 1
            db.execute("UPDATE snapshots SET payload=? WHERE event_id='evt' AND version=1",
                       (json.dumps(payload, ensure_ascii=False, sort_keys=True),))
        result = self.flow.zone_flow('evt', version=1)
        self.assertFalse(result['reconciliation']['matches'])
        self.assertTrue(any(item['scope'] == 'tea' and item['field'] == 'visits'
                            for item in result['reconciliation']['differences']))

    def test_cross_day_token_flag(self):
        self.setup_event(start='2026-10-26T08:00:00+08:00',
                         end='2026-10-27T19:00:00+08:00',
                         zones=[('tea', 'tea', '2026-10-26T09:00:00+08:00',
                                 '2026-10-27T18:00:00+08:00'),
                                ('tour', 'tourism', '2026-10-26T09:00:00+08:00',
                                 '2026-10-27T18:00:00+08:00')])
        self.services.record_encounter('evt', 'e1', 'tea', TEN, 'v1', 1)
        self.services.record_encounter('evt', 'e2', 'tea', '2026-10-27T10:00:00+08:00', 'v1', 1)
        self.services.record_encounter('evt', 'e3', 'tea', ELEVEN, 'v2', 1)
        result = self.flow.zone_flow('evt')
        self.assertEqual(result['repeat']['cross_day_tokens'], 1)
        self.assertEqual(result['repeat']['same_zone_repeat_tokens'], 1)
        self.assertEqual(result['repeat']['repeat_tokens'], 1)
        self.assertTrue(any('several local dates' in caveat for caveat in result['caveats']))

    def test_window_and_version_validation(self):
        self.setup_event()
        with self.assertRaises(DomainError) as caught:
            self.flow.zone_flow('evt', start='2026-10-26T12:00', end='2026-10-26T11:00')
        self.assertEqual(caught.exception.code, 'invalid_interval')
        with self.assertRaises(DomainError) as caught:
            self.flow.zone_flow('evt', start='not-a-time')
        self.assertEqual(caught.exception.code, 'invalid_timestamp')
        with self.assertRaises(DomainError) as caught:
            self.flow.zone_flow('evt', version=0)
        self.assertEqual(caught.exception.code, 'invalid_version')
        with self.assertRaises(DomainError) as caught:
            self.flow.zone_flow('evt', version=99)
        self.assertEqual(caught.exception.code, 'snapshot_not_found')
        with self.assertRaises(DomainError) as caught:
            self.flow.zone_flow('missing')
        self.assertEqual(caught.exception.code, 'not_found')
