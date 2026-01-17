from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
import io
import os
from typing import Optional, Dict, Any, List
import asyncio
import importlib
from dataclasses import dataclass
from collections import deque
import time

# Directory to look for local models and offline mode flag
MODELS_DIR = os.getenv('MODELS_DIR', os.path.join(os.getcwd(), 'models'))
MODELS_OFFLINE = os.getenv('MODELS_OFFLINE', '0').lower() in ('1', 'true', 'yes')
RESULTS_TTL_SECONDS = int(os.getenv('RESULTS_TTL_SECONDS', '120'))

origins = [
    # "http://localhost",          # 本地前端地址
    # "http://localhost:8080",     # 前端常用端口
    # "http://127.0.0.1:5500",     # 静态文件服务器端口
    # "https://your-production-domain.com",  # 生产环境域名
    "*"
]



# Simple in-memory cache of rembg sessions keyed by model name or model file path
SESSIONS: dict[str, object] = {}
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass
class QueueTask:
    id: int
    input_bytes: bytes
    model: Optional[str]
    requested_model: Optional[str]
    future: Optional[asyncio.Future]


TASK_QUEUE: asyncio.Queue[QueueTask] = asyncio.Queue()
PENDING_TASK_IDS = deque()
TASK_METADATA: Dict[int, Dict[str, Any]] = {}
TASK_RESULTS: Dict[int, bytes] = {}
CURRENT_TASK_ID: Optional[int] = None
_TASK_ID_COUNTER = 0
_TASK_ID_LOCK = asyncio.Lock()


async def get_session(model: Optional[str]):
    """Return a rembg session for the given model name or local model path.

    If model is None, returns None. If a session for the model already exists in
    cache, it is returned. Otherwise a new session is created in a threadpool
    (since model loading is CPU / IO bound) and cached.
    """
    if not model:
        return None

    # If model is a simple name, prefer a local file under ./models/{name}.onnx to avoid remote download
    # MODELS_DIR is defined at module level
    candidate_local = None
    if not os.path.isabs(model) and not os.path.exists(model):
        # look for ./models/{model}.onnx
        candidate = os.path.join(MODELS_DIR, f"{model}.onnx")
        if os.path.exists(candidate):
            candidate_local = candidate
            print(f"Found local model for '{model}': {candidate_local} - will use local file")

    # If model refers to an existing local file/path (or we found a local candidate), use its absolute path as cache key
    resolved_model = candidate_local or model
    # If offline mode is required but there's no local model file, fail early to avoid rembg downloading from GitHub
    if MODELS_OFFLINE and not os.path.exists(resolved_model):
        raise FileNotFoundError(f"Local model for '{model}' not found in {MODELS_DIR} and MODELS_OFFLINE is enabled")
    key = os.path.abspath(resolved_model) if os.path.exists(resolved_model) else resolved_model

    # fast path: already created
    if key in SESSIONS:
        return SESSIONS[key]

    # ensure a single creator per key using asyncio.Lock
    lock = _SESSION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[key] = lock

    await lock.acquire()
    try:
        # another coroutine may have created it while we waited
        if key in SESSIONS:
            return SESSIONS[key]

        def create():
            # lazy import rembg.new_session to avoid heavy imports on startup
            rembg = importlib.import_module('rembg')
            new_session = getattr(rembg, 'new_session')
            # print whether we're loading a local model path or a named model
            if os.path.exists(resolved_model):
                print(f"Loading rembg session from local model file: {resolved_model}")
            else:
                print(f"Loading rembg session for model name: {resolved_model}")
            # If resolved_model is a local file, pass it as model_path to new_session
            if os.path.exists(resolved_model):
                return new_session(model_path=resolved_model)
            # otherwise pass model name
            return new_session(resolved_model)

        session = await run_in_threadpool(create)
        SESSIONS[key] = session
        return session
    finally:
        lock.release()


app = FastAPI(title="Background Remover")


async def next_task_id() -> int:
    global _TASK_ID_COUNTER
    async with _TASK_ID_LOCK:
        _TASK_ID_COUNTER += 1
        return _TASK_ID_COUNTER


async def remove_background_bytes(input_bytes: bytes, model: Optional[str]) -> bytes:
    normalized = normalize_model_input(model) if model else None
    session = await get_session(normalized) if normalized else None

    def process():
        rembg = importlib.import_module('rembg')
        remove = getattr(rembg, 'remove')
        if session is not None:
            return remove(input_bytes, session=session)
        if normalized:
            if os.path.exists(normalized):
                model_arg = os.path.abspath(normalized)
            else:
                model_arg = normalized
            return remove(input_bytes, model_name=model_arg)
        return remove(input_bytes)

    return await run_in_threadpool(process)


