from __future__ import annotations

import io
import os
import uuid
import zipfile
import threading
import json
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

from .orchestrator import Orchestrator
from .run_manager import RunManager
from .tool_router import ToolRouter

_ZIP_CACHE: Dict[str, bytes] = {}
_JOB_STATUS: Dict[str, Dict[str, str]] = {}


HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Agent CLI UI</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 24px; }
      .box { max-width: 900px; margin: 0 auto; }
      textarea { width: 100%; height: 180px; }
      input[type="file"] { width: 100%; }
      .btn { padding: 10px 16px; margin-top: 12px; }
      .note { color: #666; font-size: 12px; }
    </style>
  </head>
  <body>
    <div class="box">
      <h2>Agent 需求输入</h2>
      <form method="POST" enctype="multipart/form-data">
        <label>需求描述（文本）</label><br />
        <textarea name="task" placeholder="例如：修复构建错误并补齐测试"></textarea>
        <br /><br />
        <label>或上传需求文件（.txt/.md）</label><br />
        <input type="file" name="task_file" />
        <br />
        <button class="btn" type="submit">提交并生成源码</button>
      </form>
      <p class="note">提示：提交后会自动运行 agent（auto 模式）。完成后可下载源码压缩包。</p>
    </div>
  </body>
</html>
"""


def _collect_task(task_text: str, file_bytes: Optional[bytes]) -> str:
    task_text = (task_text or "").strip()
    file_text = ""
    if file_bytes:
        file_text = file_bytes.decode("utf-8", errors="replace").strip()
    if task_text and file_text:
        return task_text + "\n\n" + file_text
    return task_text or file_text or ""


def _zip_workspace(repo_root: Path) -> bytes:
    buf = io.BytesIO()
    ignore_dirs = {".git", ".agent", "__pycache__", "runtime", "build"}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in repo_root.rglob("*"):
            rel = path.relative_to(repo_root)
            if any(part in ignore_dirs for part in rel.parts):
                continue
            if path.is_dir():
                continue
            zf.write(path, rel.as_posix())
    return buf.getvalue()


def _collect_changed_files(run_manager: RunManager, repo_root: Path) -> list[dict]:
    state = run_manager.load_latest()
    if not state:
        return []
    patches_dir = state.run_dir / "patches"
    if not patches_dir.exists():
        return []
    files = []
    seen = set()
    for patch_path in sorted(patches_dir.glob("*.diff")):
        try:
            payload = json.loads(patch_path.read_text(encoding="utf-8"))
            if isinstance(payload, str):
                payload = json.loads(payload)
            file_path = payload.get("file_path")
        except Exception:
            continue
        if not file_path or file_path in seen:
            continue
        full_path = repo_root / file_path
        if not full_path.exists() or full_path.is_dir():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        seen.add(file_path)
        files.append({"path": file_path, "content": content})
    return files


def _truncate_outputs(files: list[dict], max_total: int = 300_000, max_per_file: int = 50_000) -> list[dict]:
    total = 0
    result = []
    for f in files:
        text = f.get("content", "")
        truncated = False
        if len(text) > max_per_file:
            text = text[:max_per_file] + "\n... [truncated] ..."
            truncated = True
        total += len(text)
        if total > max_total:
            break
        result.append({"path": f.get("path", ""), "content": text, "truncated": truncated})
    return result


class AgentUIHandler(BaseHTTPRequestHandler):
    server_version = "AgentUI/1.0"

    def _send(self, status: int, content: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/status/"):
            job_id = parsed.path.split("/status/")[-1]
            status = _JOB_STATUS.get(job_id)
            if not status:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
                return
            payload = json.dumps(status, ensure_ascii=False).encode("utf-8")
            self._send(HTTPStatus.OK, payload, "application/json; charset=utf-8")
            return
        if parsed.path.startswith("/download/"):
            token = parsed.path.split("/download/")[-1]
            data = _ZIP_CACHE.get(token)
            if not data:
                self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                return
            self._send(HTTPStatus.OK, data, "application/zip")
            return
        self._send(HTTPStatus.OK, HTML_PAGE.encode("utf-8"))

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")
        task_text = ""
        file_bytes = None

        if content_type.startswith("multipart/form-data"):
            import cgi

            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            task_text = form.getfirst("task", "")
            file_item = form["task_file"] if "task_file" in form else None
            if file_item is not None and getattr(file_item, "file", None):
                file_bytes = file_item.file.read()
        else:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            task_text = parse_qs(body).get("task", [""])[0]

        task = _collect_task(task_text, file_bytes)
        if not task:
            self._send(HTTPStatus.BAD_REQUEST, b"empty task", "text/plain; charset=utf-8")
            return

        repo_root = Path.cwd()
        run_manager = RunManager(repo_root)
        tool_router = ToolRouter(repo_root)
        job_id = uuid.uuid4().hex
        _JOB_STATUS[job_id] = {"state": "running", "message": "任务执行中..."}

        def _worker():
            try:
                orchestrator = Orchestrator(repo_root, run_manager, tool_router)
                orchestrator.run(task, auto=True)
                zip_bytes = _zip_workspace(repo_root)
                token = uuid.uuid4().hex
                _ZIP_CACHE[token] = zip_bytes
                files = _collect_changed_files(run_manager, repo_root)
                files = _truncate_outputs(files)
                _JOB_STATUS[job_id] = {
                    "state": "done",
                    "download_url": f"/download/{token}",
                    "files": files,
                }
            except Exception as exc:
                _JOB_STATUS[job_id] = {"state": "error", "message": str(exc)}

        threading.Thread(target=_worker, daemon=True).start()

        html = f"""<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8" /><title>处理中</title></head>
  <body>
    <h3>处理中</h3>
    <p id="status">任务执行中，请稍候...</p>
    <div id="download"></div>
    <div id="code"></div>
    <script>
      const jobId = "{job_id}";
      async function poll() {{
        const res = await fetch(`/status/${{jobId}}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.state === "done") {{
          document.getElementById("status").innerText = "已完成";
          document.getElementById("download").innerHTML = `<a href="${{data.download_url}}">点击下载源码压缩包</a>`;
          const codeDiv = document.getElementById("code");
          if (Array.isArray(data.files) && data.files.length > 0) {{
            let html = "<h4>生成的需求代码：</h4>";
            data.files.forEach(f => {{
              const safe = (f.content || "").replace(/[&<>]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;"}}[c]));
              html += `<h5>${{f.path}}</h5><pre style="background:#111;color:#eee;padding:12px;white-space:pre-wrap;">${{safe}}</pre>`;
            }});
            codeDiv.innerHTML = html;
          }} else {{
            codeDiv.innerHTML = "<p>未找到可展示的修改文件。</p>";
          }}
        }} else if (data.state === "error") {{
          document.getElementById("status").innerText = "执行失败：" + (data.message || "");
        }} else {{
          setTimeout(poll, 2000);
        }}
      }}
      poll();
    </script>
    <p><a href="/">返回继续提交</a></p>
  </body>
</html>"""
        self._send(HTTPStatus.OK, html.encode("utf-8"))


def run_server(host: str = "127.0.0.1", port: int = 8080):
    try:
        print(">>> DEBUG: Preparing HTTPServer...", flush=True)
        httpd = HTTPServer((host, port), AgentUIHandler)
        print(">>> DEBUG: HTTPServer created", flush=True)
        print(f"Agent UI listening on http://{host}:{port}")
        print(f"请在浏览器访问: http://{host}:{port}")
        print(">>> DEBUG: Entering serve_forever()", flush=True)
        httpd.serve_forever()
        print(">>> DEBUG: serve_forever() returned", flush=True)
    except KeyboardInterrupt:
        print("\n服务器已停止")
    except Exception as e:
        print(f"启动服务器失败: {e}")
        import traceback
        traceback.print_exc()
        raise

