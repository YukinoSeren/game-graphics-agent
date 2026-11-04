"""Presentation pacing and direct recent-log workflows (demo only)."""

from datetime import datetime
import json
from urllib.request import urlopen

from demo.app.contracts import DemoError, Scope
from demo.tests.support import DemoCase, running_http


class PageRefinementTests(DemoCase):
    def entities(self, kind):
        workspace = self.h.workspace()
        return self.h.service.find(Scope('mw2-rtx4060', workspace['workspace_epoch']), kind)

    def test_assistant_reply_is_delayed_and_fast_mode_cannot_publish_early(self):
        self.h.command('demo.mode', {'mode': 'fast'})
        started = datetime.fromisoformat(self.h.clock.now_iso())
        reply = self.h.command('intent.submit', {'message': '我想玩现代战争1，希望流畅清晰'})
        intent = self.entities('GameIntent')[-1]
        self.assertEqual(intent['game_name'], '使命召唤：现代战争 I')
        self.assertEqual((datetime.fromisoformat(intent['reply_visible_at']) - started).total_seconds(), 3)
        self.assertEqual((datetime.fromisoformat(intent['card_not_before']) - started).total_seconds(), 63)
        for _ in range(62):
            self.h.clock.advance(1)
            self.h.service.tasks.tick()
        self.assertEqual(self.entities('GameCard'), [])
        self.h.run(reply.body['task_id'])
        self.assertEqual(len(self.entities('GameCard')), 1)

    def test_one_click_log_read_creates_a_bound_summary_and_reuses_it(self):
        applied = self.h.adopt()
        reply = self.h.command('log.capture_recent', {'log_id': 'last-play', 'expected_applied_id': applied})
        summary = self.h.entity(self.h.ref(reply, 'Summary'))
        self.assertEqual(summary['applied_id'], applied)
        self.assertGreater(summary['metrics']['average_present_rate'], 0)
        self.assertEqual(summary['data_origin'], 'simulated')
        again = self.h.command('log.capture_recent', {'log_id': 'last-play', 'expected_applied_id': applied})
        self.assertEqual(self.h.ref(again, 'Summary'), summary['id'])
        self.assertEqual(len(self.entities('RecentLogCapture')), 1)

    def test_recent_log_rejects_unconfirmed_or_stale_configuration(self):
        with self.assertRaises(DemoError) as missing:
            self.h.command('log.capture_recent', {'log_id': 'last-play', 'expected_applied_id': None})
        self.assertEqual(missing.exception.code, 'configuration_required')
        self.h.adopt()
        with self.assertRaises(DemoError) as stale:
            self.h.command('log.capture_recent', {'log_id': 'last-play', 'expected_applied_id': None})
        self.assertEqual(stale.exception.code, 'pointer_conflict')

    def test_recent_log_list_is_read_only_and_all_three_records_can_be_analyzed(self):
        applied = self.h.adopt()
        before = self.h.workspace()
        query = f"scenario_id=mw2-rtx4060&workspace_epoch={before['workspace_epoch']}"
        with running_http(self.h.service) as base:
            with urlopen(f'{base}/api/demo/v1/recent-play-logs?{query}') as response:
                logs = json.load(response)['logs']
        self.assertEqual(self.h.workspace()['revision'], before['revision'])
        self.assertEqual(len(logs), 3)
        summaries = set()
        for log in logs:
            self.assertGreater(log['duration_s'], 30)
            self.assertEqual(log['origin'], 'simulated')
            reply = self.h.command('log.capture_recent', {'log_id': log['id'], 'expected_applied_id': applied})
            summary = self.h.entity(self.h.ref(reply, 'Summary'))
            self.assertEqual(summary['quality'], 'usable')
            self.assertEqual(summary['applied_id'], applied)
            summaries.add(summary['id'])
        self.assertEqual(len(summaries), 3)
