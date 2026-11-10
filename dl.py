#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地多连接下载加速器

用法：
  直接下载：   python dl.py <url> [-o 文件名] [--conns 16] [--dir D:\\Downloads]
  本地服务：   python dl.py --serve [--port 6800]     （供 Edge 扩展调用）

原理：对支持 Range 的服务端把文件切成 N 段并发拉取，突破"单连接限速"。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerr
from urllib import request as urlreq
from urllib.parse import unquote, urlparse

VERSION = "1.0"
MIN_SPLIT = 1 << 20        # 每个分块至少 1MB
READ_SIZE = 65536
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads")

JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str) -> str:
    name = _BAD.sub("_", name or "").strip().strip(".")
    return (name or "download.bin")[:150]


def name_from_disposition(cd) -> str:
    if not cd:
        return ""
    m = re.search(r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I)
    if m:
        return unquote(m.group(1))
    m = re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    return ""


def name_from_url(url: str) -> str:
    path = urlparse(url).path
    base = unquote(path.rsplit("/", 1)[-1])
    return base or "download.bin"


def probe(url: str, headers: dict, timeout: int = 20) -> dict:
    """探测文件大小、是否支持 Range、真实文件名。"""
    h = dict(headers)
    h.setdefault("Accept-Encoding", "identity")
    h["Range"] = "bytes=0-0"
    req = urlreq.Request(url, headers=h)
    with urlreq.urlopen(req, timeout=timeout) as r:
        status = r.status
        final = r.geturl()
        total = None
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            t = cr.rsplit("/", 1)[1].strip()
            if t.isdigit():
                total = int(t)
        if total is None and status == 200:
            cl = (r.headers.get("Content-Length") or "").strip()
            if cl.isdigit():
                total = int(cl)
        fname = name_from_disposition(r.headers.get("Content-Disposition")) or name_from_url(final)
        return {
            "final_url": final,
            "total": total,
            "ranges": (status == 206 and total is not None),
            "filename": safe_name(fname),
        }


class Job:
    def __init__(self, url, out=None, headers=None, conns=16, workdir=None, verbose=False):
        self.id = uuid.uuid4().hex[:8]
        self.url = url
        self.headers = {k: v for k, v in (headers or {}).items() if v}
        self.conns = max(1, min(int(conns or 16), 32))
        self.workdir = workdir or DEFAULT_DIR
        self.name = safe_name(out) if out else name_from_url(url)
        self.path = os.path.join(self.workdir, self.name)
        self.total = -1
        self.done = 0
        self.speed = 0.0
        self.state = "pending"
        self.error = ""
        self.verbose = verbose
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._sp_t = 0.0
        self._sp_b = 0
        self._parts: list[dict] = []
        self._saver_stop = threading.Event()

    # ---------- 对外状态 ----------
    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            done = self.done
        if self._sp_t and now - self._sp_t > 0.2:
            self.speed = max(0.0, (done - self._sp_b) / (now - self._sp_t))
            self._sp_t, self._sp_b = now, done
        pct = -1.0
        if self.total and self.total > 0:
            pct = min(100.0, done / self.total * 100.0)
        return {
            "id": self.id, "name": self.name, "url": self.url, "path": self.path,
            "total": self.total, "done": done, "percent": round(pct, 1),
            "speed": int(self.speed), "state": self.state, "error": self.error,
        }

    def cancel(self):
        self._cancel.set()

    def _add(self, n: int):
        with self._lock:
            self.done += n

    # ---------- 主流程 ----------
    def run(self):
        self.state = "running"
        self._sp_t, self._sp_b = time.time(), 0
        os.makedirs(self.workdir, exist_ok=True)
        threading.Thread(target=self._report, daemon=True).start()
        try:
            if not (self.url.lower().startswith("http://") or self.url.lower().startswith("https://")):
                raise RuntimeError("只支持 http/https 链接")
            info = probe(self.url, self.headers)
            self.url = info["final_url"]
            self.total = info["total"] if info["total"] else -1
            if not self.name or self.name == "download.bin":
                self.name = info["filename"]
            self.path = os.path.join(self.workdir, self.name)

            if info["ranges"] and self.conns > 1 and self.total and self.total >= MIN_SPLIT * 2:
                self._multi()
            else:
                self._single()
            self.state = "done" if not self._cancel.is_set() else "cancelled"
        except Exception as e:
            if self._cancel.is_set():
                self.state = "cancelled"
            else:
                self.state = "error"
                self.error = f"{type(e).__name__}: {e}"
        finally:
            self._saver_stop.set()
            if self.verbose:
                s = self.snapshot()
                if self.state == "done":
                    print(f"\r完成 {self.name}  {human(s['done'])}  ->  {self.path}")
                else:
                    print(f"\r[{self.state}] {self.name} {self.error}")

    def _report(self):
        while not self._cancel.is_set():
            time.sleep(1.0)
            if self.state != "running" or not self.verbose:
                if self.state != "running":
                    return
                continue
            s = self.snapshot()
            if s["total"] > 0:
                print(f'\r[{s["name"]}] {s["percent"]}%  {human(s["done"])}/{human(s["total"])}  '
                      f'{human(s["speed"])}/s   ', end="", flush=True)
            else:
                print(f'\r[{s["name"]}] {human(s["done"])}  {human(s["speed"])}/s   ', end="", flush=True)

    # ---------- 分块并发 ----------
    def _multi(self):
        size = self.total
        part = max(MIN_SPLIT, size // self.conns)
        parts, start = [], 0
        while start < size:
            end = min(start + part - 1, size - 1)
            parts.append({"start": start, "end": end, "done": 0})
            start = end + 1
        self._parts = parts

        sidecar = self.path + ".dlparts.json"
        if os.path.exists(sidecar):
            try:
                with open(sidecar, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if meta.get("total") == size and meta.get("url") == self.url:
                    saved = meta.get("done") or []
                    for i, v in enumerate(saved):
                        if i < len(parts):
                            parts[i]["done"] = max(0, min(int(v), parts[i]["end"] - parts[i]["start"] + 1))
            except Exception:
                pass

        with open(self.path, "wb") as f:
            f.truncate(size)

        threading.Thread(target=self._save_sidecar, args=(sidecar, size), daemon=True).start()

        errors: list[str] = []
        errors_lock = threading.lock = threading.Lock()

        def worker(idx: int):
            p = parts[idx]
            got = p["done"]
            if got:
                self._add(got)
            try:
                self._range(idx, p["start"] + got, parts)
            except Exception as ex:
                with errors_lock:
                    errors.append(f"块{idx + 1}: {ex}")

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(len(parts))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if self._cancel.is_set():
            raise RuntimeError("已取消")
        if errors:
            raise RuntimeError("; ".join(errors[:3]))
        try:
            os.remove(sidecar)
        except OSError:
            pass

    def _save_sidecar(self, sidecar: str, size: int):
        while not self._saver_stop.is_set():
            time.sleep(2.0)
            try:
                data = {
                    "url": self.url, "total": size, "path": self.path,
                    "done": [p["done"] for p in self._parts],
                }
                tmp = sidecar + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, sidecar)
            except Exception:
                pass

    def _range(self, idx: int, pos: int, parts: list[dict]):
        end = parts[idx]["end"]
        attempt = 0
        while pos <= end:
            if self._cancel.is_set():
                raise RuntimeError("已取消")
            h = dict(self.headers)
            h.setdefault("Accept-Encoding", "identity")
            h["Range"] = f"bytes={pos}-{end}"
            try:
                req = urlreq.Request(self.url, headers=h)
                with urlreq.urlopen(req, timeout=30) as r:
                    if r.status == 200:
                        raise RuntimeError("服务端忽略 Range，改单连接")
                    if r.status != 206:
                        raise RuntimeError(f"HTTP {r.status}")
                    with open(self.path, "r+b") as f:
                        f.seek(pos)
                        while pos <= end:
                            if self._cancel.is_set():
                                raise RuntimeError("已取消")
                            data = r.read(min(READ_SIZE, end - pos + 1))
                            if not data:
                                raise RuntimeError("连接提前结束")
                            f.write(data)
                            pos += len(data)
                            with self._lock:
                                self.done += len(data)
                            parts[idx]["done"] += len(data)
                attempt = 0
            except RuntimeError:
                raise
            except Exception:
                attempt += 1
                if attempt >= 4:
                    raise
                time.sleep(min(2 ** attempt, 8))

    # ---------- 单连接 ----------
    def _single(self):
        h = dict(self.headers)
        h.setdefault("Accept-Encoding", "identity")
        req = urlreq.Request(self.url, headers=h)
        with urlreq.urlopen(req, timeout=30) as r, open(self.path, "wb") as f:
            cl = (r.headers.get("Content-Length") or "").strip()
            if self.total <= 0 and cl.isdigit():
                self.total = int(cl)
            while True:
                if self._cancel.is_set():
                    raise RuntimeError("已取消")
                data = r.read(READ_SIZE)
                if not data:
                    break
                f.write(data)
                self._add(len(data))


def start_job(url, out=None, headers=None, conns=16, workdir=None, verbose=False) -> Job:
    job = Job(url, out=out, headers=headers, conns=conns, workdir=workdir, verbose=verbose)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=job.run, daemon=True).start()
    return job


