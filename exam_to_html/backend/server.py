"""
exam_to_html.backend.server — FastAPI app

Endpoints:
  GET  /api/health                 → 健康检查 (uvicorn 启动探针)
  POST /api/convert                → 上传 PDF (multipart), 异步转 HTML, 返回 job_id
  GET  /api/jobs/{job_id}          → 查 job 状态 (前端轮询)
  POST /api/open-html              → 调 OS 默认浏览器打开 HTML
  GET  /api/config                 → 读 config.json
  POST /api/config                 → 写 config.json

设计权衡 (M1 求简):
- 任务状态存内存 dict (app 单实例, 多窗口场景留 v1.1)
- BackgroundTasks 异步跑 convert_pdf; 不做细进度, 二态 (processing/done/failed)
- 不做鉴权 (本地 localhost only, 不暴露公网)
- 路径白名单 (M7 安全加固): open-html / clear-incomplete / convert 输出目录都
  必须落进允许的根目录 (courseware/, project_root/), 避免 localhost 上的
  其他进程读 /Users/<...>/.ssh/id_rsa 等敏感路径触发任意文件删除/打开
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from .. import config as app_config
from ..paths import gui_static_dir, default_output_dir
from ..updater import THROTTLE_HOURS, check_update
from .pipeline import PipelineError, convert_pdf, scan_incomplete_uploads, clear_incomplete_uploads

log = logging.getLogger(__name__)

# ============================================================
# 安全: 路径白名单根 (H-3, H-5, H-6)
# ============================================================
# 仅允许服务操作以下白名单目录之下的文件:
#   - inbox/    (上传暂存, 仅 clear 残留时用)
#   - archive/  (解析后归档)
#   - courseware/images  (topic_garden 写图目录, 走 paths.courseware_images_dir)
#   - default_output_dir (默认输出目录 = 桌面)
#   - 系统临时目录 (上传临时落点)
# 其他路径 (e.g. /Users/z/.ssh/id_rsa) 一律拒绝。
def _resolve_allowed_roots() -> List[Path]:
    """返回允许操作的根目录列表 (绝对路径)。"""
    roots: List[Path] = []
    try:
        from ..paths import archive_dir, inbox_dir, courseware_images_dir
        roots.append(archive_dir().resolve())
        roots.append(inbox_dir().resolve())
        roots.append(default_output_dir().resolve())
        ci = courseware_images_dir()
        # parent.parent 因为 images/ 之下是图, 实际我们要允许 courseware/
        roots.append(ci.resolve().parent)
    except Exception as e:
        log.warning("[server] paths 解析失败, 白名单退化: %s", e)
    # 系统临时目录 — 上传文件临时落点
    import tempfile as _tf
    roots.append(Path(_tf.gettempdir()).resolve())
    return roots


def _is_path_within_allowed(path: Path) -> bool:
    """检查 path 是否在任一白名单根之下 (resolve 后比较)。"""
    try:
        abs_p = path.resolve()
    except (OSError, RuntimeError):
        return False
    for root in _resolve_allowed_roots():
        try:
            abs_p.relative_to(root)
            return True
        except ValueError:
            continue
    return False

app = FastAPI(
    title="exam-to-html",
    version=__version__,
    description="PDF → HTML 讲评课件生成器 (内部 HTTP API)",
)

# ============================================================
# 任务状态 (内存 dict, 单实例 OK)
# 修 M-16: 加 TTL + max size, 避免长跑后 _jobs 无限增长
# ============================================================
_JOBS_MAX_SIZE = 1000          # 上限 — 超出时清理最老的
_JOBS_TTL_SECONDS = 3600       # 1h 后访问时清理
_jobs_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        # 限长: 超过上限删最老的 (按 created_at)
        if len(_jobs) >= _JOBS_MAX_SIZE:
            sorted_jobs = sorted(
                _jobs.items(),
                key=lambda kv: kv[1].get("created_at", 0),
            )
            for old_id, _ in sorted_jobs[: len(_jobs) - _JOBS_MAX_SIZE + 1]:
                _jobs.pop(old_id, None)
        _jobs[job_id] = {
            "status": "queued",
            "created_at": time.time(),
            "filename": None,
            "html_path": None,
            "stats": None,
            "error": None,
            "error_code": None,
            "error_recovery": None,
        }
    return job_id


def _update_job(job_id: str, **kwargs: Any) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """获取 job, 顺便清理过期 (created_at 超 1h)."""
    with _jobs_lock:
        now = time.time()
        # 惰性 TTL 清理
        expired = [
            jid for jid, j in _jobs.items()
            if now - j.get("created_at", now) > _JOBS_TTL_SECONDS
        ]
        for jid in expired:
            _jobs.pop(jid, None)
        return _jobs.get(job_id)


# ============================================================
# Endpoints
# ============================================================
@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "version": __version__}


@app.post("/api/convert")
async def api_convert(
    file: UploadFile = File(...),
    output_dir: Optional[str] = Form(None),
    mineru_token: Optional[str] = Form(None),
    mode: Optional[str] = Form(None),
) -> Dict[str, Any]:
    """上传 PDF → 异步转 HTML → 返回 job_id。

    立即返回 (job_id + status='queued')。
    前端轮询 GET /api/jobs/{job_id} 直到 status='done' 或 'failed'。

    安全 (H-4, H-6):
    - 边读边累计字节数, 超过 MAX_UPLOAD_BYTES 立刻拒绝 (避免 OOM)
    - output_dir 必须落在白名单根之下 (避免任意目录写入)
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="请上传 .pdf 文件")

    # H-4: 上传大小限制 — 边读边计, 早退 (避免 2GB 文件全量驻内存)
    from .pipeline import MAX_PDF_SIZE_BYTES
    MAX_UPLOAD_BYTES = MAX_PDF_SIZE_BYTES  # 复用 pipeline 阈值 = 100MB

    # 读 config: 若调用方没传 mineru_token, 用 config 里的
    cfg = app_config.load()
    token = mineru_token if mineru_token else cfg.get("mineru_token")
    effective_mode = mode if mode else app_config.resolve_mode(cfg)

    # H-6: output_dir 路径白名单 — 解析后必须落进允许的根目录
    if output_dir:
        out_dir = Path(output_dir)
        if not _is_path_within_allowed(out_dir):
            raise HTTPException(
                status_code=400,
                detail=f"output_dir 不在白名单范围内: {out_dir}",
            )
    else:
        out_dir = default_output_dir()

    # 先把上传流写到临时文件 (避免 UploadFile.read() 阻塞 + 保留 PDF 原始字节)
    suffix = Path(file.filename).suffix or ".pdf"
    tmp_pdf = Path(tempfile.mkstemp(prefix="exam_upload_", suffix=suffix)[1])
    try:
        # H-4: chunked read + 累加字节数, 超过阈值立刻 unlink + 拒
        chunk_size = 1024 * 1024  # 1MB chunks
        total = 0
        with open(tmp_pdf, "wb") as f:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    f.close()
                    tmp_pdf.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 限制",
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        tmp_pdf.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"上传保存失败: {e}")

    job_id = _new_job()
    _update_job(job_id, status="processing", filename=file.filename)

    # 后台线程跑 convert_pdf
    def _run():
        try:
            result = convert_pdf(
                pdf_path=tmp_pdf,
                output_dir=out_dir,
                mode=effective_mode,
                mineru_token=token,
            )
            _update_job(
                job_id,
                status="done",
                html_path=result["html_path"],
                stats=result["stats"],
            )
            log.info("[server] job %s done: %s", job_id, result["html_path"])
        except PipelineError as e:
            # PipelineError 含 error_code + recovery_hint (M3-1)
            err = e.to_dict()
            _update_job(
                job_id,
                status="failed",
                error=err["message"],
                error_code=err["code"],
                error_recovery=err["recovery"],
            )
            log.warning("[server] job %s failed: [%s] %s", job_id, err["code"], e)
        except Exception as e:
            _update_job(
                job_id,
                status="failed",
                error=f"unexpected: {e}",
                error_code="UNKNOWN",
                error_recovery="retry_button",
            )
            log.exception("[server] job %s unexpected error", job_id)
        finally:
            tmp_pdf.unlink(missing_ok=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "processing", "filename": file.filename}


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str) -> Dict[str, Any]:
    """轮询 job 状态. status ∈ {'queued', 'processing', 'done', 'failed'}"""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


