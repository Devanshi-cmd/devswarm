"""Bounded capture of command output."""

from __future__ import annotations

from typing import IO


class HeadTailBuffer:
    """Keeps the first and last part of a stream and counts what was dropped.

    Memory stays bounded however much a command prints, so ``yes`` cannot exhaust the
    host, and the agent still sees how a log started and how it ended.
    """

    def __init__(self, head: int, tail: int) -> None:
        self._head_limit = head
        self._tail_limit = tail
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0

    def feed(self, chunk: bytes) -> None:
        self._total += len(chunk)
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk and self._tail_limit > 0:
            self._tail += chunk
            del self._tail[: -self._tail_limit]

    def drain(self, stream: IO[bytes]) -> None:
        while chunk := stream.read1(65_536):  # type: ignore[attr-defined]
            self.feed(chunk)

    def text(self) -> str:
        omitted = self._total - len(self._head) - len(self._tail)
        head = self._head.decode(errors="replace")
        tail = self._tail.decode(errors="replace")
        if omitted > 0:
            return f"{head}\n... [{omitted} bytes omitted] ...\n{tail}"
        return head + tail


def clip(text: str, head: int, tail: int) -> str:
    """Shorten text the same way command output is shortened."""
    buffer = HeadTailBuffer(head, tail)
    buffer.feed(text.encode())
    return buffer.text()
