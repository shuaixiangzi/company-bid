import hashlib
import tempfile
import unittest
from pathlib import Path

from company_bid.import_sources import SourceImportError, import_directory


class ImportSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.catalog = self.root / "catalog"

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o666)
        self.temporary.cleanup()

    def pdf(self, name, content=b"%PDF-1.7\nsynthetic fixture\n"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_deduplicates_bytes_and_keeps_distinct_provenance(self):
        original = self.pdf("certificate.pdf")
        self.pdf("split/营业执照_页28-29.pdf")
        metadata = {"split/": {"source_role": "derived_from_bid", "parent_source_sha256": "a" * 64}}
        result = import_directory(self.source, self.catalog, source_metadata=metadata)
        self.assertEqual(result["counts"]["source_files"], 2)
        self.assertEqual(result["counts"]["unique_objects"], 1)
        derived = next(entry for entry in result["entries"] if entry["identity"] == "derived_from_bid")
        self.assertFalse(derived["independent_original"])
        self.assertEqual(derived["source_page_range"]["status"], "filename_inferred")
        self.assertEqual(derived["source_page_range"]["start"], 28)
        self.assertEqual(derived["source_page_range"]["end"], 29)
        self.assertEqual(Path(derived["stored_path"]).read_bytes(), original.read_bytes())
        self.assertEqual(derived["sha256"], hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertFalse(result["database_imported"])
        self.assertFalse(result["content_extracted"])

    def test_repeated_import_is_reproducible_and_does_not_modify_source(self):
        original = self.pdf("input.pdf")
        before = original.stat()
        first = import_directory(self.source, self.catalog)
        frozen = Path(first["manifest_path"]).read_bytes()
        second = import_directory(self.source, self.catalog)
        self.assertEqual(first, second)
        self.assertEqual(frozen, Path(second["manifest_path"]).read_bytes())
        self.assertEqual(before.st_mtime_ns, original.stat().st_mtime_ns)
        self.assertEqual(len(list((self.catalog / "objects").iterdir())), 1)

    def test_drafts_and_unselected_files_are_not_imported(self):
        self.pdf("半成品-工作稿.pdf")
        self.pdf("certificate.pdf")
        self.pdf("tender.pdf")
        result = import_directory(self.source, self.catalog, include_patterns=["certificate.pdf"])
        self.assertEqual(result["counts"]["source_files"], 1)
        self.assertEqual({item["reason"] for item in result["skipped"]}, {"excluded_by_policy", "not_selected"})

    def test_rejects_spoofed_extension_and_overlapping_roots(self):
        self.pdf("fake.pdf", b"not a PDF")
        with self.assertRaisesRegex(SourceImportError, "file_signature"):
            import_directory(self.source, self.catalog)
        with self.assertRaisesRegex(SourceImportError, "must_not_overlap"):
            import_directory(self.source, self.source / "catalog")

    def test_corrupted_stored_object_is_not_overwritten(self):
        self.pdf("input.pdf")
        first = import_directory(self.source, self.catalog)
        stored = Path(first["entries"][0]["stored_path"])
        stored.chmod(0o666)
        stored.write_bytes(b"damaged")
        with self.assertRaisesRegex(SourceImportError, "integrity_check"):
            import_directory(self.source, self.catalog)
        self.assertEqual(stored.read_bytes(), b"damaged")

    def test_verified_locator_does_not_upgrade_inferred_filename_pages(self):
        self.pdf("页10.pdf")
        result = import_directory(self.source, self.catalog, source_metadata={"页10.pdf": {"source_role": "derived_from_bid", "locator": {"pdf_page": 1, "verified": True}}})
        entry = result["entries"][0]
        self.assertEqual(entry["source_page_range"]["status"], "filename_inferred")
        self.assertTrue(entry["provenance"]["locator"]["verified"])

    def test_symlink_is_skipped_instead_of_followed(self):
        self.pdf("input.pdf")
        external = self.root / "outside.pdf"
        external.write_bytes(b"%PDF-1.7\nexternal\n")
        try:
            (self.source / "link.pdf").symlink_to(external)
        except OSError:
            self.skipTest("OS account cannot create symlinks")
        result = import_directory(self.source, self.catalog)
        self.assertEqual(result["counts"]["source_files"], 1)
        self.assertEqual(result["skipped"][0]["reason"], "linked_file")


if __name__ == "__main__":
    unittest.main()
