# Background Remover API 文档

## 基础信息

- 基础 URL：`http://localhost:{PORT}`
  - 默认 `PORT=8080`，可通过环境变量修改
- 功能：图片去背景（rembg）+ 任务排队队列（串行执行）

### 环境变量

- `PORT`：服务端口，默认 `8080`
- `DEFAULT_MODEL`：默认模型名，默认 `"u2net"`
- `MODELS_DIR`：本地模型目录，默认 `./models`
- `MODELS_OFFLINE`：
  - `"1" / "true" / "yes"` 时启用离线模式
  - 启用后，如果找不到本地模型文件会直接报错，不会联网下载

### 模型选择规则

请求中的 `model` 参数（表单字段或者查询参数）遵循以下规则：

- 别名：
  - `light` / `fast` / `small` → 使用 `u2netp`（轻量版，资源消耗较小）
  - `standard` / `default` / `high` → 使用 `u2net`
- 直接模型名：
  - 例如：`u2net`, `u2netp`, `u2net_human_seg` 等 rembg 支持的模型
- 未提供时：
  - 优先使用环境变量 `DEFAULT_MODEL`
  - 否则默认 `u2net`

---

## 1. 健康检查

- 方法：`GET /`
- 说明：用于检查服务是否正常运行

### 请求

- 无参数

### 响应示例

```json
{
  "status": "ok",
  "message": "POST /remove-background with form field 'file'"
}
```

---

## 2. 同步模式：直接返回处理后的图片

- 方法：`POST /remove-background`
- 说明：
  - 请求会进入队列按顺序执行
  - 当前 HTTP 请求会一直等待直到处理完成
  - 直接返回去除背景后的 PNG 图片
  - 同时在响应头中附带任务 ID（`X-Task-ID`）

### 请求

- `Content-Type: multipart/form-data`
- 表单字段：
  - `file`（必填）：图片文件
  - `model`（可选）：模型名或别名
- 也可以使用查询参数传递 `model`：
  - 示例：`/remove-background?model=light`

### 成功响应

- 状态码：`200 OK`
- 响应头：
  - `Content-Type: image/png`
  - `X-Task-ID: <数字>`（当前任务 ID）
- 响应体：PNG 图片二进制数据（已去除背景）

### 错误响应

- `400 Bad Request`：上传的不是图片

  ```json
  {"detail": "Uploaded file must be an image"}
  ```

- `503 Service Unavailable`：在离线模式下找不到本地模型文件

  ```json
  {"detail": "Local model for 'u2net' not found in ..."}
  ```

- `500 Internal Server Error`：其他内部错误

---

## 3. 队列概览接口

同步模式和异步模式的任务都共享同一个队列，这两个接口对所有任务通用。

### 3.1 获取队列大小

- 方法：`GET /queue/size`
- 说明：返回当前排队中的任务数量和正在执行的任务数量

#### 响应示例

```json
{
  "pending": 3,
  "running": 1
}
```

- `pending`：排队中的任务数量
- `running`：正在执行的任务数量（当前实现是单 worker，一般为 0 或 1）

### 3.2 获取队列任务详情

- 方法：`GET /queue/tasks`
- 说明：查看所有任务的状态和排队位置

#### 响应示例

```json
{
  "current_task_id": 5,
  "tasks": [
    {
      "id": 3,
      "status": "done",
      "position": null,
      "requested_model": "u2net",
      "created_at": 1768583600.123456
    },
    {
      "id": 4,
      "status": "pending",
      "position": 0,
      "requested_model": "light",
      "created_at": 1768583610.654321
    }
  ]
}
```

字段说明：

- `current_task_id`：当前正在执行的任务 ID（没有时为 `null`）
- `tasks`：任务列表，每个任务包含：
  - `id`：任务 ID
  - `status`：`"pending" | "running" | "done" | "failed"`
  - `position`：
    - 对 `pending` 任务：为队列中的位置（`0` 表示下一个执行）
    - 对其他状态：为 `null`
  - `requested_model`：该任务使用的模型或别名
  - `created_at`：任务创建时间（Unix 时间戳，秒）

---

## 4. 异步模式：任务提交 + 状态查询 + 结果获取

异步模式提供三个核心接口：

1. `POST /tasks`：提交任务，仅返回任务信息，不返回图片
2. `GET /tasks/{task_id}`：查询单个任务的状态及排队位置
3. `GET /tasks/{task_id}/image`：在任务完成后获取结果图片

所有任务（同步和异步）共享同一个任务队列和任务 ID 计数器。

### 4.1 提交异步任务

- 方法：`POST /tasks`
- 说明：
  - 将图片放入队列排队
  - 立即返回任务 ID 和当前的排队位置
  - 不返回处理后的图片

#### 请求

- `Content-Type: multipart/form-data`
- 表单字段：
  - `file`（必填）：图片文件
  - `model`（可选）：模型名或别名
