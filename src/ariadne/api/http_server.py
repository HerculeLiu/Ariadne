from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
import cgi

from ariadne.api.facade import AriadneAPI
from ariadne.infrastructure.app_logger import get_logger


ROOT_DIR = Path(__file__).resolve().parents[3]
FRONTEND_INDEX = ROOT_DIR / "frontend" / "index.html"
FRONTEND_KNOWLEDGE = ROOT_DIR / "frontend" / "knowledge.html"
logger = get_logger("api.http")


class AriadneHandler(BaseHTTPRequestHandler):
    api = AriadneAPI()

    def _trace_id(self) -> str:
        return f"tr_{uuid4().hex[:10]}"

    def _send_common_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Access-Control-Allow-Origin", self.api.config.cors_allow_origin)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Request-Id, Idempotency-Key")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._send_common_headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)
        logger.info("http %s %s -> %s json_code=%s", self.command, self.path, status, payload.get("code") if isinstance(payload, dict) else None)

    def _send_html(self, html_text: str, status=200):
        body = html_text.encode("utf-8")
        self.send_response(status)
        self._send_common_headers("text/html; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)
        logger.info("http %s %s -> %s html", self.command, self.path, status)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _handle_multipart_file_upload(self):
        """
        Handle multipart/form-data file upload.

        Returns:
            dict with 'file_name', 'file_content', and 'size_bytes'
        """
        content_type = self.headers.get('Content-Type', '')
        if not content_type.startswith('multipart/form-data'):
            return None

        # Parse the multipart boundary
        boundary = None
        for part in content_type.split(';'):
            part = part.strip()
            if part.startswith('boundary='):
                boundary = part[len('boundary='):].strip('"')
                break

        if not boundary:
            logger.warning("Multipart upload without boundary")
            return None

        # Read the request body
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 50 * 1024 * 1024:  # 50MB limit
            logger.warning("Invalid content length: %d", length)
            return None

        body = self.rfile.read(length)

        # Parse multipart form data
        boundary_bytes = ('--' + boundary).encode('utf-8')
        parts = body.split(boundary_bytes)

        file_name = None
        file_content = None

        for part in parts:
            if b'Content-Disposition' not in part:
                continue

            # Extract headers and content
            if b'\r\n\r\n' not in part:
                continue

            headers, content = part.split(b'\r\n\r\n', 1)
            headers_str = headers.decode('utf-8', errors='ignore')

            # Check if this is the file part
            if 'name="file"' in headers_str or 'name="files[]"' in headers_str:
                # Extract filename from Content-Disposition
                import re
                filename_match = re.search(r'filename="([^"]*)"', headers_str)
                if filename_match:
                    file_name = filename_match.group(1)

                # Remove trailing \r\n from content
                if content.endswith(b'\r\n'):
                    content = content[:-2]
                file_content = content
                break

        if file_name and file_content is not None:
            return {
                'file_name': file_name,
                'file_content': file_content,
                'size_bytes': len(file_content),
            }

        return None

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_common_headers("text/plain; charset=utf-8", 0)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        logger.debug("do_GET path=%s query=%s", path, query)

        if path in {"/", "/index.html", "/frontend/index.html"}:
            if FRONTEND_INDEX.exists():
                return self._send_html(FRONTEND_INDEX.read_text(encoding="utf-8"))
            return self._send_html("<h1>Ariadne MVP</h1><p>frontend/index.html not found.</p>")

        if path in {"/knowledge.html", "/frontend/knowledge.html"}:
            if FRONTEND_KNOWLEDGE.exists():
                return self._send_html(FRONTEND_KNOWLEDGE.read_text(encoding="utf-8"))
            return self._send_html("<h1>Ariadne MVP</h1><p>frontend/knowledge.html not found.</p>")

        if path.startswith("/downloads/") and path.endswith(".html"):
            file_name = path.rsplit("/", 1)[-1]
            courseware_id = file_name[: -len(".html")]
            html_payload = self.api.export_html_content(courseware_id)
            if html_payload.get("code") != 0:
                return self._send_json(html_payload, status=404)
            return self._send_html(html_payload["data"]["html"])

        if path == "/api/v1/health/live":
            return self._send_json(self.api.health_live())
        if path == "/api/v1/health/ready":
            return self._send_json(self.api.health_ready())

        if path.startswith("/api/v1/coursewares/") and path.endswith("/progress"):
            courseware_id = path.split("/")[4]
            return self._send_json(self.api.get_progress(courseware_id))

        if path.startswith("/api/v1/coursewares/") and path.endswith("/chunks"):
            courseware_id = path.split("/")[4]
            include_content = query.get("include_content", ["true"])[0].lower() == "true"
            only_favorite = query.get("only_favorite", ["false"])[0].lower() == "true"
            return self._send_json(self.api.list_chunks(courseware_id, include_content=include_content, only_favorite=only_favorite))

        if path.startswith("/api/v1/coursewares/") and path.endswith("/markdown"):
            courseware_id = path.split("/")[4]
            return self._send_json(self.api.get_markdown(courseware_id))

        if path.startswith("/api/v1/coursewares/") and path.endswith("/html"):
            courseware_id = path.split("/")[4]
            return self._send_json(self.api.export_html_content(courseware_id))

        if path.startswith("/api/v1/coursewares/"):
            courseware_id = path.split("/")[4]
            return self._send_json(self.api.get_courseware(courseware_id))

        if path.startswith("/api/v1/assets/") and path.endswith("/parse-status"):
            asset_id = path.split("/")[4]
            return self._send_json(self.api.get_asset_status(asset_id))

        if path == "/api/v1/retrieval/settings":
            return self._send_json(self.api.get_retrieval_settings())

        if path == "/api/v1/profiles/current":
            return self._send_json(self.api.get_profile())

        if path == "/api/v1/chat/sessions":
            courseware_id = query.get("courseware_id", [None])[0]
            page_id = query.get("page_id", [None])[0]
            return self._send_json(self.api.list_chat_sessions(courseware_id, page_id))

        if path == "/api/v1/logs/events":
            event_type = query.get("event_type", [None])[0]
            return self._send_json(self.api.get_logs(event_type=event_type))

        if path == "/api/v1/metrics/performance":
            return self._send_json(self.api.get_metrics())

        self._send_json({"code": 10003, "message": "resource not found", "trace_id": "tr_http"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        content_type = self.headers.get('Content-Type', '')

        # Check if this is a multipart file upload
        if content_type.startswith('multipart/form-data') and path == "/api/v1/assets/upload":
            file_data = self._handle_multipart_file_upload()
            if file_data:
                return self._send_json(
                    self.api.upload_asset_with_content(
                        file_data['file_name'],
                        file_data['file_content'],
                        file_data['size_bytes'],
                    )
                )
            return self._send_json({"code": 10001, "message": "Invalid file upload", "trace_id": self._trace_id()}, status=400)

        # Regular JSON requests
        payload = self._read_json()
        logger.debug("do_POST path=%s keys=%s", path, list(payload.keys()))

        if path == "/api/v1/coursewares/generate":
            return self._send_json(self.api.generate_courseware(payload))

        if path.startswith("/api/v1/chunks/") and path.endswith("/ask"):
            chunk_id = path.split("/")[4]
            return self._send_json(self.api.ask_chunk(chunk_id, payload))

        if path.startswith("/api/v1/chunks/") and path.endswith("/append"):
            chunk_id = path.split("/")[4]
            return self._send_json(self.api.append_chunk(chunk_id, payload))

        if path == "/api/v1/assets/upload":
            file_name = payload.get("file_name", "")
            size_bytes = int(payload.get("size_bytes", 0))
            return self._send_json(self.api.upload_asset(file_name, size_bytes))

        if path.startswith("/api/v1/coursewares/") and path.endswith("/export"):
            courseware_id = path.split("/")[4]
            fmt = payload.get("format", "html")
            return self._send_json(self.api.export_courseware(courseware_id, fmt))

        if path == "/api/v1/chat/sessions":
            return self._send_json(self.api.create_chat_session(payload))

        if path == "/api/v1/chat/messages":
            return self._send_json(self.api.send_chat_message(payload))

        if path.startswith("/api/v1/pages/") and path.endswith("/rewrite-draft"):
            page_id = path.split("/")[4]
            return self._send_json(self.api.rewrite_draft(page_id, payload))

        if path.startswith("/api/v1/pages/") and path.endswith("/apply-draft"):
            page_id = path.split("/")[4]
            return self._send_json(self.api.apply_draft(page_id, payload))

        if path.startswith("/api/v1/coursewares/") and path.endswith("/undo"):
            courseware_id = path.split("/")[4]
            return self._send_json(self.api.undo(courseware_id, payload))

        if path == "/api/v1/intent/analyze":
            return self._send_json(self.api.analyze_intent(payload))

        if path.startswith("/api/v1/chunks/") and path.endswith("/apply"):
            chunk_id = path.split("/")[4]
            payload["chunk_id"] = chunk_id
            return self._send_json(self.api.apply_chunk_modification(payload))

        if path.startswith("/api/v1/chunks/") and path.endswith("/delete"):
            chunk_id = path.split("/")[4]
            return self._send_json(self.api.delete_chunk(chunk_id, payload))

        self._send_json({"code": 10003, "message": "resource not found", "trace_id": "tr_http"}, status=404)

    def do_PATCH(self):
        path = urlparse(self.path).path
        payload = self._read_json()
        logger.debug("do_PATCH path=%s keys=%s", path, list(payload.keys()))

        if path.startswith("/api/v1/chunks/") and path.endswith("/state"):
            chunk_id = path.split("/")[4]
            return self._send_json(self.api.update_chunk_state(chunk_id, payload))

        self._send_json({"code": 10003, "message": "resource not found", "trace_id": "tr_http"}, status=404)

    def do_PUT(self):
        path = urlparse(self.path).path
        payload = self._read_json()
        logger.debug("do_PUT path=%s keys=%s", path, list(payload.keys()))

        if path == "/api/v1/retrieval/settings":
            return self._send_json(self.api.put_retrieval_settings(payload))

        if path == "/api/v1/profiles/current":
            return self._send_json(self.api.put_profile(payload))

        if path.startswith("/api/v1/coursewares/") and path.endswith("/markdown"):
            courseware_id = path.split("/")[4]
            return self._send_json(self.api.put_markdown(courseware_id, payload))

        self._send_json({"code": 10003, "message": "resource not found", "trace_id": "tr_http"}, status=404)


def run(host="127.0.0.1", port=8000):
    server = HTTPServer((host, port), AriadneHandler)
    print(f"Ariadne API+Frontend listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
