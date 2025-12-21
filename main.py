from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from rembg import remove, new_session
import io
import os
from typing import Optional
import asyncio

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


async def get_session(model: Optional[str]):
    """Return a rembg session for the given model name or local model path.

    If model is None, returns None. If a session for the model already exists in
    cache, it is returned. Otherwise a new session is created in a threadpool
    (since model loading is CPU / IO bound) and cached.
    """
    if not model:
        return None

    # If model refers to an existing local file/path, use its absolute path as cache key
    key = os.path.abspath(model) if os.path.exists(model) else model

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
            # new_session accepts a model name or a local model path
            print(f"Loading rembg model/session for '{model}'...")
            return new_session(model)

        session = await run_in_threadpool(create)
        SESSIONS[key] = session
        return session
    finally:
        lock.release()


app = FastAPI(title="Background Remover")

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,        # 允许的源列表
    allow_credentials=True,       # 是否允许携带Cookie
    allow_methods=["*"],          # 允许所有HTTP方法（GET/POST/PUT/DELETE等）
    allow_headers=["*"],          # 允许所有请求头
)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "POST /remove-background with form field 'file'"}


@app.post("/remove-background")
async def remove_background(request: Request, file: UploadFile = File(...), model: Optional[str] = Form(None)):
    """接收 multipart/form-data 的文件并返回去除背景后的 PNG 图像流（带透明通道）。

    可选的表单字段/查询参数：
    - model: 指定 rembg 使用的模型名称或本地模型路径（例如 'u2net', 'u2net_human_seg' 等）。
      支持通过表单字段上传：-F "model=u2net" 或通过查询参数：/remove-background?model=u2net
    """
    # 如果没有通过表单提供 model，尝试从查询参数读取；如果仍为 None 则使用默认模型（env DEFAULT_MODEL 或 'u2net'）
    if not model:
        model = request.query_params.get("model")

    if not model:
        model = os.getenv("DEFAULT_MODEL", "u2net")

    # 为模型创建或复用一个 session（默认也会创建一次并缓存），避免多次重复加载
    session = await get_session(model)

    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Uploaded file must be an image")

    input_bytes = await file.read()

    try:
        # rembg.remove 是 CPU 密集型操作，放到线程池中运行以免阻塞事件循环
        def process():
            # 如果我们已有一个 session（来自 new_session），则使用 session
            if session is not None:
                return remove(input_bytes, session=session)

            # 如果提供了 model，但没有创建 session（例如用户传了模型名而不需要 session），
            # 将按 rembg 要求传递 model_name（如果看起来像本地路径则传路径，否则传模型名）
            if model:
                # 如果是本地文件路径，传入绝对路径；否则传模型名给 rembg
                if os.path.exists(model):
                    model_arg = os.path.abspath(model)
                else:
                    model_arg = model
                return remove(input_bytes, model_name=model_arg)

            # 未指定模型：调用 remove 不传 session 或 model_name，避免重复创建会话
            return remove(input_bytes)

        output_bytes = await run_in_threadpool(process)

        # rembg 输出通常为 PNG with alpha channel
        return StreamingResponse(io.BytesIO(output_bytes), media_type="image/png")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # 读取环境变量PORT，默认8000
    port = int(os.getenv("PORT", 8080))
    # 必须绑定0.0.0.0，否则云平台无法访问
    uvicorn.run(app, host="0.0.0.0", port=port)