# ---------- 本地 API ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        origin = self.headers.get("Origin", "") or ""
        if origin.startswith("chrome-extension://") or origin.startswith("http://127.0.0.1") \
                or origin.startswith("http://localhost"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ping":
            return self._json({"ok": True, "version": VERSION})
        if path == "/list":
            with JOBS_LOCK:
                return self._json({"jobs": [j.snapshot() for j in JOBS.values()]})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)

        if path == "/add":
            url = (payload.get("url") or "").strip()
            if not url:
                return self._json({"error": "缺少 url"}, 400)
            try:
                job = start_job(
                    url,
                    out=payload.get("out") or None,
                    headers=payload.get("headers") or {},
                    conns=payload.get("conns") or 16,
                    workdir=payload.get("dir") or None,
                )
                return self._json({"id": job.id, "name": job.name})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if path == "/cancel":
            jid = payload.get("id") or ""
            with JOBS_LOCK:
                job = JOBS.get(jid)
            if job:
                job.cancel()
                return self._json({"ok": True})
            return self._json({"error": "没有这个任务"}, 404)

        self._json({"error": "not found"}, 404)


def serve(port: int):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"下载加速服务已启动: http://127.0.0.1:{port}   默认目录: {DEFAULT_DIR}")
    print("保持这个窗口开着，浏览器里的下载才会转给本地引擎。Ctrl+C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def main():
    ap = argparse.ArgumentParser(description="本地多连接下载加速器")
    ap.add_argument("url", nargs="?", help="要下载的链接")
    ap.add_argument("-o", "--out", help="保存的文件名")
    ap.add_argument("--dir", help=f"保存目录，默认 {DEFAULT_DIR}")
    ap.add_argument("--conns", type=int, default=16, help="并发连接数，默认 16")
    ap.add_argument("--serve", action="store_true", help="启动本地 API 服务")
    ap.add_argument("--port", type=int, default=6800, help="API 端口，默认 6800")
    args = ap.parse_args()

    if args.serve:
        return serve(args.port)
    if not args.url:
        ap.print_help()
        return

    job = Job(args.url, out=args.out, conns=args.conns, workdir=args.dir, verbose=True)
    job.run()
    if job.state == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
