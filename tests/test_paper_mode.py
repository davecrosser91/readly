"""Paper mode: Bücher haben einen Modus (language|paper), der Prompt-Wahl
und Lookup-Stil steuert — gleiche Mechanik, andere Domäne."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


class PaperModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DATA_DIR = self.tmp.name
        server.DB_PATH = os.path.join(self.tmp.name, "lector.db")
        server.EVENTS_PATH = os.path.join(self.tmp.name, "events.jsonl")
        with server.db() as conn:
            conn.executescript(server.SCHEMA)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_stores_mode(self):
        text = ("Kapitel 1\n" + "Ein Satz. " * 60).encode()
        book_id = server.import_book("x.txt", text, title="T", language="en",
                                     mode="paper")
        with server.db() as conn:
            row = conn.execute("SELECT mode FROM books WHERE id=?", (book_id,)).fetchone()
        self.assertEqual(row["mode"], "paper")

    def test_default_mode_is_language(self):
        book_id = server.import_book("y.txt", ("Text. " * 100).encode(), title="U")
        with server.db() as conn:
            row = conn.execute("SELECT mode FROM books WHERE id=?", (book_id,)).fetchone()
        self.assertEqual(row["mode"], "language")

    def test_system_prompt_follows_mode(self):
        self.assertIs(server.system_for_mode("paper"), server.PAPER_SYSTEM)
        self.assertIs(server.system_for_mode("language"), server.TEACHER_SYSTEM)
        self.assertIs(server.system_for_mode(None), server.TEACHER_SYSTEM)

    def test_paper_system_is_academic_not_language_teaching(self):
        self.assertIn("Paper", server.PAPER_SYSTEM)
        self.assertNotIn("Spanisch", server.PAPER_SYSTEM)


if __name__ == "__main__":
    unittest.main()
