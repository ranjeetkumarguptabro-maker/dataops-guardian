import unittest

from dataguardian.version import check_version


class TestCheckVersion(unittest.TestCase):
    def test_matching_versions_pass(self):
        result = check_version("8.15.0", "8.15.0")
        self.assertEqual(result["status"], "passed")

    def test_mismatched_versions_fail(self):
        result = check_version("8.12.0", "8.15.0")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["message"], "Version mismatch")

    def test_whitespace_is_removed(self):
        result = check_version(" 8.15.0 ", "8.15.0")
        self.assertEqual(result["status"], "passed")

    def test_empty_actual_version_fails(self):
        result = check_version("", "8.15.0")
        self.assertEqual(result["status"], "failed")

    def test_empty_required_version_fails(self):
        result = check_version("8.15.0", " ")
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
