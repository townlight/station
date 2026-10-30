# SPDX-License-Identifier: AGPL-3.0-only
"""Unit checks for the operational workflow that need no database or media.

Covers the request contract, the approval/publication gate ordering, and the
separation of playout (commit-to-air) from publication. The end-to-end media,
permission, restart and recovery evidence lives in accept_operational.py and
verify_recovery.py, run against a disposable PostgreSQL cluster.
"""
import inspect
import unittest

from pydantic import ValidationError

from townlight_station import operations
from townlight_station.store import playouts, recordings, schedules, station_state


class OperationalContractTests(unittest.TestCase):
    def test_schedule_requires_explicit_start_and_idempotency(self):
        body = {'meeting_id': '11111111-1111-1111-1111-111111111111',
                'source_file': 'program.mp4', 'start_at': '2026-01-01T00:00:00+00:00',
                'idempotency_key': 'k'}
        self.assertEqual(operations.ScheduleRequest(**body).start_at.year, 2026)
        for broken in ({**body, 'start_at': 'not-a-date'}, {**body, 'idempotency_key': ''},
                       {**body, 'source_file': '../escape.mp4'}, {**body, 'live_source_id': '../x'},
                       {k: v for k, v in body.items() if k != 'start_at'}):
            with self.assertRaises(ValidationError):
                operations.ScheduleRequest(**broken)

    def test_caption_sidecar_must_be_srt_name(self):
        base = {'meeting_id': '11111111-1111-1111-1111-111111111111',
                'source_file': 'program.mp4', 'start_at': '2026-01-01T00:00:00+00:00',
                'idempotency_key': 'k'}
        self.assertEqual(operations.ScheduleRequest(**base, captions_file='reviewed.srt').captions_file,
                         'reviewed.srt')
        for bad in ('reviewed.vtt', 'reviewed.txt', '../reviewed.srt', '.srt'):
            with self.assertRaises(ValidationError):
                operations.ScheduleRequest(**base, captions_file=bad)

    def test_playout_mode_is_dry_run_or_commit_only(self):
        base = {'recording_id': '11111111-1111-1111-1111-111111111111', 'idempotency_key': 'k'}
        for mode in ('dry-run', 'commit'):
            self.assertEqual(operations.PlayoutRequest(**base, mode=mode).mode, mode)
        for bad in ('publish', 'air', 'DELETE', ''):
            with self.assertRaises(ValidationError):
                operations.PlayoutRequest(**base, mode=bad)

    def test_approval_note_is_required_and_bounded(self):
        self.assertEqual(operations.ApprovalRequest(note='reviewed').note, 'reviewed')
        for bad in ('', 'no'):
            with self.assertRaises(ValidationError):
                operations.ApprovalRequest(note=bad)

    def test_playout_view_never_reports_publication_from_commit(self):
        row = {'id': 'p', 'status': 'complete', 'recording_id': 'r',
               'details': {'mode': 'commit', 'approved': None, 'published': None,
                           'output_file': 'program.media', 'sha256': 'a' * 64}}
        view = operations._playout_view(row)
        self.assertTrue(view['on_air'] is False)
        self.assertIsNone(view['approved'])
        self.assertIsNone(view['published'])

    def test_store_has_schedule_playout_and_on_air_state_tables(self):
        for table, column in ((schedules, 'start_at'), (playouts, 'recording_id'),
                              (station_state, 'on_air')):
            self.assertIn(column, table.c)
        self.assertIn('schedule_id', inspect.getsource(__import__('townlight_station.worker', fromlist=['x'])))

    def test_publish_endpoint_requires_approval_checked_before_publication(self):
        source = inspect.getsource(operations.install_operations)
        playout_source = source.split('def publish')[1]
        approval = playout_source.index('approved')
        publication = playout_source.index("'published'")
        self.assertLess(approval, publication,
                        'approval must be checked before anything is marked published')
        self.assertIn('Explicit staff approval is required', playout_source)

    def test_public_playback_route_hides_unpublished_playouts(self):
        source = inspect.getsource(operations.install_operations)
        route = source.split('def public_playback')[1]
        self.assertIn("details'].get('published')", route)
        self.assertIn("status'] != 'complete'", route)
        self.assertIn('404', route)


if __name__ == '__main__':
    unittest.main(verbosity=2)
