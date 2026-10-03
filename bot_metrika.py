"""Server-side first-start analytics. No Telegram identity is sent to Metrika."""
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request


def parse_yandex_start(payload):
    """Return (click ID, optional referral code) for valid Telegram payloads."""
    if not isinstance(payload, str) or len(payload) > 64:
        return None
    match = re.fullmatch(r'yd_([0-9]{1,40})(?:_ref_([A-Za-z0-9_-]{1,48}))?', payload)
    return (match.group(1), match.group(2) or '') if match else None


class BotMetrika:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.counter = os.environ.get('BOT_METRIKA_ID', '').strip()
        self.token = os.environ.get('BOT_METRIKA_MP_TOKEN', '').strip()
        self.username = os.environ.get('BOT_USERNAME', 'belowmarketdubaibot').lstrip('@')

    @property
    def enabled(self):
        return bool(self.counter and self.token)

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS bot_metrika_users (
                    chat_id INTEGER PRIMARY KEY,
                    cid TEXT NOT NULL,
                    first_start_at INTEGER,
                    payload TEXT,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'pageview',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    error TEXT
                );
            ''')
            conn.execute("""CREATE TABLE IF NOT EXISTS bot_metrika_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cid TEXT NOT NULL, first_start_at INTEGER NOT NULL,
                payload TEXT, status TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'pageview',
                attempts INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL, error TEXT
            )""")
            conn.execute("UPDATE bot_metrika_tests SET status='uncertain', error='worker_interrupted' WHERE status='sending'")
            # Exclude existing subscribers, including those predating deployment.
            conn.execute('''INSERT OR IGNORE INTO bot_metrika_users
                (chat_id, cid, status, updated_at)
                SELECT chat_id, '', 'existing', ? FROM subscribers''', (int(time.time()),))
            # A crash after an HTTP send has an ambiguous outcome: do not double-count.
            conn.execute("UPDATE bot_metrika_users SET status='uncertain', error='worker_interrupted' WHERE status='sending'")

    def record_start(self, chat_id, payload=''):
        if not self.enabled or chat_id <= 0:
            return
        now = int(time.time())
        payload = payload if re.fullmatch(r'[A-Za-z0-9_-]{0,64}', payload) else ''
        with self.connect() as conn:
            conn.execute('''INSERT OR IGNORE INTO bot_metrika_users
                (chat_id, cid, first_start_at, payload, status, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?)''',
                (chat_id, str(secrets.randbits(63) or 1), now, payload, now))

    def record_test_start(self, chat_id, payload=''):
        if not self.enabled or chat_id <= 0:
            return
        now = int(time.time())
        payload = payload if re.fullmatch(r'[A-Za-z0-9_-]{0,64}', payload) else ''
        with self.connect() as conn:
            # Each intentional diagnostic start is a separate test, even for old users.
            conn.execute("""INSERT INTO bot_metrika_tests
                (cid, first_start_at, payload, status, updated_at)
                VALUES (?, ?, ?, 'pending', ?)""",
                (str(secrets.randbits(63) or 1), now, payload, now))

    def parameters(self, row, diagnostic=False):
        params = dict(tid=self.counter, cid=row['cid'], ms=self.token,
                      et=str(row['first_start_at']))
        url = 'https://t.me/' + self.username
        query = {}
        if row['payload']:
            query['start'] = row['payload']
        attribution = parse_yandex_start(row['payload'] or '')
        if attribution:
            query['yclid'] = attribution[0]
        if query:
            url += '?' + urllib.parse.urlencode(query)
        params['dl'] = url
        if row['phase'] == 'pageview':
            params.update(t='pageview', dt='Telegram bot: first start')
        else:
            params.update(t='event', ea='bot_start_test' if diagnostic else 'bot_start')
        return params

    def send(self, params):
        request = urllib.request.Request('https://mc.yandex.ru/collect',
            data=urllib.parse.urlencode(params).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read(2048).decode('utf-8', 'replace').strip()
            if response.status != 200 or body != '<!-- OK -->':
                raise ValueError('unexpected_response')

    def process_one(self, diagnostic=False):
        if not self.enabled:
            return False
        now = int(time.time())
        table, key = ('bot_metrika_tests', 'id') if diagnostic else ('bot_metrika_users', 'chat_id')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(f"SELECT * FROM {table} WHERE status='pending' ORDER BY first_start_at LIMIT 1").fetchone()
            if not row:
                return False
            if now - row['first_start_at'] > 11 * 3600:
                conn.execute(f"UPDATE {table} SET status='expired', error='outside_delivery_window' WHERE {key}=?", (row[key],))
                return True
            conn.execute(f"UPDATE {table} SET status='sending', attempts=attempts+1, updated_at=? WHERE {key}=?", (now, row[key]))
        try:
            self.send(self.parameters(row, diagnostic=diagnostic))
        except urllib.error.HTTPError as exc:
            status, error = 'failed', 'http_' + str(exc.code)
        except Exception as exc:
            # Never log URLs or exception text: they may include the secret token.
            status, error = 'uncertain', type(exc).__name__
        else:
            status, error = ('pending' if row['phase'] == 'pageview' else 'accepted'), None
        with self.connect() as conn:
            conn.execute(f'''UPDATE {table} SET status=?, phase=?, updated_at=?, error=?
                WHERE {key}=?''', (status, 'event' if status == 'pending' else row['phase'],
                                    int(time.time()), error, row[key]))
        return True

    def run(self, stop_event):
        while not stop_event.is_set():
            try:
                active = self.process_one()
                active = self.process_one(diagnostic=True) or active
            except Exception:
                active = False
            stop_event.wait(0.2 if active else 5)