async def task_worker():
    global CURRENT_TASK_ID
    while True:
        task = await TASK_QUEUE.get()
        CURRENT_TASK_ID = task.id
        meta = TASK_METADATA.get(task.id)
        if meta:
            meta["status"] = "running"
            meta["started_at"] = time.time()
        if PENDING_TASK_IDS and PENDING_TASK_IDS[0] == task.id:
            PENDING_TASK_IDS.popleft()
        try:
            output_bytes = await remove_background_bytes(task.input_bytes, task.model)
            if meta:
                meta["status"] = "done"
                meta["finished_at"] = time.time()
            TASK_RESULTS[task.id] = output_bytes
            if task.future is not None and not task.future.done():
                task.future.set_result(output_bytes)
        except Exception as exc:
            if meta:
                meta["status"] = "failed"
                meta["finished_at"] = time.time()
                meta["error"] = str(exc)
            if task.future is not None and not task.future.done():
                task.future.set_exception(exc)
        finally:
            CURRENT_TASK_ID = None
            TASK_QUEUE.task_done()


async def cleanup_results_worker():
    if RESULTS_TTL_SECONDS <= 0:
        return
    while True:
        now = time.time()
        expired_ids = []
        for task_id, meta in list(TASK_METADATA.items()):
            if meta.get("status") != "done":
                continue
            finished_at = meta.get("finished_at")
            if not finished_at:
                continue
            if now - finished_at > RESULTS_TTL_SECONDS:
                expired_ids.append(task_id)
        for task_id in expired_ids:
            TASK_RESULTS.pop(task_id, None)
        await asyncio.sleep(RESULTS_TTL_SECONDS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,        # 允许的源列表
    allow_credentials=True,       # 是否允许携带Cookie
    allow_methods=["*"],          # 允许所有HTTP方法（GET/POST/PUT/DELETE等）
    allow_headers=["*"],          # 允许所有请求头
)


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(task_worker())
    asyncio.create_task(cleanup_results_worker())


@app.get("/")
def read_root():
    return {"status": "ok", "message": "POST /remove-background with form field 'file'"}


@app.post("/remove-background")
async def remove_background(request: Request, file: UploadFile = File(...), model: Optional[str] = Form(None)):
    """接收 multipart/form-data 的文件并返回去除背景后的 PNG 图像流（带透明通道）。

    可选的表单字段/查询参数：
    - model: 指定 rembg 使用的模型名称或本地模型路径（例如 'u2net', 'u2net_human_seg' 等）。
      支持通过表单字段上传：-F "model=u2net" 或通过查询参数：/remove-background?model=u2net
      也可以使用别名，例如 model=light 或 model=fast 使用资源占用更小的模型
    """
    if not model:
        model = request.query_params.get("model")

    if not model:
        model = os.getenv("DEFAULT_MODEL", "u2net")

    model = resolve_model_choice(model)

    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Uploaded file must be an image")

    input_bytes = await file.read()

    try:
        task_id = await next_task_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        queue_task = QueueTask(
            id=task_id,
            input_bytes=input_bytes,
            model=model,
            requested_model=model,
            future=future,
        )
        TASK_METADATA[task_id] = {
            "id": task_id,
            "status": "pending",
            "requested_model": model,
            "created_at": time.time(),
        }
        PENDING_TASK_IDS.append(task_id)
        await TASK_QUEUE.put(queue_task)
        try:
            output_bytes = await future
        except FileNotFoundError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        return StreamingResponse(
            io.BytesIO(output_bytes),
            media_type="image/png",
            headers={"X-Task-ID": str(task_id)},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/queue/size")
def get_queue_size():
    pending = sum(1 for item in TASK_METADATA.values() if item.get("status") == "pending")
    running = sum(1 for item in TASK_METADATA.values() if item.get("status") == "running")
    return {"pending": pending, "running": running}


@app.get("/queue/tasks")
def get_queue_tasks():
    positions = {task_id: index for index, task_id in enumerate(PENDING_TASK_IDS)}
    tasks = []
    for task_id, meta in sorted(TASK_METADATA.items()):
        tasks.append(
            {
                "id": task_id,
                "status": meta.get("status"),
                "position": positions.get(task_id),
                "requested_model": meta.get("requested_model"),
                "created_at": meta.get("created_at"),
            }
        )
    return {"current_task_id": CURRENT_TASK_ID, "tasks": tasks}


@app.post("/tasks")
async def create_task(request: Request, file: UploadFile = File(...), model: Optional[str] = Form(None)):
    if not model:
        model = request.query_params.get("model")

    if not model:
        model = os.getenv("DEFAULT_MODEL", "u2net")

    model = resolve_model_choice(model)

    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Uploaded file must be an image")

    input_bytes = await file.read()

    task_id = await next_task_id()
    queue_task = QueueTask(
        id=task_id,
        input_bytes=input_bytes,
        model=model,
        requested_model=model,
        future=None,
    )
    TASK_METADATA[task_id] = {
        "id": task_id,
        "status": "pending",
        "requested_model": model,
        "created_at": time.time(),
    }
    PENDING_TASK_IDS.append(task_id)
    await TASK_QUEUE.put(queue_task)
    positions = {tid: index for index, tid in enumerate(PENDING_TASK_IDS)}
    position = positions.get(task_id)
    return {"id": task_id, "status": "pending", "position": position}


@app.post("/tasks/batch")
async def create_tasks_batch(request: Request, files: List[UploadFile] = File(...), model: Optional[str] = Form(None)):
    if not model:
        model = request.query_params.get("model")

    if not model:
        model = os.getenv("DEFAULT_MODEL", "u2net")

    model = resolve_model_choice(model)

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    for f in files:
        if not f.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="All uploaded files must be images")

    created_tasks = []
    for f in files:
        input_bytes = await f.read()
        task_id = await next_task_id()
        queue_task = QueueTask(
            id=task_id,
            input_bytes=input_bytes,
            model=model,
            requested_model=model,
            future=None,
        )
        TASK_METADATA[task_id] = {
            "id": task_id,
            "status": "pending",
            "requested_model": model,
            "created_at": time.time(),
            "filename": f.filename,
        }
        PENDING_TASK_IDS.append(task_id)
        await TASK_QUEUE.put(queue_task)
        created_tasks.append(task_id)

    positions = {tid: index for index, tid in enumerate(PENDING_TASK_IDS)}
    result = []
    for task_id in created_tasks:
        meta = TASK_METADATA.get(task_id, {})
        result.append(
            {
                "id": task_id,
                "status": meta.get("status"),
                "position": positions.get(task_id),
                "requested_model": meta.get("requested_model"),
                "filename": meta.get("filename"),
            }
        )
    return {"tasks": result}


