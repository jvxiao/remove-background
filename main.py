from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from rembg import remove, new_session
import io
import os
from typing import Optional

origins = [
    # "http://localhost",          # 本地前端地址
    # "http://localhost:8080",     # 前端常用端口
    # "http://127.0.0.1:5500",     # 静态文件服务器端口
    # "https://your-production-domain.com",  # 生产环境域名
    "*"
]



# Simple in-memory cache of rembg sessions keyed by model name or model file path
SESSIONS: dict[str, object] = {}


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
    if key in SESSIONS:
        return SESSIONS[key]

    # Create the session in a thread to avoid blocking the event loop
    def create():
        # new_session accepts a model name or a local model path
        print(f"Loading rembg model/session for '{model}'...")
        return new_session(model, model_path=model)

    session = await run_in_threadpool(create)
    SESSIONS[key] = session
    return session


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
async def remove_background(request: Request, file: UploadFile = File(...), model: str = Form('u2net')):
    """接收 multipart/form-data 的文件并返回去除背景后的 PNG 图像流（带透明通道）。

    可选的表单字段/查询参数：
    - model: 指定 rembg 使用的模型名称或本地模型路径（例如 'u2net', 'u2net_human_seg' 等）。
      支持通过表单字段上传：-F "model=u2net" 或通过查询参数：/remove-background?model=u2net
    """
    # 如果没有通过表单提供 model，尝试从查询参数读取
    print(model)
    if not model:
        model = request.query_params.get("model")

    # 为指定的模型创建或复用一个 session（如果提供了 model）
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
            # 否则将 model_name（如果提供）传递给 remove，最后回退到默认模型
            if model:
                return remove(input_bytes, model_name="./models/"+model+".onnx")
            return remove(input_bytes)

        output_bytes = await run_in_threadpool(process)

        return StreamingResponse(io.BytesIO(output_bytes), media_type="image/jpeg")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # 读取环境变量PORT，默认8000
    port = int(os.getenv("PORT", 8080))
    # 必须绑定0.0.0.0，否则云平台无法访问
    uvicorn.run(app, host="0.0.0.0", port=port)