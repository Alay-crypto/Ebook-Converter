"""
电子书格式转换平台 —— FastAPI 后端服务
=====================================
核心依赖：
  pip install fastapi uvicorn[standard] python-multipart aiofiles

系统依赖（需提前安装）：
  macOS:   brew install calibre
  Ubuntu:  sudo apt install calibre
  验证:    ebook-convert --version

启动命令：
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import uuid
import shutil
import logging
import subprocess
from pathlib import Path

import aiofiles
from fastapi import (
    FastAPI, File, Form, UploadFile,
    BackgroundTasks, HTTPException
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# ─────────────────────────────────────────────────────────────
# 日志配置：统一输出带时间戳的结构化日志，便于生产环境排查问题
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ebook-converter")


# ─────────────────────────────────────────────────────────────
# 全局常量
# ─────────────────────────────────────────────────────────────

# 所有临时工作目录的根目录，服务启动时自动创建
TEMP_ROOT = Path("./tmp_workspaces")
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

# 允许上传的源文件后缀（小写）
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".docx", ".doc"}

# 允许的目标转换格式
ALLOWED_TARGET_FORMATS = {"epub", "mobi", "azw3"}

# ebook-convert 工具路径
# 若 Calibre 安装在非标准位置，可在此处指定绝对路径，例如：
#   /opt/calibre/ebook-convert  或  C:\Program Files\Calibre2\ebook-convert.exe
EBOOK_CONVERT_BIN = os.environ.get("EBOOK_CONVERT_BIN", r"C:\Program Files\Calibre2\ebook-convert.exe")

# 单次转换最大允许耗时（秒），防止超大文件或异常进程长期占用资源
CONVERT_TIMEOUT_SECONDS = int(os.environ.get("CONVERT_TIMEOUT", "120"))


# ─────────────────────────────────────────────────────────────
# FastAPI 应用实例
# ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="电子书格式转换 API",
    description="基于 Calibre(ebook-convert) 的电子书格式转换服务，支持 PDF/DOCX → EPUB/MOBI/AZW3",
    version="1.0.0",
    docs_url="/docs",       # Swagger UI
    redoc_url="/redoc",     # ReDoc UI
)


# ─────────────────────────────────────────────────────────────
# CORS 跨域配置
# 前后端分离开发时，浏览器会发送预检请求（OPTIONS），必须正确响应。
# 生产环境建议将 allow_origins 改为精确的前端域名，提高安全性。
# ─────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",    # React/Vite 开发服务器
        "http://localhost:5173",    # Vite 默认端口
        "http://localhost:8080",    # Vue CLI 默认端口
        "http://127.0.0.1:5500",   # VSCode Live Server
        "*",                        # 开发阶段可放开，生产务必收紧
    ],
    allow_credentials=True,
    allow_methods=["*"],            # 允许所有 HTTP 方法（GET/POST/OPTIONS 等）
    allow_headers=["*"],            # 允许所有请求头
)


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def cleanup_workspace(workspace: Path) -> None:
    """
    后台清理任务：删除整个临时工作目录及其所有内容。
    由 BackgroundTasks 在 FileResponse 流式传输完成后自动调用，
    确保磁盘不积压临时文件，实现"零垃圾"服务。

    Args:
        workspace: 需要删除的临时目录 Path 对象
    """
    try:
        if workspace.exists():
            shutil.rmtree(workspace)
            logger.info(f"✅ 已清理临时目录: {workspace}")
    except Exception as e:
        # 清理失败不应影响用户体验，仅记录告警日志
        logger.warning(f"⚠️  临时目录清理失败: {workspace} — {e}")


def validate_upload_extension(filename: str) -> str:
    """
    校验上传文件的后缀名是否在白名单内，并返回规范化的小写后缀。

    Args:
        filename: 原始文件名（含后缀）

    Returns:
        小写后缀字符串，如 '.pdf'

    Raises:
        HTTPException 400: 后缀不在允许列表中
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "不支持的文件类型",
                "received": suffix or "（无后缀）",
                "allowed": list(ALLOWED_UPLOAD_EXTENSIONS),
            },
        )
    return suffix


def validate_target_format(target_format: str) -> str:
    """
    校验目标格式是否在允许列表内，并统一转为小写。

    Args:
        target_format: 前端传入的目标格式字符串

    Returns:
        小写格式字符串，如 'epub'

    Raises:
        HTTPException 400: 格式不在允许列表中
    """
    fmt = target_format.strip().lower()
    if fmt not in ALLOWED_TARGET_FORMATS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "不支持的目标格式",
                "received": fmt,
                "allowed": list(ALLOWED_TARGET_FORMATS),
            },
        )
    return fmt


