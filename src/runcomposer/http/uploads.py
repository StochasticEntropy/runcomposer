"""Receiving a pushed results bundle (DESIGN.md §5): a ``multipart/form-data``
body read as a stream, bounded *while* it is read.

Nothing here buffers a body before looking at it. The bytes arrive chunk by
chunk from the connection; each file part goes straight into the bundle
directory under a name checked before its first byte is written; the size
limits are counted as the bytes come in, so an endless body — chunked
transfer encoding has no ``Content-Length``, and a declared length proves
nothing — is refused once it passes the limit, not after it ended.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, BinaryIO

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request

from runcomposer.core.errors import InvalidRequest, PayloadTooLarge
from runcomposer.core.filenames import child_path, name_collision_key, windows_name_problem

if TYPE_CHECKING:
    from python_multipart.exceptions import FormParserError
    from python_multipart.multipart import MultipartCallbacks, MultipartParser, parse_options_header
else:
    try:
        from python_multipart.exceptions import FormParserError
        from python_multipart.multipart import MultipartParser, parse_options_header
    except ModuleNotFoundError:  # python-multipart before 0.0.13
        from multipart.exceptions import FormParserError
        from multipart.multipart import MultipartParser, parse_options_header

__all__ = ["FILES_FIELD", "ReceivedBundle", "receive_bundle", "upload_name"]

#: The form field the bundle's files arrive under.
FILES_FIELD = "files"
#: More file parts than this in one push is refused — a bundle is a handful
#: of result files, not an inode flood.
MAX_FILES = 1000
#: A text field (``shard``, ``dispatch``, ``format``) is an id, not a document.
MAX_FIELD_BYTES = 4096
#: The headers of one part.
MAX_PART_HEADER_BYTES = 16 * 1024
#: The longest file name most file systems keep.
MAX_NAME_BYTES = 255


@dataclass
class ReceivedBundle:
    #: The name of every file written, in the order the parts arrived.
    files: list[str] = field(default_factory=list)
    #: The text fields of the form.
    fields: dict[str, str] = field(default_factory=dict)


def upload_name(raw: str) -> str:
    """The name a pushed file is kept under: its base name — a bundle is
    flat, so a client's directories (``reports/output.xml``, or a Windows
    path) are dropped — refused with ``InvalidRequest`` when that is not
    a plain file name: empty, ``.`` or ``..`` (also as compatibility
    look-alikes that normalize to them), a control character anywhere,
    a drive or root of its own, or longer than a file system keeps. On a
    server that runs Windows, also a name Windows cannot keep as written
    (:func:`~runcomposer.core.filenames.windows_name_problem`); a Linux or
    macOS server keeps such a name, as it always did."""
    if any(unicodedata.category(char) == "Cc" for char in raw):
        raise InvalidRequest(
            f"upload file name {raw!r} contains a control character — not a file name"
        )
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    folded = unicodedata.normalize("NFKC", name)
    if (
        name in ("", ".", "..")
        or folded in (".", "..")
        or "/" in folded
        or "\\" in folded
        or PureWindowsPath(name).drive
        or PureWindowsPath(name).anchor
        or PurePosixPath(name).anchor
    ):
        raise InvalidRequest(f"upload file name {raw!r} does not name a file")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise InvalidRequest(
            f"upload file name {name[:40]!r}… is longer than {MAX_NAME_BYTES} bytes"
        )
    problem = windows_name_problem(name) if os.name == "nt" else None
    if problem is not None:
        raise InvalidRequest(f"upload file name {raw!r} {problem} — this server runs on Windows")
    return name


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


class _Parts:
    """The parser's callbacks: every file part of ``files`` goes into
    ``directory``; text fields are kept; every other file part is read past.
    Runs in a worker thread (file writes stay off the event loop)."""

    def __init__(self, directory: Path, max_file_bytes: int, too_large: PayloadTooLarge):
        self.directory = directory
        self.max_file_bytes = max_file_bytes
        self.too_large = too_large
        self.received = ReceivedBundle()
        self.ended = False
        self._file_bytes = 0
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._header_bytes = 0
        self._disposition = b""
        self._field = ""
        self._out: BinaryIO | None = None
        self._value: bytearray | None = None

    def callbacks(self) -> MultipartCallbacks:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    def _header_part(self, target: bytearray, data: bytes, start: int, end: int) -> None:
        self._header_bytes += end - start
        if self._header_bytes > MAX_PART_HEADER_BYTES:
            raise InvalidRequest(f"the headers of a part exceed {MAX_PART_HEADER_BYTES} bytes")
        target.extend(data[start:end])

    def on_part_begin(self) -> None:
        self._header_bytes = 0
        self._disposition = b""
        self._field = ""
        self._value = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_part(self._header_name, data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_part(self._header_value, data, start, end)

    def on_header_end(self) -> None:
        if bytes(self._header_name).strip().lower() == b"content-disposition":
            self._disposition = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        if b"name" not in options:
            raise InvalidRequest("a part of the multipart body has no Content-Disposition name")
        self._field = _text(options[b"name"])
        if b"filename" not in options:
            self._value = bytearray()
            return
        if self._field != FILES_FIELD:
            return  # a file under another field is not part of the bundle
        if len(self.received.files) >= MAX_FILES:
            raise InvalidRequest(f"more than {MAX_FILES} files in one push")
        name = upload_name(_text(options[b"filename"]))
        try:
            target = child_path(self.directory, name)  # upload_name guarantees it; this proves it
        except ValueError:
            raise InvalidRequest(f"upload file name {name!r} does not name a file") from None
        try:
            # Exclusively: whether two names are one file is the file
            # system's answer — `Results.json` after `results.json` is a
            # second file on Linux, the same one on Windows and macOS.
            target.touch(exist_ok=False)
        except FileExistsError:
            key = name_collision_key(name)
            earlier = [kept for kept in self.received.files if name_collision_key(kept) == key]
            raise InvalidRequest(
                f"upload file name {name!r} names a file this push already holds"
                + (f" ({', '.join(map(repr, earlier))})" if earlier else "")
                + " on this server's file system — the names of a bundle are distinct"
            ) from None
        self._out = open(target, "wb")  # noqa: SIM115 — closed at the part's end or on any error
        self.received.files.append(name)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._out is not None:
            self._file_bytes += end - start
            if self._file_bytes > self.max_file_bytes:
                raise self.too_large
            self._out.write(data[start:end])
        elif self._value is not None:
            if len(self._value) + end - start > MAX_FIELD_BYTES:
                raise InvalidRequest(f"form field {self._field!r} is longer than {MAX_FIELD_BYTES} bytes")
            self._value.extend(data[start:end])

    def on_part_end(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None
        elif self._value is not None:
            self.received.fields[self._field] = _text(bytes(self._value))
            self._value = None

    def on_end(self) -> None:
        self.ended = True

    def close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None


async def receive_bundle(
    request: Request,
    directory: Path,
    *,
    max_file_bytes: int,
    max_body_bytes: int,
    too_large: PayloadTooLarge,
) -> ReceivedBundle:
    """Read the request's multipart body into ``directory``.

    ``max_file_bytes`` bounds the files together, ``max_body_bytes`` the
    whole body as it arrives (the files plus the multipart envelope) — both
    counted while reading: once either is passed, ``too_large`` is raised
    and the rest of the body is never read. A body that is not
    ``multipart/form-data``, cannot be parsed, ends before its closing
    boundary or carries no file under ``files`` is an ``InvalidRequest``;
    so is a file name that is not one (:func:`upload_name`)."""
    content_type, params = parse_options_header(request.headers.get("content-type"))
    if content_type != b"multipart/form-data" or not params.get(b"boundary"):
        raise InvalidRequest(
            "a results push is a multipart/form-data body (files under 'files'), "
            f"not {request.headers.get('content-type') or 'a body without a Content-Type'}"
        )
    parts = _Parts(directory, max_file_bytes, too_large)
    parser = MultipartParser(params[b"boundary"], parts.callbacks())
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_body_bytes:
                raise too_large
            if chunk:
                await run_in_threadpool(parser.write, chunk)
        await run_in_threadpool(parser.finalize)
    except FormParserError as exc:
        raise InvalidRequest(f"the multipart body cannot be read: {exc}") from None
    except ClientDisconnect:
        raise InvalidRequest("the client went away before the body was complete") from None
    finally:
        parts.close()
    if not parts.ended:
        raise InvalidRequest("the multipart body ended before its closing boundary")
    if not parts.received.files:
        raise InvalidRequest(f"a results push carries at least one file under {FILES_FIELD!r}")
    return parts.received
