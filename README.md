# 水文监测与洪峰事件识别

接收监测站水位观测批次，识别"水位达到洪水阈值起、回落到恢复阈值止"的洪峰事件。
仅使用 **Python 3.11 标准库 + SQLite**，无前端、无账号系统、无外部服务依赖。

- `app/`：业务实现（HTTP / 持久化 / 事件计算按职责拆分）
- `tests/`：可直接运行的 unittest（34 个用例）
- `fixtures/observations.json`：合成的监测站与观测样例
- `Dockerfile` / `run.sh`：容器与本地一键启动

## 快速启动

需要 Python 3.11（仅标准库，无需 pip 安装任何依赖）。

```bash
# 方式一：一键脚本（SQLite 文件自动创建在 ./data/hydro.db）
./run.sh                 # 默认 0.0.0.0:8000
./run.sh 0.0.0.0 9000    # 自定义端口

# 方式二：直接运行模块
python3 -m app.server --host 0.0.0.0 --port 8000 --db data/hydro.db

# 方式三：Docker
docker build -t hydro-monitor .
docker run --rm -p 8000:8000 -v "$PWD/data:/app/data" hydro-monitor
```

可用环境变量：`HOST`、`PORT`、`DB_PATH`。数据、批次去重信息和事件均持久化在
SQLite 文件中，服务重启后仍可查询并继续接收数据。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 接口

所有请求/响应均为 JSON。校验失败返回 4xx JSON（`{"error": ..., "message": ...}`），
不会把内部异常当成功响应。

### 1. 健康检查

```bash
curl http://localhost:8000/health
# 200 {"status": "ok"}
```

### 2. 创建监测站

`POST /stations`，字段：`id`、`name`、`flood_level`（洪水阈值）、
`recovery_level`（恢复阈值）。两个阈值必须是有限数，且 `flood_level > recovery_level`；
站号唯一，重复创建返回 409。

```bash
curl -X POST http://localhost:8000/stations \
  -H 'Content-Type: application/json' \
  -d '{"id":"river-a","name":"合成监测站 A","flood_level":10.0,"recovery_level":8.0}'
# 201 {"id":"river-a","name":"合成监测站 A","flood_level":10,"recovery_level":8}
```

### 3. 提交观测批次

`POST /observations`，请求体：`{"batch_id": "...", "readings": [...]}`，
每条观测含 `station_id`、`observed_at`、`level`：

- `observed_at` 必须是**带时区**的 ISO 8601（如 `2026-09-01T08:00:00+08:00` 或
  `...Z`），内部统一按 UTC 比较和输出；
- `level` 必须是有限数，不接受布尔值、NaN、Infinity；
- 站点必须存在，否则整批失败（404）。

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "example-001",
    "readings": [
      {"station_id":"river-a","observed_at":"2026-09-01T08:00:00+08:00","level":7.0},
      {"station_id":"river-a","observed_at":"2026-09-01T08:10:00+08:00","level":10.0},
      {"station_id":"river-a","observed_at":"2026-09-01T08:20:00+08:00","level":12.0},
      {"station_id":"river-a","observed_at":"2026-09-01T08:30:00+08:00","level":9.0},
      {"station_id":"river-a","observed_at":"2026-09-01T08:40:00+08:00","level":8.0}
    ]
  }'
```

响应包含 `received` / `inserted` / `duplicates` 计数以及受影响站点重算后的事件。

**批次语义：**

- **整批原子**：任一条非法、站点不存在或与已存在观测（同站同一 UTC 时刻）水位
  冲突，整批回滚——不留下任何新增观测，也不留下部分重算的事件。
- **幂等重试**：相同 `batch_id` + 相同内容（与观测顺序、时区表示无关）重试，
  返回首次成功的原结果；相同 `batch_id` 但内容不同返回 **409**。
- **重复观测**：相同站点、等价时区表示的同一时刻、相同水位视为重复，不重复计数；
  同批内的重复同样跳过，同批内同刻不同水位直接 400 拒绝。
- **乱序/迟到**：允许任意到达顺序，入库后重算受影响站点的完整事件，结果与把全部
  观测一次性按时间顺序导入完全一致。

### 4. 查询站点事件

`GET /stations/{id}/events`，按开始时间升序返回事件；未知站点返回 404。

```bash
curl http://localhost:8000/stations/river-a/events
```

```json
{
  "station_id": "river-a",
  "events": [
    {
      "start_at": "2026-09-01T00:10:00+00:00",
      "end_at":   "2026-09-01T00:40:00+00:00",
      "peak_at":  "2026-09-01T00:20:00+00:00",
      "peak_level": 12,
      "sample_count": 4,
      "status": "closed"
    }
  ]
}
```

未结束的事件 `status` 为 `open`、`end_at` 为 `null`。

## 事件识别规则

同一站点的观测按时间排序后：

1. 水位首次 **≥ flood_level** 时开启事件；
2. 事件期间每条观测都计入样本；回落到 flood_level 以下但仍高于 recovery_level
   时事件**继续，不拆分**；
3. 水位首次 **≤ recovery_level** 时事件结束，该条结束观测计入事件；
4. 结束后再次 ≥ flood_level 才开启新事件；阈值之间的普通观测不构成事件；
5. 峰值相同取最早观测时间；全部观测处理完仍未回落则为 open 事件。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `app/server.py` | HTTP 路由与 JSON 响应（标准库 `http.server`） |
| `app/service.py` | 业务编排：建站、批次原子入库、去重/冲突、事件重算、查询 |
| `app/database.py` | SQLite 连接、表结构、事务（WAL + 写锁串行化） |
| `app/events.py` | 事件状态机（纯函数，不依赖数据库） |
| `app/validation.py` | 水位/时间戳等输入校验 |
| `app/errors.py` | 业务异常 → 4xx 状态码映射 |
| `tests/` | 状态机、服务层（回滚/乱序/时区/重启/并发）、HTTP 端到端测试 |
