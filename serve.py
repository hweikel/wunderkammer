#!/usr/bin/env python3
# run with: python3 serve.py /path/to/photos

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

PHOTOS_DIR: str = ""
SCRIPT_DIR: Path = Path(__file__).parent
PHOTO_CACHE: dict[str, float] = {}  # filename -> mtime
TAGS: dict[str, list[str]] = {}
TAGS_FILE: str = ""

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
CHUNK_SIZE = 65536  # 64 KB streaming chunks


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def load_photo_cache() -> None:
    global PHOTO_CACHE
    PHOTO_CACHE = {}
    try:
        with os.scandir(PHOTOS_DIR) as it:
            for entry in it:
                if entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in PHOTO_EXTENSIONS:
                        PHOTO_CACHE[entry.name] = entry.stat().st_mtime
    except OSError as exc:
        print(f"Error scanning directory: {exc}", file=sys.stderr)


def load_tags() -> None:
    global TAGS
    try:
        with open(TAGS_FILE, "r", encoding="utf-8") as fh:
            TAGS = json.load(fh)
    except FileNotFoundError:
        TAGS = {}
    except json.JSONDecodeError as exc:
        print(f"Warning: tags.json is malformed ({exc}), starting empty.", file=sys.stderr)
        TAGS = {}


def save_tags() -> None:
    with open(TAGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(TAGS, fh, indent=2)


def backfill_tags() -> None:
    """Add an empty-array entry for any image file not yet in TAGS, then save."""
    added = [fname for fname in PHOTO_CACHE if fname not in TAGS]
    if not added:
        return
    for fname in added:
        TAGS[fname] = []
    save_tags()
    print(f"Tags backfilled  : {len(added)} new entries added to tags.json")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class PhotoHandler(BaseHTTPRequestHandler):

    # --- logging -----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        print(f"{self.address_string()} - {fmt % args}")

    # --- helpers -----------------------------------------------------------

    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        raw = self.rfile.read(length)
        return json.loads(raw)

    def safe_filename(self, raw: str) -> Optional[str]:
        """Return the filename if it is safe (no path traversal), else None."""
        name = os.path.basename(unquote(raw))
        if not name or name.startswith("."):
            return None
        return name

    # --- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self._serve_index()

        elif path == "/api/photos":
            self._get_photos(query)

        elif path.startswith("/api/photos/") and path.endswith("/tags"):
            inner = path[len("/api/photos/"):-len("/tags")]
            filename = self.safe_filename(inner)
            if filename is None:
                self.send_error_json(400, "Invalid filename")
            else:
                self._get_tags(filename)

        elif path.startswith("/photo/"):
            raw = path[len("/photo/"):]
            filename = self.safe_filename(raw)
            if filename is None:
                self.send_error_json(400, "Invalid filename")
            else:
                self._serve_photo(filename)

        else:
            self.send_error_json(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/photos/") and path.endswith("/tags"):
            inner = path[len("/api/photos/"):-len("/tags")]
            filename = self.safe_filename(inner)
            if filename is None:
                self.send_error_json(400, "Invalid filename")
            else:
                self._add_tag(filename)
        else:
            self.send_error_json(404, "Not found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if not path.startswith("/api/photos/"):
            self.send_error_json(404, "Not found")
            return

        rest = path[len("/api/photos/"):]

        if "/tags/" in rest:
            # DELETE /api/photos/{filename}/tags/{tag}
            fname_part, tag_part = rest.split("/tags/", 1)
            filename = self.safe_filename(fname_part)
            tag = unquote(tag_part)
            if filename is None:
                self.send_error_json(400, "Invalid filename")
            else:
                self._remove_tag(filename, tag)
        else:
            # DELETE /api/photos/{filename}
            filename = self.safe_filename(rest)
            if filename is None:
                self.send_error_json(400, "Invalid filename")
            else:
                self._delete_photo(filename)

    # --- handlers ----------------------------------------------------------

    def _serve_index(self) -> None:
        index_path = SCRIPT_DIR / "index.html"
        try:
            content = index_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error_json(404, "index.html not found")

    def _get_photos(self, query: dict) -> None:
        tag_filter = query.get("tag", [None])[0]
        sort = query.get("sort", [None])[0]

        if tag_filter == "untagged":
            result = [fname for fname in PHOTO_CACHE if not TAGS.get(fname)]
        elif tag_filter:
            result = [
                fname for fname, tags in TAGS.items()
                if tag_filter in tags and fname in PHOTO_CACHE
            ]
        else:
            result = list(PHOTO_CACHE)

        if sort == "recent":
            result.sort(key=lambda f: PHOTO_CACHE[f], reverse=True)
        else:
            result.sort()

        self.send_json(result)

    def _get_tags(self, filename: str) -> None:
        if filename not in PHOTO_CACHE:
            self.send_error_json(404, "Photo not found")
            return
        self.send_json(TAGS.get(filename, []))

    def _serve_photo(self, filename: str) -> None:
        if filename not in PHOTO_CACHE:
            self.send_error_json(404, "Photo not found")
            return

        filepath = os.path.join(PHOTOS_DIR, filename)
        ext = os.path.splitext(filename)[1].lower()
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

        try:
            file_size = os.path.getsize(filepath)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            with open(filepath, "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except BrokenPipeError:
            pass  # client disconnected mid-transfer
        except OSError as exc:
            self.send_error_json(500, str(exc))

    def _add_tag(self, filename: str) -> None:
        if filename not in PHOTO_CACHE:
            self.send_error_json(404, "Photo not found")
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_error_json(400, "Invalid JSON body")
            return
        if not body or "tag" not in body:
            self.send_error_json(400, 'Missing "tag" field')
            return
        tag = str(body["tag"]).strip()
        if not tag:
            self.send_error_json(400, "Tag cannot be empty")
            return
        if filename not in TAGS:
            TAGS[filename] = []
        if tag not in TAGS[filename]:
            TAGS[filename].append(tag)
            save_tags()
        self.send_json(TAGS[filename])

    def _remove_tag(self, filename: str, tag: str) -> None:
        if filename not in PHOTO_CACHE:
            self.send_error_json(404, "Photo not found")
            return
        if filename in TAGS and tag in TAGS[filename]:
            TAGS[filename].remove(tag)
            if not TAGS[filename]:
                del TAGS[filename]
            save_tags()
        self.send_json(TAGS.get(filename, []))

    def _delete_photo(self, filename: str) -> None:
        if filename not in PHOTO_CACHE:
            self.send_error_json(404, "Photo not found")
            return
        filepath = os.path.join(PHOTOS_DIR, filename)
        try:
            os.remove(filepath)
        except OSError as exc:
            self.send_error_json(500, f"Failed to delete: {exc}")
            return
        PHOTO_CACHE.pop(filename, None)
        if filename in TAGS:
            del TAGS[filename]
            save_tags()
        self.send_json({"deleted": filename})


# ---------------------------------------------------------------------------
# Threaded server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Photo Wunderkammer Server")
    parser.add_argument("photos_dir", help="Directory containing photos")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()

    global PHOTOS_DIR, TAGS_FILE
    PHOTOS_DIR = os.path.abspath(args.photos_dir)

    if not os.path.isdir(PHOTOS_DIR):
        print(f"Error: {PHOTOS_DIR!r} is not a directory", file=sys.stderr)
        sys.exit(1)

    TAGS_FILE = os.path.join(PHOTOS_DIR, "tags.json")

    load_tags()
    load_photo_cache()
    backfill_tags()

    print(f"Photos directory : {PHOTOS_DIR}")
    print(f"Tags file        : {TAGS_FILE}")
    print(f"Photos found     : {len(PHOTO_CACHE)}")
    print(f"Listening on     : http://localhost:{args.port}")

    server = ThreadedHTTPServer(("", args.port), PhotoHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