@app.get("/tasks/{task_id}")
def get_task(task_id: int):
    meta = TASK_METADATA.get(task_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Task not found")
    positions = {tid: index for index, tid in enumerate(PENDING_TASK_IDS)}
    position = positions.get(task_id)
    return {
        "id": task_id,
        "status": meta.get("status"),
        "position": position,
        "requested_model": meta.get("requested_model"),
        "created_at": meta.get("created_at"),
        "finished_at": meta.get("finished_at"),
    }


@app.get("/tasks/{task_id}/image")
def get_task_image(task_id: int):
    meta = TASK_METADATA.get(task_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Task not found")
    status = meta.get("status")
    if status in ("pending", "running"):
        raise HTTPException(status_code=202, detail="Task not finished")
    if status == "failed":
        raise HTTPException(status_code=500, detail=meta.get("error") or "Task failed")
    output_bytes = TASK_RESULTS.get(task_id)
    if output_bytes is None:
        raise HTTPException(status_code=410, detail="Result no longer available")
    return StreamingResponse(io.BytesIO(output_bytes), media_type="image/png")


def normalize_model_input(model: Optional[str]) -> Optional[str]:
    """Normalize model input to either an absolute local path (if exists) or a stable model name.

    This ensures the cache key used by get_session is stable between requests even if callers
    pass different forms like './models/u2net.onnx', 'u2net', or '/home/www/models/u2net.onnx'.
    """
    if not model:
        return None
    m = os.path.expanduser(model)
    if os.path.isabs(m) and os.path.exists(m):
        return os.path.realpath(m)
    rel = os.path.join(os.getcwd(), m)
    if os.path.exists(rel):
        return os.path.realpath(rel)
    candidate = os.path.join(MODELS_DIR, f"{m}.onnx")
    if os.path.exists(candidate):
        return os.path.realpath(candidate)
    return m


def resolve_model_choice(model: Optional[str]) -> Optional[str]:
    if not model:
        return None
    name = model.strip().lower()
    if name in ("light", "fast", "small"):
        return "u2netp"
    if name in ("standard", "default", "high"):
        return "u2net"
    return model

if __name__ == "__main__":
    import uvicorn
    # 读取环境变量PORT，默认8000
    port = int(os.getenv("PORT", 8080))
    # 必须绑定0.0.0.0，否则云平台无法访问
    uvicorn.run(app, host="0.0.0.0", port=port)
