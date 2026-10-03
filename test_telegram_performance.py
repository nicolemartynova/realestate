import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class TelegramPerformanceTests(unittest.TestCase):
    def test_ipv4_transport_preserves_tls_hostname(self):
        connection = app.TelegramHTTPSConnection('api.telegram.org', timeout=30)
        self.assertEqual(connection.host, 'api.telegram.org')
        with patch.object(app.socket, 'getaddrinfo', return_value=[(2,1,6,'',('149.154.166.110',443))]) as dns, patch.object(app.socket, 'create_connection') as connect:
            connection._create_connection(('api.telegram.org',443),30,None)
            dns.assert_called_once_with('api.telegram.org',443,app.socket.AF_INET,app.socket.SOCK_STREAM)
            connect.assert_called_once_with(('149.154.166.110',443),30,None)

    def test_admin_notifications_persist_without_network_in_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(app,'DATA_DIR',root), patch.object(app,'UPLOAD_DIR',root/'uploads'), patch.object(app,'BROADCAST_UPLOAD_DIR',root/'broadcasts'), patch.object(app,'DB_PATH',root/'db.sqlite3'), patch.object(app,'ADMIN_CHAT_ID','1,2'), patch.object(app,'send_message') as send:
                app.init_db()
                app.notify_admin('New lead')
                send.assert_not_called()
                # Startup preserves the outbox for delivery after a restart.
                app.init_db()
                send.return_value = {'ok':True}
                self.assertTrue(app.deliver_admin_notification())
                send.assert_called_once_with('1','New lead')
                send.return_value = {'ok':False,'error_code':429,'parameters':{'retry_after':10}}
                app.deliver_admin_notification()
                with app.db() as conn:
                    row=conn.execute('select * from admin_notifications').fetchone()
                    self.assertEqual(row['status'],'pending')
                    self.assertEqual(row['attempts'],1)
                    conn.execute('update admin_notifications set next_attempt=0')
                send.return_value=None
                app.deliver_admin_notification()
                with app.db() as conn:
                    self.assertEqual(conn.execute('select status from admin_notifications').fetchone()[0],'failed')
                self.assertFalse(app.deliver_admin_notification())
