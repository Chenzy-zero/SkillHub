"""Tests for safe cross-platform workspace removal."""

from __future__ import annotations

import errno
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from skill_batch_review.filesystem import remove_tree


class RemoveTreeTests(unittest.TestCase):
    def test_windows_unlocks_read_only_git_pack_file(self) -> None:
        unlink = Mock()
        denied = PermissionError(errno.EACCES, "access denied")

        def fake_rmtree(path, *, onerror):
            self.assertEqual(path, Path("transport.git"))
            onerror(
                unlink,
                "transport.git/objects/pack/example.idx",
                (PermissionError, denied, None),
            )

        with (
            patch("skill_batch_review.filesystem.shutil.rmtree", side_effect=fake_rmtree),
            patch("skill_batch_review.filesystem.os.chmod") as chmod,
        ):
            remove_tree("transport.git", platform_name="nt")

        chmod.assert_called_once_with(
            "transport.git/objects/pack/example.idx",
            0o600,
        )
        unlink.assert_called_once_with("transport.git/objects/pack/example.idx")

    def test_windows_retries_transient_access_denied(self) -> None:
        denied = PermissionError(errno.EACCES, "temporarily locked")
        with (
            patch(
                "skill_batch_review.filesystem.shutil.rmtree",
                side_effect=(denied, None),
            ) as rmtree,
            patch("skill_batch_review.filesystem.time.sleep") as sleep,
        ):
            remove_tree("transport.git", platform_name="nt")
        self.assertEqual(rmtree.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_non_windows_uses_standard_removal(self) -> None:
        with patch("skill_batch_review.filesystem.shutil.rmtree") as rmtree:
            remove_tree("workspace", platform_name="posix")
        rmtree.assert_called_once_with(Path("workspace"))

    def test_non_permission_error_is_not_hidden(self) -> None:
        failure = OSError(errno.EIO, "disk error")
        with patch(
            "skill_batch_review.filesystem.shutil.rmtree",
            side_effect=failure,
        ):
            with self.assertRaises(OSError) as raised:
                remove_tree("transport.git", platform_name="nt")
        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
