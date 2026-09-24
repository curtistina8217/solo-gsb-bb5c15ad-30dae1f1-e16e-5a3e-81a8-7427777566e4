# 电子琴发声区间 API

把电子琴演奏事件流(按键 / 松键 / 延音踏板)渲染为每个音的**实际发声起止毫秒区间**。
Python 3.12 + FastAPI + Uvicorn,容器启动即可调用。

## 一条命令启动

```bash
docker compose up --build
```

启动后(本机访问):

- API 地址:`http://localhost:8000`
- 渲染接口:`POST http://localhost:8000/render`
- 渲染 + 通道统计:`POST http://localhost:8000/render/stats`
- 健康检查:`GET http://localhost:8000/health`
- 交互式文档(Swagger UI):`http://localhost:8000/docs`
- OpenAPI Schema:`http://localhost:8000/openapi.json`

## 必要配置

- 容器内服务监听固定端口 **8000**;宿主机通过 Compose 的端口映射 `8000:8000` 访问。
- 如需改宿主机端口,改 `docker-compose.yml` 中的 `"8000:8000"`(左为主机端口)即可,容器侧无需改动。
- 无数据库、无环境变量、无外部依赖;`restart: unless-stopped` 保证异常退出自动重启。

## 接口约定

### `POST /render`

请求体:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `end_ms` | int, ≥ 0 | 结束时刻;仍在发声的音统一在此刻关闭 |
| `events` | array | 按 `time_ms` **非递减**排列的事件;同一时刻按**输入顺序**处理 |

每个事件:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `time_ms` | int, ≥ 0, ≤ `end_ms` | 事件时刻 |
| `channel` | int | 通道号(踏板状态按通道独立) |
| `type` | string | `note_on` / `note_off` / `pedal_down` / `pedal_up` |
| `pitch` | int | 音高;`note_on`、`note_off` 必填,踏板事件可不带 |

发声规则:

1. 每个 `note_on` 生成一条独立区间;同音高重复按键各自独立保留。
2. `note_off` 按 FIFO 配对同通道、同音高上**最早仍按住**的 `note_on`:
   - 踏板未踩下:该音在松键时刻立即截断;
   - 踏板踩着:不结束,记为该通道的**待释放**音。
3. `pedal_down` 踩下延音踏板;`pedal_up` 抬起踏板时,该通道所有待释放音在**同一时刻**同时截断。
4. `end_ms` 时刻,所有仍按住或被踏板延续的音统一关闭。
5. 返回的 `intervals` 按**起音事件顺序**排列,每项含 `channel`、`pitch`、`start_ms`、`end_ms`(实际起止毫秒)。

语义错误不会导致请求失败(HTTP 200),放在响应的 `errors` 中,无效事件不影响其他事件:

| `reason` | 含义 |
| --- | --- |
| `unpaired_note_off` | 松键找不到可配对的、仍按住的同音高 `note_on` |
| `pedal_already_down` | 踏板已踩下,重复 `pedal_down`(状态不变) |
| `pedal_already_up` | 踏板未踩下时收到 `pedal_up`(状态不变) |

`errors[].index` 是该事件在输入 `events` 数组中的下标(从 **0** 开始)。

请求结构非法(缺字段、`pitch` 缺失、时间为负、事件未按时间非递减排列、事件晚于 `end_ms`)返回 HTTP 422 及校验详情。

### `POST /render/stats`

请求体与 `POST /render` 完全相同(同一个 `RenderRequest`),响应在原有 `intervals`、`errors`
之外附带 `stats`:每个**出现过按键(`note_on`)**的通道一项,按 `channel` **升序**排列。
语义错误的事件不参与渲染,因此也不会影响统计。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `channel` | int | 通道号 |
| `total_duration_ms` | int | 该通道所有音区间时长之和(`end_ms - start_ms` 逐条累加) |
| `peak_polyphony` | int | 半开区间 `[start_ms, end_ms)` 内同时发声的最大音数;零长度音不参与 |
| `peak_at_ms` | int \| null | 首次达到 `peak_polyphony` 的时刻;通道内完全没有正时长音时为 `null`(此时峰值为 0) |
| `sustained_duration_ms` | int | 松键后仅靠延音踏板延续的时长之和:松键时刻 -> 抬踏板或 `end_ms` |

统计基于渲染结果计算,因此重复同音的 FIFO 配对、无效踏板切换等规则与 `/render` 完全一致。

### 请求示例

```bash
curl -s -X POST http://localhost:8000/render \
  -H 'Content-Type: application/json' \
  -d '{
    "end_ms": 1000,
    "events": [
      {"time_ms": 0,   "channel": 0, "type": "note_on",  "pitch": 60},
      {"time_ms": 100, "channel": 0, "type": "pedal_down"},
      {"time_ms": 200, "channel": 0, "type": "note_off", "pitch": 60},
      {"time_ms": 800, "channel": 0, "type": "pedal_up"}
    ]
  }'
```

松键发生在踩踏板期间,声音延续到抬踏板才结束,响应:

```json
{
  "intervals": [
    {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 800}
  ],
  "errors": []
}
```

未松键直接结束、松键多于按键的示例:

```json
{
  "end_ms": 500,
  "events": [
    {"time_ms": 0,  "channel": 0, "type": "note_on",  "pitch": 60},
    {"time_ms": 100,"channel": 0, "type": "note_off", "pitch": 60},
    {"time_ms": 200,"channel": 0, "type": "note_off", "pitch": 60}
  ]
}
```

```json
{
  "intervals": [
    {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 100}
  ],
  "errors": [
    {"index": 2, "reason": "unpaired_note_off"}
  ]
}
```

### `/render/stats` 请求示例

```bash
curl -s -X POST http://localhost:8000/render/stats \
  -H 'Content-Type: application/json' \
  -d '{
    "end_ms": 1000,
    "events": [
      {"time_ms": 0,   "channel": 0, "type": "note_on",  "pitch": 60},
      {"time_ms": 0,   "channel": 1, "type": "note_on",  "pitch": 64},
      {"time_ms": 100, "channel": 0, "type": "pedal_down"},
      {"time_ms": 200, "channel": 0, "type": "note_off", "pitch": 60},
      {"time_ms": 300, "channel": 1, "type": "note_off", "pitch": 64},
      {"time_ms": 800, "channel": 0, "type": "pedal_up"}
    ]
  }'
```

通道 0 的音在 200ms 松键后靠踏板延续到 800ms(延续 600ms);通道 1 没有踩踏板,
松键即结束。两项统计按通道升序返回:

```json
{
  "intervals": [
    {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 800},
    {"channel": 1, "pitch": 64, "start_ms": 0, "end_ms": 300}
  ],
  "errors": [],
  "stats": [
    {
      "channel": 0,
      "total_duration_ms": 800,
      "peak_polyphony": 1,
      "peak_at_ms": 0,
      "sustained_duration_ms": 600
    },
    {
      "channel": 1,
      "total_duration_ms": 300,
      "peak_polyphony": 1,
      "peak_at_ms": 0,
      "sustained_duration_ms": 0
    }
  ]
}
```

## 项目结构

```
app/
  main.py        # FastAPI 应用:/health、/render、/render/stats
  schemas.py     # 请求/响应模型与 422 校验
  renderer.py    # 纯标准库的发声区间渲染与通道统计核心
tests/
  test_renderer.py
Dockerfile
docker-compose.yml
requirements.txt
```

## 本地(不使用 Docker)运行与测试

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 核心算法测试仅依赖标准库
python3 -m unittest discover -s tests -v
```
