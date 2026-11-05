"""Where uploaded files live: a local folder, or Google Drive in the cloud, behind the same calls."""
from __future__ import annotations

import mimetypes
import threading
from pathlib import Path
from typing import Protocol


class FileStore(Protocol):
    def save(self, parts: list[str], name: str, data: bytes) -> str: ...
    def read(self, uri: str) -> bytes: ...


class LocalFileStore:
    scheme = "local:"

    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, parts, name, data):
        folder = self.root.joinpath(*parts)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        if not path.exists():
            path.write_bytes(data)
        return self.scheme + path.relative_to(self.root).as_posix()

    def read(self, uri):
        if not uri.startswith(self.scheme):
            raise ValueError(f"not a local file uri: {uri[:20]}")
        return (self.root / uri[len(self.scheme):]).read_bytes()


FOLDER = "application/vnd.google-apps.folder"


class DriveFileStore:

    scheme = "drive:"

    def __init__(self, root_folder_id: str, credentials):
        self.root = root_folder_id
        self._credentials = credentials
        self._local = threading.local()
        self._folder_lock = threading.Lock()
        self._folders: dict[tuple[str, ...], str] = {}

    @property
    def drive(self):
        if getattr(self._local, "drive", None) is None:
            from googleapiclient.discovery import build
            self._local.drive = build("drive", "v3", credentials=self._credentials, cache_discovery=False)
        return self._local.drive

    def _find(self, parent: str, name: str, mime: str | None = None) -> str | None:
        q = f"'{parent}' in parents and name = '{name}' and trashed = false"
        if mime:
            q += f" and mimeType = '{mime}'"
        found = self.drive.files().list(q=q, fields="files(id)", pageSize=2).execute().get("files", [])
        return found[0]["id"] if found else None

    def _folder(self, parts: list[str]) -> str:
        with self._folder_lock:
            parent, path, fresh = self.root, (), False
            for part in parts:
                path += (part,)
                if path not in self._folders:
                    found = None if fresh else self._find(parent, part, FOLDER)
                    if not found:
                        found = self.drive.files().create(
                            body={"name": part, "mimeType": FOLDER, "parents": [parent]}, fields="id").execute()["id"]
                        fresh = True
                    self._folders[path] = found
                parent = self._folders[path]
            return parent

    def save(self, parts, name, data):
        from googleapiclient.http import MediaInMemoryUpload
        folder = self._folder(parts)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        media = MediaInMemoryUpload(data, mimetype=mime, resumable=False)
        file_id = self.drive.files().create(body={"name": name, "parents": [folder]}, media_body=media,
                                            fields="id").execute()["id"]
        return self.scheme + file_id

    def read(self, uri):
        if not uri.startswith(self.scheme):
            raise ValueError(f"not a drive file uri: {uri[:20]}")
        return self.drive.files().get_media(fileId=uri[len(self.scheme):]).execute()
