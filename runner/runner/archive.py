from __future__ import annotations

import os
import tarfile
from pathlib import Path


def safe_extract(tf: tarfile.TarFile, dest_dir: Path) -> None:
    dest_dir = dest_dir.resolve()
    for member in tf.getmembers():
        member_path = (dest_dir / member.name).resolve()
        if not str(member_path).startswith(str(dest_dir) + os.sep):
            raise ValueError(f"Blocked path traversal in tar entry: {member.name}")
        if member.issym() or member.islnk():
            link_path = (member_path.parent / member.linkname).resolve()
            if not str(link_path).startswith(str(dest_dir) + os.sep):
                raise ValueError(f"Blocked unsafe link in tar entry: {member.name}")
    tf.extractall(path=dest_dir)
