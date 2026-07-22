#!/usr/bin/env python3
"""Serve Flutter's web build with HTTP byte-range support for PMTiles."""

from __future__ import annotations

import argparse
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """A static-file handler that supports PMTiles' Range requests."""

    _range: tuple[int, int] | None = None

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            file = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(file.fileno()).st_size
        self._range = None
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match:
            start = int(match.group(1) or 0)
            end = int(match.group(2) or size - 1)
            if start >= size or end < start:
                file.close()
                self.send_error(416, "Requested range not satisfiable")
                return None
            end = min(end, size - 1)
            self._range = (start, end)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(size))

        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return file

    def copyfile(self, source, outputfile):
        if self._range is None:
            return super().copyfile(source, outputfile)
        start, end = self._range
        source.seek(start)
        remaining = end - start + 1
        while remaining:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--directory", type=Path, default=Path("build/web"))
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_dir():
        raise SystemExit(f"Web build directory not found: {directory}")

    handler = lambda *a, **kw: RangeRequestHandler(*a, directory=str(directory), **kw)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Serving {directory} at http://localhost:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
