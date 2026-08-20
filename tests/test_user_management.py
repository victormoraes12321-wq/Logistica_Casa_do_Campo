# -*- coding: utf-8 -*-
import unittest
import os
import tempfile

import app


class UserManagementTests(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.orig_db_target = app.DB_TARGET
        self.orig_db_path = app.DB_PATH

        app.DB_PATH = self.db_path
        app.DB_TARGET = app.RuntimeDatabaseTarget(
            backend='sqlite',
            database_url=f'sqlite:///{self.db_path}',
            sqlite_path=self.db_path
        )
        app.init_db()

        with app.conn() as db:
            db.execute("INSERT INTO users(name, username, password_hash, role, active, created_at) VALUES('GOD Test', 'god_test', 'hash1', 'GOD', 1, ?)", (app.now(),))
            db.execute("INSERT INTO users(name, username, password_hash, role, active, created_at) VALUES('Maria Test', 'maria_test', 'hash2', 'Operador', 1, ?)", (app.now(),))
            db.commit()

            self.user1_id = db.execute("SELECT id FROM users WHERE username='god_test'").fetchone()['id']
            self.user2_id = db.execute("SELECT id FROM users WHERE username='maria_test'").fetchone()['id']

    def tearDown(self):
        app.DB_TARGET = self.orig_db_target
        app.DB_PATH = self.orig_db_path
        os.close(self.db_fd)
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_post_profile_update_username_and_password(self):
        with app.conn() as db:
            god_user = dict(db.execute("SELECT * FROM users WHERE id=?", (self.user1_id,)).fetchone())

        class DummyHandler:
            def post_data(self):
                return {
                    'redirect_section': 'profile',
                    'name': 'GOD Admin Renomeado',
                    'username': 'god_novo',
                    'password': 'NovoPassword123!'
                }
            def redirect(self, url):
                self.redirect_url = url

        h = DummyHandler()
        app.App.post_profile(h, god_user)

        with app.conn() as db:
            updated = dict(db.execute("SELECT * FROM users WHERE id=?", (self.user1_id,)).fetchone())
            self.assertEqual(updated['name'], 'GOD Admin Renomeado')
            self.assertEqual(updated['username'], 'god_novo')
            self.assertTrue(app.verify_password('NovoPassword123!', updated['password_hash']))

    def test_post_user_update_another_user(self):
        with app.conn() as db:
            god_user = dict(db.execute("SELECT * FROM users WHERE id=?", (self.user1_id,)).fetchone())

        class DummyHandler:
            def post_data(self):
                return {
                    'redirect_section': 'users',
                    'name': 'Maria Editada',
                    'username': 'maria_login_novo',
                    'role': 'Admin',
                    'active': '1',
                    'password': 'SenhaMaria456!'
                }
            def has_perm(self, u, perm): return True
            def redirect(self, url):
                self.redirect_url = url

        h = DummyHandler()
        app.App.post_user_update(h, god_user, self.user2_id)

        with app.conn() as db:
            updated = dict(db.execute("SELECT * FROM users WHERE id=?", (self.user2_id,)).fetchone())
            self.assertEqual(updated['name'], 'Maria Editada')
            self.assertEqual(updated['username'], 'maria_login_novo')
            self.assertEqual(updated['role'], 'Admin')
            self.assertTrue(app.verify_password('SenhaMaria456!', updated['password_hash']))

    def test_duplicate_username_prevented(self):
        with app.conn() as db:
            god_user = dict(db.execute("SELECT * FROM users WHERE id=?", (self.user1_id,)).fetchone())

        class DummyHandler:
            def post_data(self):
                return {
                    'redirect_section': 'users',
                    'name': 'Maria Tentativa Dup',
                    'username': 'god_test',  # Pertence a user1_id
                    'role': 'Operador',
                    'active': '1',
                    'password': ''
                }
            def has_perm(self, u, perm): return True
            def redirect(self, url): pass

        h = DummyHandler()
        with self.assertRaises(ValueError):
            app.App.post_user_update(h, god_user, self.user2_id)


if __name__ == '__main__':
    unittest.main()
