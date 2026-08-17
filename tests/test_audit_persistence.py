from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from papyri.object_layout import (
    CURRENT_LAYOUT_VERSION, MetaKey, migrate_object, read_capture_audits,
    read_meta, remove_capture_audits, rename_capture_audits,
    store_capture_audit, write_meta,
)


class AuditPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.object_dir = Path(self.temp.name) / "object"
        self.object_dir.mkdir()
        self.meta_path = self.object_dir / "_meta.json"
        write_meta(str(self.meta_path), {
            MetaKey.LAYOUT_VERSION: 1,
            "title": "preserve me",
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_v2_migration_is_idempotent_and_additive(self) -> None:
        self.assertTrue(migrate_object(str(self.object_dir)))
        self.assertFalse(migrate_object(str(self.object_dir)))
        meta = read_meta(str(self.meta_path))
        self.assertEqual(meta[MetaKey.LAYOUT_VERSION], CURRENT_LAYOUT_VERSION)
        self.assertEqual(meta[MetaKey.AUDITS], {})
        self.assertEqual(meta["title"], "preserve me")

    def test_store_rename_remove_preserve_other_metadata(self) -> None:
        store_capture_audit(
            str(self.meta_path), "old", "sharpness",
            {"metric_version": "v2-erf", "sharp_px": 1.2},
        )
        rename_capture_audits(str(self.meta_path), {"old": "new"})
        self.assertIn("new", read_capture_audits(str(self.meta_path)))
        self.assertNotIn("old", read_capture_audits(str(self.meta_path)))
        remove_capture_audits(str(self.meta_path), "new")
        self.assertEqual(read_capture_audits(str(self.meta_path)), {})
        self.assertEqual(read_meta(str(self.meta_path))["title"], "preserve me")


if __name__ == "__main__":
    unittest.main()
