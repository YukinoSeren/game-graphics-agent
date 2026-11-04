"""Offline revised presentation flow; isolated temporary state only."""

from demo.app.contracts import Scope
from demo.tests.support import DemoCase


class RevisedFlowTests(DemoCase):
    def entities(self, kind):
        workspace = self.h.workspace()
        return self.h.service.find(Scope('mw2-rtx4060', workspace['workspace_epoch']), kind)

    def test_two_messages_publish_complete_plan_with_card_after_deadline(self):
        first = self.h.command('intent.submit', {'message': '我想玩使命召唤现代战争1'})
        parent = self.h.ref(first, 'GameIntent')
        reply = self.h.command('intent.submit', {
            'message': '我想要在保证流畅的前提下 尽可能保证画面的丰富感',
            'reply_to': parent,
        })
        self.assertIn('画面丰富度', self.entities('GameIntent')[-1]['preferences'])
        for _ in range(62):
            self.h.clock.advance(1)
            self.h.service.tasks.tick()
        self.assertEqual(self.entities('GameCard'), [])
        self.assertEqual(self.entities('Recommendation'), [])
        task = self.h.run(reply.body['task_id'])
        self.assertEqual(task['status'], 'completed')
        card = self.entities('GameCard')[0]
        plan = self.h.entity(card['recommendation_id'])
        knowledge = self.h.entity(card['knowledge_id'])
        self.assertEqual(task['result']['recommendation_id'], plan['id'])
        self.assertEqual(plan['knowledge_id'], knowledge['id'])
        self.assertEqual(len(knowledge['source_ids']), 15)
        self.assertEqual(plan['readiness'], 'ready')
        self.assertEqual(len(plan['settings']), 14)
        source_ids = set(knowledge['source_ids'])
        for item in plan['settings']:
            self.assertIn(item['value'], item['allowed_values'])
            self.assertFalse(item['unknown'])
            self.assertGreaterEqual(len(item['evidence_ids']), 2)
            views = item['evidence_decision']['source_views']
            self.assertGreaterEqual(len(views), 2)
            self.assertTrue(all(view['source_id'] in source_ids for view in views))

    def test_new_source_pack_has_full_distinct_local_content(self):
        pack = self.h.service.catalog.get('mw2-rtx4060')
        sources = pack['sources']
        self.assertEqual(len(sources), 15)
        self.assertEqual(len({s['source_key'] for s in sources}), 15)
        self.assertEqual(len({s['channel'] for s in sources}), 6)
        texts = []
        for source in sources:
            body = ''.join(b['text'] for b in source['display_blocks'])
            self.assertGreaterEqual(len(body), 300, source['title'])
            self.assertIsNone(source.get('external_url'))
            self.assertEqual(source['data_origin'], 'simulated')
            texts.append(body)
        self.assertEqual(len(set(texts)), 15)

    def test_edited_hardware_is_not_considered_fixed_profile(self):
        self.h.command('device.check')
        current = self.h.workspace()['context_id']
        changed = self.h.command('context.save', {
            'expected_context_id': current,
            'fields': {'cpu': 'Different CPU'},
        })
        context = self.h.entity(self.h.ref(changed, 'ContextSnapshot'))
        self.assertNotEqual(context['applicability'], 'preset')

    def test_invalid_plan_cannot_publish_a_half_ready_card(self):
        reply = self.h.command('intent.submit', {'message': '我想玩现代战争1，希望流畅并保留画面丰富感'})
        pack = self.h.service.catalog.get('mw2-rtx4060')
        pack['settings'][0]['evidence_source_ids'] = []
        task = self.h.run(reply.body['task_id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(self.entities('GameCard'), [])
        self.assertEqual(self.entities('Recommendation'), [])

    def test_research_preserves_declared_hardware_before_first_scan(self):
        self.h.command('context.save', {
            'expected_context_id': self.h.workspace()['context_id'],
            'fields': {'cpu': 'Custom CPU'},
        })
        self.h.command('intent.submit', {'message': '我想玩现代战争1，希望流畅并保留画面丰富感'})
        context = self.h.entity(self.h.workspace()['context_id'])
        self.assertEqual(context['cpu'], 'Custom CPU')
        self.assertNotEqual(context['applicability'], 'preset')

    def test_same_request_after_background_edit_creates_a_new_plan(self):
        payload = {'message': '我想玩现代战争1，希望流畅并保留画面丰富感'}
        first = self.h.command('intent.submit', payload)
        self.h.run(first.body['task_id'])
        self.h.command('context.save', {
            'expected_context_id': self.h.workspace()['context_id'],
            'fields': {'cpu': 'Custom CPU'},
        })
        second = self.h.command('intent.submit', payload)
        self.assertNotEqual(second.body['task_id'], first.body['task_id'])
        self.assertEqual(self.h.entity(self.h.workspace()['context_id'])['cpu'], 'Custom CPU')
