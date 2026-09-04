"""Small cross-platform filesystem operations used by review workspaces."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Callable


def _access_denied(error: BaseException) -> bool:
    return (
        isinstance(error, PermissionError)
        or getattr(error, "errno", None) in {errno.EACCES, errno.EPERM}
        or getattr(error, "winerror", None) == 5
    )


def remove_tree(
    path: str | Path,
    *,
    platform_name: str | None = None,
    attempts: int = 3,
) -> None:
    """Remove one tree, including read-only Git pack files on Windows.

    Git can leave ``objects/pack/*.idx`` and ``*.pack`` read-only.  Windows
    refuses to unlink those files until the write bit is restored.  A short
    retry also covers a transient antivirus/indexer handle without hiding a
    persistent error.
    """

    if attempts < 1:
        raise ValueError("attempts must be positive")
    target = Path(path)
    platform = os.name if platform_name is None else platform_name
    if platform != "nt":
        shutil.rmtree(target)
        return

    def unlock_and_retry(
        function: Callable[[str], object],
        filename: str,
        exc_info: tuple[type[BaseException], BaseException, object],
    ) -> None:
        error = exc_info[1]
        if not _access_denied(error):
            raise error
        os.chmod(filename, stat.S_IREAD | stat.S_IWRITE)
        function(filename)

    for attempt in range(attempts):
        try:
            shutil.rmtree(target, onerror=unlock_and_retry)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if not _access_denied(exc) or attempt + 1 >= attempts:
                raise
            time.sleep(0.1 * (attempt + 1))


__all__ = ["remove_tree"]
