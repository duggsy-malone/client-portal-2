"""
Sends a slice of a file over HTTP without loading that slice into memory.

TransferNow and WeTransfer take big files in parts, which can be tens or
hundreds of MB each. Reading a whole part into memory before sending it can
use up a small (512 MB) server, so this streams the part from disk instead.
"""

import time
import requests


class FilePart:
    """A read-only view of bytes [start, start+size) of a file on disk.
    `requests` sends it in small chunks and uses len() for Content-Length."""

    def __init__(self, path, start, size):
        self._f = open(path, "rb")
        self._f.seek(start)
        self._remaining = size
        self._size = size

    def __len__(self):
        return self._size

    def read(self, amt=-1):
        if self._remaining <= 0:
            return b""
        if amt is None or amt < 0 or amt > self._remaining:
            amt = self._remaining
        amt = min(amt, 1024 * 1024)
        data = self._f.read(amt)
        self._remaining -= len(data)
        return data

    def close(self):
        self._f.close()


def put_file_part(url, path, start, size, timeout, attempts=3):
    """PUT one part, streamed from disk, retrying briefly on network errors
    and server errors. Returns the final response."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        part = FilePart(path, start, size)
        try:
            resp = requests.put(url, data=part, timeout=timeout)
            if resp.status_code < 500 or attempt == attempts:
                return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt == attempts:
                raise
        finally:
            part.close()
        time.sleep(2 * attempt)
    raise last_exc
