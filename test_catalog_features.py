import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class CatalogFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(app, 'DATA_DIR', root), patch.object(app, 'UPLOAD_DIR', root / 'uploads'),
                        patch.object(app, 'BROADCAST_UPLOAD_DIR', root / 'broadcasts'),
                        patch.object(app, 'DB_PATH', root / 'test.sqlite3'),
                        patch.object(app, 'notify_admin'), patch.object(app, 'telegram_api', return_value={'ok': True}),
                        patch.object(app, 'BOT_METRIKA'), patch.object(app, 'PUBLIC_BASE_URL', 'https://example.com')]
        for p in self.patches:
            p.start()
        app.init_db()
        self.user = {'id': 101, 'first_name': 'Test'}
        app.upsert_subscriber(self.user, 101)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def form(self, **overrides):
        values = dict(listing_type='developer', category='Apartment', city='Dubai', district='Marina',
                      developer_name='Example Developer', project_name='Example Tower', studio_price_aed='734500',
                      developer_comment='Комментарий', developer_comment_en='English comment', payment_plan='20/80', handover='Q4 2028')
        values.update(overrides)
        return {k: [str(v)] for k, v in values.items()}

    def create(self, **overrides):
        return app.save_project(self.form(**overrides))

    def prefs(self, **values):
        with app.db() as conn:
            conn.execute('update subscribers set ' + ','.join(k+'=?' for k in values) + ' where chat_id=101', list(values.values()))

    def test_prices_and_optional_fields_roundtrip(self):
        pid = self.create(studio_area='850 sqft', below_market_pct='12.5', market_price='900000')
        project = app.get_project(pid)
        self.assertEqual(project['price'], 734500)
        self.assertEqual(project['studio_max_price_aed'], 834500)
        self.assertEqual(project['studio_max_estimated'], 1)
        self.assertEqual(project['studio_area'], '850 sqft')
        self.assertEqual(project['below_market_pct'], 12.5)
        self.assertIn('200 000 USD', app.project_caption(project, 'en', 'USD'))
        self.assertIn('English comment', app.project_caption(project, 'en'))
        self.assertNotRegex(app.project_caption(project, 'en'), '[А-Яа-яЁё]')
        self.assertEqual(app.format_price(project['price'], 'AED'), '734 500 AED')
        app.save_project(self.form(studio_max_price_aed='810000', studio_max_estimated='0'), pid)
        project = app.get_project(pid)
        self.assertEqual(project['studio_max_price_aed'], 810000)
        self.assertEqual(project['studio_max_estimated'], 0)
        self.assertIn('name="studio_max_estimated" value="0"', app.project_form(project))

    def test_validation_does_not_save_invalid_values(self):
        for overrides in [dict(studio_max_price_aed='10'), dict(studio_price_aed='nan'), dict(below_market_pct='101'),
                          dict(developer_comment_en=''), dict(payment_plan='Рассрочка'), dict(studio_price_aed='', studio_area='90 m2')]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.create(**overrides)
        with app.db() as conn:
            self.assertEqual(conn.execute('select count(*) from projects').fetchone()[0], 0)

    def test_migration_is_once_only(self):
        pid = self.create()
        with app.db() as conn:
            for unit, _ in app.UNIT_TYPES:
                conn.execute(f'alter table projects drop column {unit}_price_aed')
            conn.execute('update projects set studio_price_usd=200000, price=200000 where id=?', (pid,))
        app.init_db()
        self.assertEqual(app.get_project(pid)['price'], 734500)
        app.init_db()
        self.assertEqual(app.get_project(pid)['price'], 734500)
        self.assertEqual(app.get_project(pid)['studio_price_usd'], 200000)

    def test_estimates_excluded_from_statistics(self):
        for i in range(5):
            self.create(project_name=f'Estimate {i}', studio_max_price_aed='900000')
        self.assertEqual(app.max_price_suggestion('studio'), (100000, 0))
        for i in range(5):
            self.create(project_name=f'Confirmed {i}', studio_max_price_aed='934500', studio_max_estimated='0')
        self.assertEqual(app.max_price_suggestion('studio'), (200000, 5))

    def test_catalog_filter_and_sort_including_developer_units(self):
        low = self.create(project_name='Cheap', studio_price_aed='500000', one_bed_price_aed='1000000')
        high = self.create(project_name='Expensive', studio_price_aed='900000')
        page = app.app_projects_page({'developer':['1'], 'sort':['price_asc']}, base_path='/en', lang='en')
        self.assertLess(page.index('Cheap by'), page.index('Expensive by'))
        page = app.app_projects_page({'sort':['price_desc']}, base_path='/en', lang='en')
        self.assertLess(page.index('Expensive by'), page.index('Cheap by'))
        page = app.app_projects_page({'rooms':['1BR'], 'max_price':['250000'], 'currency':['USD']})
        self.assertNotIn('Cheap от', page)
        page = app.app_projects_page({'rooms':['1BR'], 'max_price':['300000'], 'currency':['USD']})
        self.assertIn('Cheap от', page)
        self.assertNotIn('Expensive от', page)
        app.log_event(101, self.user, 'click_interest', project_id=high)
        page = app.app_projects_page({'sort':['popular']})
        self.assertLess(page.index('Expensive от'), page.index('Cheap от'))
        page = app.app_projects_page({'q':['Cheap'], 'sort':['relevance']})
        self.assertIn('Cheap от', page)
        self.assertNotIn('Expensive от', page)

    def test_pages_render_both_languages(self):
        pid = self.create()
        for lang in ('ru', 'en'):
            page = app.app_project_page(pid, base_path='/'+lang, lang=lang)
            self.assertIn('display-currency', page)
            self.assertNotIn('🏗', page)
            self.assertIn('734 500 AED', page)
            self.assertIn('English comment' if lang == 'en' else 'Комментарий', page)
        self.assertNotIn('studio_price_usd', app.project_form(app.get_project(pid)))
        self.assertIn('studio_price_aed', app.project_form(app.get_project(pid)))
        app.dashboard()
        app.build_active_projects_xlsx()

    def test_language_first_and_deeplink_resume(self):
        pid = self.create()
        app.handle_start(101, self.user, f'lot_{pid}')
        calls = app.telegram_api.call_args_list
        self.assertEqual(calls[-1].args[1]['text'], 'Выберите язык / Choose your language')
        app.telegram_api.reset_mock()
        with patch.object(app, 'send_project_card', return_value={'ok':True}) as card:
            app.handle_callback_inner({'id':'q1', 'data':'lang:en', 'from':self.user, 'message':{'chat':{'id':101}}})
            caption, keyboard = card.call_args.args[3:]
            self.assertIn('English comment', caption)
            self.assertIn('Request details', keyboard)
            self.assertNotRegex(caption + keyboard, '[А-Яа-яЁё]')
        self.assertEqual(app.subscriber_preferences(101), ('en', 'AED'))
        app.BOT_METRIKA.record_start.assert_called_once_with(101, f'lot_{pid}')
        self.assertIn('lang=en', app.welcome_keyboard(101))

    def test_bot_filters_and_currency(self):
        low = self.create(project_name='Cheap', studio_price_aed='500000')
        high = self.create(project_name='Expensive', studio_price_aed='900000')
        self.prefs(language='en', filter_developer=1, filter_rooms='Studio', filter_sort='price_asc')
        self.assertEqual(app.next_project_for(101)['id'], high)
        app.mark_seen(101, high)
        self.assertEqual(app.filtered_projects_counts(101), (2, 1))
        self.assertEqual(app.next_project_for(101)['id'], low)
        with patch.object(app, 'send_project_card', return_value={'ok':True}) as card:
            app.handle_callback_inner({'id':'q2', 'data':f'currency:USD:{low}', 'from':self.user, 'message':{'chat':{'id':101}}})
            self.assertIn('USD', card.call_args.args[3])
        self.assertEqual(app.subscriber_preferences(101), ('en','USD'))
        app.send_welcome_message(101)
        self.assertIn('Alexander Vinogradov', app.telegram_api.call_args.args[1]['text'])
        app.send_catalog_followup(101, 'Test')
        self.assertNotRegex(app.telegram_api.call_args.args[1]['text'], '[А-Яа-яЁё]')

    def test_bot_filter_menu_has_no_sort_and_advances(self):
        self.prefs(language='en')
        app.send_filter_options(101)
        payload = app.telegram_api.call_args.args[1]
        self.assertNotIn('filter_sort:', payload['reply_markup'])
        self.assertIn('filter_continue', payload['reply_markup'])
        with patch.object(app, 'send_filter_rooms_prompt') as prompt:
            for data in ('filter_continue', 'filter_sort:popular'):
                app.handle_callback_inner({'id':'q4', 'data':data, 'from':self.user, 'message':{'chat':{'id':101}}})
            self.assertEqual(prompt.call_count, 2)
        for text in app.BOT_DESCRIPTIONS.values():
            self.assertNotIn('nikadigital', text)
            self.assertLessEqual(len(text), 512)

    def test_broadcast_language_and_pending_subscribers(self):
        self.prefs(language='en')
        app.upsert_subscriber({'id':102, 'first_name':'Pending'}, 102)
        with patch.object(app.time, 'sleep'):
            self.assertEqual(app.send_custom_to_all(1, 'Русский текст', [], 'English broadcast'), 1)
        payload = app.telegram_api.call_args.args[1]
        self.assertEqual(payload['text'], 'English broadcast')
        self.assertNotRegex(payload['reply_markup'], '[А-Яа-яЁё]')
        app.telegram_api.reset_mock()
        with patch.object(app.time, 'sleep'):
            self.assertEqual(app.send_custom_to_all(1, 'Старый текст без перевода', []), 0)
        app.telegram_api.assert_not_called()

    def test_resale_roundtrip_currency_and_manual_discount(self):
        values = dict(listing_type='resale', title='Test apartment', category='Apartment', city='Dubai', district='Marina',
                      building='Marina Tower', rooms='1BR', floor_level='Высокий', availability='Свободно', area='90 м²',
                      price='734500', description='Вид на море', description_en='Sea view', below_market_pct='11')
        pid = app.save_project({key:[value] for key,value in values.items()})
        project = app.get_project(pid)
        self.assertIn('200 000 USD', app.project_caption(project, 'en', 'USD'))
        self.assertIn('90 m²', app.project_caption(project, 'en'))
        self.assertNotRegex(app.project_caption(project, 'en'), '[А-Яа-яЁё]')
        page = app.app_project_page(pid, lang='en')
        self.assertIn('11%', page)
        self.assertIn('Sea view', page)
        self.assertIn('name="lang" value="en"', page)
        self.assertIn('data-currency="USD"', page)

    def test_discount_sort_and_distress_filters(self):
        unknown = self.create(project_name='Unknown')
        calculated = self.create(project_name='Calculated', market_price='1000000', distress='1')
        manual = self.create(project_name='Manual', below_market_pct='40', distress='1')
        zero = self.create(project_name='Zero', below_market_pct='0')
        negative = self.create(project_name='Above market', market_price='500000')
        page = app.app_projects_page({'sort':['discount_desc']}, lang='en')
        for first, second in [('Manual by', 'Calculated by'), ('Calculated by', 'Zero by'), ('Zero by', 'Above market by'), ('Above market by','Unknown by')]:
            self.assertLess(page.index(first), page.index(second))
        page = app.app_projects_page({'sort':['discount_desc'], 'distress':['1'], 'developer':['1']}, lang='en')
        self.assertIn('Manual by', page)
        self.assertIn('Calculated by', page)
        self.assertNotIn('Unknown by', page)
        self.assertNotIn('Zero by', page)
        self.assertIn('type="radio" name="sort" value="price_asc"', page)
        self.assertIn('type="radio" name="sort" value="popular"', page)
        self.prefs(language='en', filter_sort='discount_desc', filter_distress=1)
        self.assertEqual(app.next_project_for(101)['id'], manual)
        app.mark_seen(101, manual)
        self.assertEqual(app.next_project_for(101)['id'], calculated)
        self.assertEqual(app.filtered_projects_counts(101), (2,1))
        self.assertTrue(app.subscriber_filters_active(101))
        callback = {'id':'q3', 'data':'filter_reset', 'from':self.user, 'message':{'chat':{'id':101}}}
        app.handle_callback_inner(callback)
        self.assertFalse(app.subscriber_filters_active(101))
        self.assertEqual(app.filtered_projects_counts(101), (5,4))

    def test_resale_translation_fields_and_missing_discount(self):
        page = app.project_form()
        self.assertIn('Комментарий / описание (English)', page)
        self.assertIn('name="description_en"', page)
        pid = self.create(studio_price_aed='', market_price='1000000')
        self.assertIsNone(app.project_discount(app.get_project(pid)))


if __name__ == '__main__':
    unittest.main()
