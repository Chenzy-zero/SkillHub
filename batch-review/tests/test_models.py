import unittest

from skill_batch_review.models import ReviewTargetKey, SourceKey, normalize_branch, normalize_skill_path


class SourceModelTests(unittest.TestCase):
    def test_source_and_target_keys_are_distinct(self) -> None:
        source = SourceKey("team/skills", "refs/heads/main", "tools/example/", "Example Skill")
        target = ReviewTargetKey("team/skills", "tools/example")
        self.assertEqual(source.branch, "main")
        self.assertEqual(source.skill_path, target.skill_path)

    def test_traversal_and_invalid_branch_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_skill_path("skills/../outside")
        with self.assertRaises(ValueError):
            normalize_skill_path("/absolute")
        with self.assertRaises(ValueError):
            normalize_branch("refs/tags/v1")

    def test_repository_root_path(self) -> None:
        self.assertEqual(normalize_skill_path("/"), ".")
        self.assertEqual(normalize_skill_path("./"), ".")

    def test_non_posix_and_duplicate_separators_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_skill_path("skills\\demo")
        with self.assertRaises(ValueError):
            normalize_skill_path("skills//demo")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