# ─────────────────────────────────────────────────────────────
# 健康检查接口
# ─────────────────────────────────────────────────────────────

@app.get("/health", tags=["系统"])
async def health_check():
    """
    服务健康检查端点，同时探测 ebook-convert 是否可用。
    可用于 Docker HEALTHCHECK 或 k8s liveness probe。
    """
    calibre_status = "unavailable"
    calibre_version = None

    try:
        # 用 --version 探测 Calibre 是否安装且可执行
        result = subprocess.run(
            [EBOOK_CONVERT_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            calibre_status = "ok"
            # 取第一行版本信息
            calibre_version = result.stdout.strip().splitlines()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return {
        "status": "ok",
        "service": "ebook-converter",
        "calibre": {
            "status": calibre_status,
            "version": calibre_version,
            "bin": EBOOK_CONVERT_BIN,
        },
    }


# ─────────────────────────────────────────────────────────────
# 核心转换接口
# ─────────────────────────────────────────────────────────────

@app.post(
    "/api/convert",
    tags=["转换"],
    summary="上传文件并转换为指定电子书格式",
    response_description="转换后的电子书文件（二进制流下载）",
)
async def convert_ebook(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="源文件，支持 .pdf / .docx / .doc"),
    target_format: str = Form(..., description="目标格式：epub | mobi | azw3"),
):
    """
    电子书格式转换主接口。

    处理流程：
    1. 参数校验（文件后缀 + 目标格式）
    2. 创建 UUID 隔离的临时工作目录
    3. 异步写入上传文件到磁盘
    4. 调用 ebook-convert 执行格式转换
    5. 通过 FileResponse 流式返回下载
    6. 注册 BackgroundTask 在传输完成后自动清理临时文件
    """

    # ── Step 1: 参数校验 ────────────────────────────────────
    original_filename = file.filename or "upload"
    source_ext = validate_upload_extension(original_filename)
    fmt = validate_target_format(target_format)

    logger.info(
        f"📥 收到转换请求 | 文件: {original_filename} | 目标格式: {fmt.upper()}"
    )

    # ── Step 2: 创建 UUID 隔离的临时工作目录 ────────────────
    # 每个请求拥有独立目录，完全避免并发请求之间的文件污染
    workspace_id = uuid.uuid4().hex          # 32位随机十六进制字符串，唯一且无碰撞
    workspace = TEMP_ROOT / workspace_id
    workspace.mkdir(parents=True, exist_ok=True)

    # 构造输入/输出文件的完整路径
    # 保留原始文件名（去掉后缀后拼接），提升可读性
    stem = Path(original_filename).stem      # 文件名（不含后缀），如 "my-novel"
    input_path  = workspace / f"{stem}{source_ext}"
    output_path = workspace / f"{stem}.{fmt}"

    logger.info(f"📁 工作目录: {workspace}")
    logger.info(f"   ├─ 输入: {input_path.name}")
    logger.info(f"   └─ 输出: {output_path.name}")

    # ── Step 3: 异步写入上传文件 ────────────────────────────
    # 使用 aiofiles 进行非阻塞 I/O，避免在事件循环中阻塞线程。
    # 分块读取（chunk_size=1MB）防止大文件一次性载入内存导致 OOM。
    try:
        async with aiofiles.open(input_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):   # 每次读取 1 MB
                await f.write(chunk)
        logger.info(f"💾 文件写入完成: {input_path} ({input_path.stat().st_size / 1024:.1f} KB)")
    except OSError as e:
        # 磁盘写入失败（磁盘满、权限问题等）
        cleanup_workspace(workspace)
        logger.error(f"❌ 文件写入失败: {e}")
        raise HTTPException(
            status_code=500,
            detail={"error": "文件保存失败，请联系管理员", "detail": str(e)},
        )
    finally:
        # 确保释放上传流资源
        await file.close()

    # ── Step 4: 调用 ebook-convert 执行格式转换 ─────────────
    #
    # 命令格式：ebook-convert <input_file> <output_file> [options]
    # Calibre 会根据输入/输出文件的后缀自动选择转换插件，无需手动指定。
    #
    # 为什么用 subprocess 而不是 asyncio.create_subprocess_exec？
    # - subprocess.run 会阻塞当前线程。
    # - 在 FastAPI 中，可通过 run_in_executor 包装为异步，但对于
    #   CPU 密集型的 Calibre 转换，线程池隔离本身就是合理策略。
    # - 此处为简洁起见使用同步 subprocess，生产环境可升级为
    #   asyncio.create_subprocess_exec 以完全不阻塞事件循环。
    #
    cmd = [
        EBOOK_CONVERT_BIN,
        str(input_path),
        str(output_path),
        # 可根据需求追加 Calibre 选项，例如：
        # "--output-profile", "kindle_oasis",  # 针对特定 Kindle 机型优化
        # "--chapter-mark", "pagebreak",        # 章节分隔方式
        # "--pretty-print",                      # 格式化 HTML 输出（调试用）
    ]

    logger.info(f"🔧 执行转换命令: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,    # 同时捕获 stdout 和 stderr，不显示到终端
            text=True,              # 将字节输出解码为字符串（UTF-8）
            timeout=CONVERT_TIMEOUT_SECONDS,  # 超时保护，防止进程永久挂起
            check=False,            # 不自动抛出异常，由我们手动检查 returncode
        )

        logger.info(f"   ├─ 退出码: {result.returncode}")
        if result.stdout:
            # Calibre 会输出详细的转换日志，记录前500字符即可
            logger.info(f"   ├─ stdout: {result.stdout[:500]}")
        if result.stderr:
            logger.warning(f"   └─ stderr: {result.stderr[:500]}")

        # returncode != 0 代表 Calibre 转换失败
        if result.returncode != 0:
            cleanup_workspace(workspace)  # 立即清理，失败不应保留临时文件
            error_msg = result.stderr.strip() or result.stdout.strip() or "Calibre 转换失败，原因未知"
            logger.error(f"❌ 转换失败 (returncode={result.returncode}): {error_msg[:300]}")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "电子书转换失败",
                    "reason": error_msg[:500],   # 截断，避免泄露过多系统信息
                    "hint": "请检查文件内容是否损坏，或联系管理员查看服务器日志",
                },
            )

        # 验证输出文件确实存在且不为空（防御性检查）
        if not output_path.exists() or output_path.stat().st_size == 0:
            cleanup_workspace(workspace)
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "转换完成但输出文件缺失",
                    "hint": "可能是格式兼容性问题，请尝试其他目标格式",
                },
            )

    except subprocess.TimeoutExpired:
        # 超时：强制终止子进程，清理资源
        cleanup_workspace(workspace)
        logger.error(f"❌ 转换超时（>{CONVERT_TIMEOUT_SECONDS}s），已终止进程")
        raise HTTPException(
            status_code=504,
            detail={
                "error": f"转换超时（超过 {CONVERT_TIMEOUT_SECONDS} 秒）",
                "hint": "文件可能过大或内容复杂，请尝试精简文件后重试",
            },
        )
    except FileNotFoundError:
        # ebook-convert 二进制不存在
        cleanup_workspace(workspace)
        logger.critical(f"❌ 找不到 ebook-convert，请确认 Calibre 已正确安装！路径: {EBOOK_CONVERT_BIN}")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "转换服务不可用",
                "hint": "服务器未安装 Calibre，请联系管理员",
            },
        )

    # ── Step 5: 返回文件 + 注册后台清理任务 ─────────────────
    #
    # FileResponse 会将文件以流式方式发送给客户端。
    # BackgroundTasks 中注册的函数会在响应体完整发送后才执行，
    # 因此文件在传输期间不会被删除，传输完成后自动清理。
    #
    output_filename = output_path.name   # 下载时显示给用户的文件名
    output_size_kb  = output_path.stat().st_size / 1024

    logger.info(
        f"✅ 转换成功 | 输出: {output_filename} | 大小: {output_size_kb:.1f} KB | "
        f"工作区 {workspace_id} 将在传输后自动清理"
    )

    # 注册后台任务：等文件传完再删目录
    background_tasks.add_task(cleanup_workspace, workspace)

    # MIME 类型映射，确保浏览器正确识别下载文件
    media_type_map = {
        "epub": "application/epub+zip",
        "mobi": "application/x-mobipocket-ebook",
        "azw3": "application/vnd.amazon.mobi8-ebook",
    }

    return FileResponse(
        path=str(output_path),
        media_type=media_type_map.get(fmt, "application/octet-stream"),
        filename=output_filename,             # Content-Disposition: attachment; filename=...
        background=background_tasks,          # 绑定后台任务到此响应
    )


# ─────────────────────────────────────────────────────────────
# 全局异常兜底处理器
# 防止未捕获的异常将 Python traceback 暴露给前端
# ─────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception(f"💥 未捕获的全局异常: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "服务器内部错误",
            "hint": "请稍后重试，或联系管理员查看服务器日志",
        },
    )


# ─────────────────────────────────────────────────────────────
# 直接运行入口（开发调试用）
# 生产环境请使用 uvicorn 命令启动
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,       # 代码变更自动热重载，仅开发环境使用
        log_level="info",
    )
