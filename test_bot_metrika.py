import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from bot_metrika import BotMetrika, parse_yandex_start


class TrackingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = self.tmp.name + '/test.sqlite'
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE subscribers (chat_id INTEGER PRIMARY KEY)')
            conn.execute('INSERT INTO subscribers VALUES (1)')
        self.tracker = BotMetrika(path)
        self.tracker.counter = '113326213'
        self.tracker.token = 'test-only'
        self.tracker.initialize()

    def row(self, chat_id):
        with self.tracker.connect() as conn:
            return conn.execute('SELECT * FROM bot_metrika_users WHERE chat_id=?', (chat_id,)).fetchone()

    def test_existing_and_repeated_start(self):
        self.tracker.record_start(1, 'yd_123')
        self.assertEqual(self.row(1)['status'], 'existing')
        self.tracker.record_start(2, 'yd_123')
        self.tracker.record_start(2, 'yd_999')
        self.assertEqual(self.row(2)['payload'], 'yd_123')
        sent = []
        self.tracker.send = sent.append
        self.assertTrue(self.tracker.process_one())
        self.assertTrue(self.tracker.process_one())
        self.assertFalse(self.tracker.process_one())
        self.assertEqual([p['t'] for p in sent], ['pageview', 'event'])
        self.assertEqual(sent[1]['ea'], 'bot_start')
        self.assertEqual(sent[0]['cid'], sent[1]['cid'])
        self.assertIn('yclid=123', sent[0]['dl'])
        self.assertEqual(self.row(2)['status'], 'accepted')

    def test_pending_survives_restart(self):
        self.tracker.record_start(2, 'ref_existing_code')
        self.tracker.initialize()
        self.assertEqual(self.row(2)['status'], 'pending')
        self.assertNotIn('yclid=', self.tracker.parameters(self.row(2))['dl'])

    def test_combined_attribution(self):
        payload = 'yd_123456_ref_source_d2d9'
        self.assertEqual(parse_yandex_start(payload), ('123456', 'source_d2d9'))
        self.tracker.record_start(2, payload)
        self.assertIn('yclid=123456', self.tracker.parameters(self.row(2))['dl'])
        for invalid in ('yd_{yclid}', 'yd_abc', 'yd_12_ref_', 'yd_' + '1' * 41,
                        'yd_123_ref_' + 'a' * 55, 'ref_source_d2d9'):
            self.assertIsNone(parse_yandex_start(invalid))

    def test_handler_notification_and_referral_statistics(self):
        import app
        from pathlib import Path
        from unittest.mock import Mock
        test_path = Path(self.tmp.name) / 'app.sqlite'
        tracker = Mock()
        messages = []
        def immediate_thread(*, target, args, daemon):
            thread = Mock()
            thread.start.side_effect = lambda: target(*args)
            return thread
        with patch.object(app, 'DB_PATH', test_path), patch.object(app, 'BOT_METRIKA', tracker), \
             patch.object(app, 'notify_admin', side_effect=messages.append), \
             patch.object(app, 'send_welcome_message'), \
             patch.object(app.threading, 'Thread', side_effect=immediate_thread):
            app.init_db()
            app.create_referral_link('Директ Поиск', 'Директ Поиск', 'source_d2d9')
            user = {'id': 123, 'first_name': 'Test', 'username': 'test_user'}
            app.handle_start(123, user, 'yd_98765_ref_source_d2d9')
            self.assertIn('Кампания: Директ Поиск', messages[-1])
            self.assertIn('Источник: Директ Поиск', messages[-1])
            self.assertIn('ID клика Яндекса: 98765', messages[-1])
            with app.db() as conn:
                row = conn.execute('SELECT referral_code, referral_link_id FROM referral_visits').fetchone()
                self.assertEqual(row['referral_code'], 'source_d2d9')
                self.assertIsNotNone(row['referral_link_id'])
            app.handle_start(123, user, 'ref_source_d2d9')
            self.assertIn('Переход по реферальной ссылке', messages[-1])
            self.assertNotIn('ID клика', messages[-1])
            app.handle_start(123, user, 'yd_54321')
            self.assertIn('Источник: Яндекс Директ', messages[-1])
            self.assertIn('Кампания: Не указана', messages[-1])
            self.assertEqual(sum('Кампания:' in message for message in messages), 3)
            tracker.reset_mock()
            app.handle_text({'chat': {'id': 123}, 'from': user,
                             'text': '/start yd_test_ref_source_d2d9'})
            tracker.record_start.assert_not_called()
            tracker.record_test_start.assert_called_once_with(123, 'yd_test_ref_source_d2d9')
            self.assertIn('ТЕСТ — проверка рекламной ссылки', messages[-1])
            self.assertIn('Кампания: Директ Поиск', messages[-1])
            self.assertIn('Источник: Директ Поиск', messages[-1])
            with app.db() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM referral_visits').fetchone()[0], 3)
                self.assertEqual(conn.execute("SELECT count(*) FROM bot_events WHERE event_type='start_yandex_test'").fetchone()[0], 1)

    def test_repeated_diagnostics_for_existing_and_new_users(self):
        for chat_id in (1, 1, 2):
            self.tracker.record_test_start(chat_id, 'yd_test_ref_source_d2d9')
        self.tracker.record_test_start(-1)
        self.tracker.initialize()
        sent = []
        self.tracker.send = sent.append
        while self.tracker.process_one(diagnostic=True):
            pass
        self.assertEqual(len(sent), 6)
        events = [p for p in sent if p['t'] == 'event']
        self.assertEqual([p['ea'] for p in events], ['bot_start_test'] * 3)
        self.assertEqual(len({p['cid'] for p in events}), 3)
        self.assertEqual(self.row(1)['status'], 'existing')
        self.assertIsNone(self.row(2))
        self.tracker.record_test_start(1)
        with patch.object(self.tracker, 'send', side_effect=TimeoutError()):
            self.tracker.process_one(diagnostic=True)
        self.assertFalse(self.tracker.process_one(diagnostic=True))

    def test_ambiguous_delivery_is_not_resent(self):
        self.tracker.record_start(2)
        with patch.object(self.tracker, 'send', side_effect=TimeoutError('secret must not be logged')):
            self.tracker.process_one()
        self.assertEqual(self.row(2)['status'], 'uncertain')
        self.assertEqual(self.row(2)['error'], 'TimeoutError')
        self.assertFalse(self.tracker.process_one())

    def test_private_chat_and_disabled(self):
        self.tracker.record_start(-3)
        self.assertIsNone(self.row(-3))
        self.tracker.token = ''
        self.tracker.record_start(2)
        self.assertIsNone(self.row(2))

    def test_expired_not_sent_and_interrupted_not_duplicated(self):
        self.tracker.record_start(2)
        with self.tracker.connect() as conn:
            conn.execute('UPDATE bot_metrika_users SET first_start_at=1 WHERE chat_id=2')
        self.tracker.process_one()
        self.assertEqual(self.row(2)['status'], 'expired')
        self.tracker.record_start(3)
        with self.tracker.connect() as conn:
            conn.execute("UPDATE bot_metrika_users SET status='sending' WHERE chat_id=3")
        self.tracker.initialize()
        self.assertEqual(self.row(3)['status'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