@app.post("/api/open-html")
def api_open_html(path: str) -> Dict[str, Any]:
    """调 OS 默认浏览器 / 文件管理器打开 HTML.

    安全 (H-5): path 必须落进白名单根 (inbox/archive/courseware/desktop/tmp),
    且后缀必须是 .html — 避免任意文件被外部进程用 OS 默认 app 打开。
    """
    p = Path(path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {path}")
    if p.suffix.lower() not in (".html", ".htm"):
        raise HTTPException(status_code=400, detail=f"非 HTML 文件: {p.name}")
    if not _is_path_within_allowed(p):
        raise HTTPException(
            status_code=403,
            detail=f"path 不在白名单范围内: {p}",
        )
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(p)])
        elif platform.system() == "Windows":
            # Windows: `start ""` 是 startfile 的 cmd 包装, 空串是 window title 占位
            subprocess.Popen(["cmd", "/c", "start", "", str(p)])
        else:  # Linux
            subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"open 失败: {e}")


@app.get("/api/config")
def api_get_config() -> Dict[str, Any]:
    return app_config.load()


@app.post("/api/config")
def api_post_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """更新 config.json (只持久化已知字段)."""
    cfg = app_config.load()
    for k in app_config.DEFAULTS:
        if k in payload:
            cfg[k] = payload[k]
    app_config.save(cfg)
    return cfg


