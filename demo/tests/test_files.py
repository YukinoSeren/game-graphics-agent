"""T03: managed demo files are addressed by ID and verified by hash."""

import hashlib

from demo.app.contracts import Command, DemoError
from demo.app.files import MAX_UPLOAD_BYTES
from demo.tests.support import DemoCase


class FileTests(DemoCase):
    def test_upload_registers_a_file_entity_without_a_disk_path(self):
        data = "demo_only,scenario_id\n".encode("utf-8")
        file_id = self.h.upload(data, filename="frames.csv")
        record = self.h.entity(file_id)
        self.assertEqual(record["kind"], "File")
        self.assertEqual(record["data_origin"], "uploaded_demo_file")
        self.assertEqual(record["content_hash"], hashlib.sha256(data).hexdigest())
        self.assertNotIn("rel_path", record)
        self.assertEqual(self.h.service.files.read(file_id), data)

    def test_staged_file_is_not_downloadable_before_registration(self):
        staged = self.h.service.files.stage(b"hello", "text/plain")
        with self.assertRaises(DemoError) as caught:
            self.h.service.files.read("demo_file_unregistered")
        self.assertEqual(caught.exception.status, 404)
        self.h.service.files.discard(staged)
        self.assertFalse(__import__("pathlib").Path(staged["staged_path"]).exists())

    def test_tampered_file_is_rejected_on_read(self):
        file_id = self.h.upload(b"original", "text/plain", filename="note.txt")
        path = self.h.service.files.path_for(file_id)
        path.write_bytes(b"tampered")
        with self.assertRaises(DemoError) as caught:
            self.h.service.files.read(file_id)
        self.assertEqual(caught.exception.code, "artifact_hash_mismatch")

    def test_path_like_filenames_are_rejected(self):
        for name in ("../escape.csv", "..\\escape.csv", "a/b.csv", "C:\\tmp\\x.csv"):
            with self.subTest(name=name):
                with self.assertRaises(DemoError) as caught:
                    self.h.upload(b"x", "text/csv", filename=name)
                self.assertEqual(caught.exception.code, "invalid_filename")

    def test_oversized_upload_is_refused(self):
        with self.assertRaises(DemoError) as caught:
            self.h.upload(b"x" * (MAX_UPLOAD_BYTES + 1), "text/csv")
        self.assertEqual(caught.exception.status, 413)

    def test_hash_and_size_must_match_the_uploaded_bytes(self):
        workspace = self.h.workspace()
        command = Command(
            request_id="bad-hash",
            scenario_id="mw2-rtx4060",
            workspace_epoch=workspace["workspace_epoch"],
            expected_revision=workspace["revision"],
            action="file.upload",
            payload={
                "media_type": "text/csv",
                "filename": "x.csv",
                "size": 3,
                "content_hash": "0" * 64,
            },
        )
        with self.assertRaises(DemoError) as caught:
            self.h.service.upload(
                command, data=b"abc", media_type="text/csv", filename="x.csv"
            )
        self.assertEqual(caught.exception.code, "hash_mismatch")

    def test_unsupported_media_type_is_refused(self):
        with self.assertRaises(DemoError) as caught:
            self.h.upload(b"MZ...", "application/x-msdownload", filename="tool.exe")
        self.assertEqual(caught.exception.code, "unsupported_media_type")

    def test_uploaded_file_is_scoped_to_its_case(self):
        file_id = self.h.upload(b"case a", "text/plain", filename="a.txt")
        with self.assertRaises(DemoError) as caught:
            self.h.entity(file_id, scenario="fh6-rtx5090")
        self.assertEqual(caught.exception.status, 404)