- 也可通过查询参数传递 `model`：
  - 示例：`/tasks?model=light`

#### 成功响应示例

```json
{
  "id": 12,
  "status": "pending",
  "position": 3
}
```

- `id`：任务 ID
- `status`：任务当前状态（创建时为 `"pending"`）
- `position`：当前在队列中的位置（`0` 表示队首）

### 4.2 查询单个任务状态

- 方法：`GET /tasks/{task_id}`

#### 路径参数

- `task_id`：任务 ID（整数）

#### 成功响应示例

```json
{
  "id": 12,
  "status": "pending",
  "position": 2,
  "requested_model": "u2netp",
  "created_at": 1768583610.654321,
  "finished_at": null
}
```

字段说明：

- `status`：
  - `"pending"`：排队中
  - `"running"`：正在执行
  - `"done"`：已完成
  - `"failed"`：执行失败
- `position`：
  - 对 `pending` 任务：队列位置（0、1、2…）
  - 对其他状态：为 `null`
- `requested_model`：任务使用的模型
- `created_at`：任务创建时间
- `finished_at`：任务完成时间（完成后才有值）

#### 错误响应

- `404 Not Found`：任务不存在

```json
{"detail": "Task not found"}
```

### 4.3 获取任务结果图片

- 方法：`GET /tasks/{task_id}/image`

#### 逻辑说明

- 任务未完成：返回 `202 Accepted`
- 任务失败：返回 `500 Internal Server Error`
- 任务完成：返回 PNG 图片流
- 任务不存在：返回 `404 Not Found`
- 结果不可用：返回 `410 Gone`
  - 默认情况下，任务完成约 120 秒后，服务会自动清理内存中的图片结果
  - 可通过环境变量 `RESULTS_TTL_SECONDS` 调整保留时间（单位秒，设置为 `0` 或负数表示不自动清理）

#### 可能响应

1. 任务未完成：

   ```json
   {"detail": "Task not finished"}
   ```

2. 任务失败：

   ```json
   {"detail": "错误信息"}
   ```

3. 成功返回图片：

   - 状态码：`200 OK`
   - 响应头：`Content-Type: image/png`
   - 响应体：PNG 图片二进制数据

4. 任务不存在：

   ```json
   {"detail": "Task not found"}
   ```

5. 结果不可用：

   ```json
   {"detail": "Result no longer available"}
   ```

---

## 5. 批量处理

批量处理目前通过异步模式支持：一次请求提交多张图片，每张图片对应一个任务。

### 5.1 提交批量异步任务

- 方法：`POST /tasks/batch`
- 说明：
  - 在一次请求中上传多张图片
  - 为每张图片创建一个独立的任务
  - 立即返回该批次所有任务的 ID、状态和队列位置

#### 请求

- `Content-Type: multipart/form-data`
- 表单字段：
  - `files`（必填，多文件）：多张图片文件
  - `model`（可选）：模型名或别名（对该批次所有图片生效）
- 也可通过查询参数传递 `model`：
  - 示例：`/tasks/batch?model=light`

所有上传的文件必须是图片类型（`Content-Type` 以 `image/` 开头），否则会返回 400。

#### 成功响应示例

```json
{
  "tasks": [
    {
      "id": 20,
      "status": "pending",
      "position": 0,
      "requested_model": "u2netp",
      "filename": "a.png"
    },
    {
      "id": 21,
      "status": "pending",
      "position": 1,
      "requested_model": "u2netp",
      "filename": "b.jpg"
    }
  ]
}
```

字段说明：

- `tasks`：本次批量请求创建的任务列表
  - `id`：任务 ID
  - `status`：当前状态（创建时为 `"pending"`）
  - `position`：当前在队列中的位置（`0` 表示队首）
  - `requested_model`：任务使用的模型（批量请求中相同）
  - `filename`：原始文件名

> 批量任务创建后，可以复用单任务查询接口：
> - `GET /tasks/{task_id}` 查询某一张图片的任务状态和排队位置
> - `GET /tasks/{task_id}/image` 在任务完成后获取该图片的处理结果

---

## 6. 同步模式 vs 异步模式

- 同步模式（简单）：
  - 接口：`POST /remove-background`
  - 特点：一次请求完成整个处理流程，直接返回图片
  - 适合：后端服务调用、简单脚本使用

- 异步模式（可控）：
  - 接口组合：
    - `POST /tasks` → 创建单个任务，立即得到 `id` 和排队位置
    - `POST /tasks/batch` → 一次创建多个任务（批量处理）
    - `GET /tasks/{id}` → 轮询任务状态和排队情况
    - `GET /tasks/{id}/image` → 任务完成后获取结果图片
  - 适合：需要在前端展示排队进度、支持批量处理或长时间任务的场景