# ============================================================
# 自动更新 (设计文档 §7)
# ============================================================
@app.get("/api/version")
def api_version() -> Dict[str, Any]:
    """读 version check 状态 (节流, 默认 24h 内不重 fetch).

    返回 status ∈ {'up_to_date', 'update_available', 'check_failed', 'throttled'}.
    """
    return check_update(force=False)


@app.post("/api/version/check")
def api_version_check() -> Dict[str, Any]:
    """手动按钮: 跳过节流, 强制检查."""
    return check_update(force=True)


# ============================================================
# M3-2: 启动恢复 (设计文档 §5.1 第 8 项)
# ============================================================
@app.get("/api/incomplete-uploads")
def api_incomplete_uploads(within_hours: int = 24) -> Dict[str, Any]:
    """扫描 inbox/archive 中最近 N 小时的 PDF 残留.

    返回 {uploads: [{filename, path, size, mtime, location}], count: N}.
    前端启动时调一次, 非空显示 "上次有未完成" banner.
    """
    uploads = scan_incomplete_uploads(within_hours=within_hours)
    return {"uploads": uploads, "count": len(uploads)}


@app.post("/api/incomplete-uploads/clear")
def api_clear_incomplete(payload: Dict[str, Any]) -> Dict[str, Any]:
    """清除指定残留 PDF (banner 的 "丢弃" 按钮).

    安全 (H-3): 传入的每个 path 必须落进白名单根 (inbox/archive/courseware/desktop/tmp),
    且后缀必须是 .pdf。避免 localhost 上其他进程利用此 endpoint 删除任意文件
    (e.g. {"paths": ["/Users/<u>/.ssh/id_rsa"]})。
    """
    paths = payload.get("paths", [])
    if not isinstance(paths, list):
        raise HTTPException(status_code=400, detail="paths must be list")
    validated: List[Path] = []
    for raw in paths:
        if not isinstance(raw, str):
            continue
        p = Path(raw)
        if p.suffix.lower() != ".pdf":
            log.warning("[server] clear_incomplete 拒绝非 .pdf: %s", p)
            continue
        if not _is_path_within_allowed(p):
            log.warning("[server] clear_incomplete 拒绝非白名单 path: %s", p)
            continue
        validated.append(p)
    deleted = clear_incomplete_uploads([str(x) for x in validated])
    return {"deleted": deleted}


# ============================================================
# 静态文件 (PyWebView 通过 FastAPI 这边 serve HTML/CSS/JS)
# ============================================================
_static_dir = gui_static_dir()
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


__all__ = ["app"]