# 电子琴发声区间 API

把电子琴演奏事件流(按键 / 松键 / 普通延音踏板 / 选择性延音踏板)渲染为每个音的**实际发声起止毫秒区间**。
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
| `channel` | int | 通道号(两类踏板状态均按通道独立) |
| `type` | string | `note_on` / `note_off` / `pedal_down` / `pedal_up` / `sostenuto_down` / `sostenuto_up` |
| `pitch` | int | 音高;`note_on`、`note_off` 必填,两类踏板事件可不带 |

发声规则:

1. 每个 `note_on` 生成一条独立区间;同音高重复按键各自独立保留。
2. `note_off` 按 FIFO 配对同通道、同音高上**最早仍按住**的 `note_on`;松键当下若有踏板保持它则继续发声,否则在松键时刻立即截断。
3. **普通延音踏板**(`pedal_down` / `pedal_up`):
   - 踩着普通延音踏板松键的音记为**待释放**;`pedal_up` 时,不再被任何踏板保持的待释放音在**同一时刻**同时截断。
   - `pedal_down` 踩下时,该通道仍在发声的待释放音(例如此前仅靠选择性踏板延续的音)也一并由它保持。
4. **选择性延音踏板**(`sostenuto_down` / `sostenuto_up`):
   - `sostenuto_down` 只锁定该通道**当下仍按住**的各次 `note_on`(含同音高重复按键中每一个仍按住的音);踩下之后新按的音**不被锁定**。
   - 被锁定的音松键后继续发声(待释放),直到选择性踏板抬起;锁定期间始终没松键的音,抬踏板不影响(键还按着)。
   - 重复 `sostenuto_down` 无效,**不会**重新锁定第一次踩下后新按的音。
5. **两块踏板同时保持时**:被两块踏板共同保持的音,任一踏板抬起时只要**另一踏板仍保持**就不截断,两者都不再保持时才结束。
6. 同一时刻的事件按**输入顺序**处理;重复同音的松键仍按 FIFO 配对最早仍按住的 `note_on`。
7. `end_ms` 时刻,所有仍按住或被任一踏板延续的音统一关闭。
8. 返回的 `intervals` 按**起音事件顺序**排列,每项含 `channel`、`pitch`、`start_ms`、`end_ms`(实际起止毫秒)。

语义错误不会导致请求失败(HTTP 200),放在响应的 `errors` 中,无效事件不影响其他事件:

| `reason` | 含义 |
| --- | --- |
| `unpaired_note_off` | 松键找不到可配对的、仍按住的同音高 `note_on` |
| `pedal_already_down` | 普通延音踏板已踩下,重复 `pedal_down`(状态不变) |
| `pedal_already_up` | 普通延音踏板未踩下时收到 `pedal_up`(状态不变) |
| `sostenuto_already_down` | 选择性踏板已踩下,重复 `sostenuto_down`(状态不变,也不重新锁定) |
| `sostenuto_already_up` | 选择性踏板未踩下时收到 `sostenuto_up`(状态不变) |

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
| `sustained_duration_ms` | int | 松键后靠**任一踏板**(普通延音 / 选择性延音)延续的时长之和:松键时刻 -> 最后一块保持它的踏板抬起或 `end_ms`;被两块踏板共同保持的时段只计一次 |

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

#### 选择性延音踏板示例

`sostenuto_down` 只锁定踩下瞬间仍按住的音;踩下之后新按的音不被锁定:

```json
{
  "end_ms": 1000,
  "events": [
    {"time_ms": 0,   "channel": 0, "type": "note_on",  "pitch": 60},
    {"time_ms": 100, "channel": 0, "type": "sostenuto_down"},
    {"time_ms": 150, "channel": 0, "type": "note_on",  "pitch": 64},
    {"time_ms": 200, "channel": 0, "type": "note_off", "pitch": 60},
    {"time_ms": 250, "channel": 0, "type": "note_off", "pitch": 64},
    {"time_ms": 400, "channel": 0, "type": "sostenuto_up"}
  ]
}
```

音高 60 在踩踏板时仍按住,被锁定,松键后延续到 400ms;音高 64 是踩下之后新按的音,
未被锁定,250ms 松键即结束:

```json
{
  "intervals": [
    {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 400},
    {"channel": 0, "pitch": 64, "start_ms": 150, "end_ms": 250}
  ],
  "errors": []
}
```

两块踏板同时保持时,任一踏板抬起,仍由另一踏板保持的音不会被截断,两者都不再保持时才结束:

```bash
curl -s -X POST http://localhost:8000/render \
  -H 'Content-Type: application/json' \
  -d '{
    "end_ms": 1000,
    "events": [
      {"time_ms": 0,   "channel": 0, "type": "note_on",       "pitch": 60},
      {"time_ms": 50,  "channel": 0, "type": "pedal_down"},
      {"time_ms": 100, "channel": 0, "type": "sostenuto_down"},
      {"time_ms": 200, "channel": 0, "type": "note_off",      "pitch": 60},
      {"time_ms": 400, "channel": 0, "type": "pedal_up"},
      {"time_ms": 600, "channel": 0, "type": "sostenuto_up"}
    ]
  }'
```

```json
{
  "intervals": [
    {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 600}
  ],
  "errors": []
}
```

无效的重复踩下 / 抬起(以选择性踏板为例)不改变状态,在 `errors` 中给出事件下标与原因:

```json
{
  "end_ms": 1000,
  "events": [
    {"time_ms": 0,  "channel": 0, "type": "sostenuto_down"},
    {"time_ms": 10, "channel": 0, "type": "sostenuto_down"},
    {"time_ms": 20, "channel": 0, "type": "sostenuto_up"},
    {"time_ms": 30, "channel": 0, "type": "sostenuto_up"}
  ]
}
```

```json
{
  "intervals": [],
  "errors": [
    {"index": 1, "reason": "sostenuto_already_down"},
    {"index": 3, "reason": "sostenuto_already_up"}
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
      {"time_ms": 0,   "channel": 0, "type": "note_on",       "pitch": 60},
      {"time_ms": 0,   "channel": 1, "type": "note_on",       "pitch": 64},
      {"time_ms": 50,  "channel": 1, "type": "sostenuto_down"},
      {"time_ms": 100, "channel": 0, "type": "pedal_down"},
      {"time_ms": 200, "channel": 0, "type": "note_off",      "pitch": 60},
      {"time_ms": 300, "channel": 1, "type": "note_off",      "pitch": 64},
      {"time_ms": 700, "channel": 1, "type": "sostenuto_up"},
      {"time_ms": 800, "channel": 0, "type": "pedal_up"}
    ]
  }'
```

通道 0 的音松键后靠普通延音踏板延续 600ms(200 -> 800);通道 1 的音松键时被选择性踏板锁定,
延续 400ms(300 -> 700)。两项统计按通道升序返回:

```json
{
  "intervals": [
    {"channel": 0, "pitch": 60, "start_ms": 0, "end_ms": 800},
    {"channel": 1, "pitch": 64, "start_ms": 0, "end_ms": 700}
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
      "total_duration_ms": 700,
      "peak_polyphony": 1,
      "peak_at_ms": 0,
      "sustained_duration_ms": 400
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
