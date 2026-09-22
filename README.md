# Three-way JSON Merge API

面向**配置审阅**场景的三方 JSON 合并服务（Python 3.12 + FastAPI）。提交一份公共
基线 `base` 和两个修订版 `left` / `right`，服务沿 [JSON Pointer (RFC 6901)](https://www.rfc-editor.org/rfc/rfc6901)
递归合并，并给出确定的结果与冲突清单。

## 一条命令启动

```bash
docker compose up --build -d
```

服务在宿主机 **8000** 端口启动（可用环境变量 `PUBLISH_PORT` 改映射端口）：

- 健康检查：<http://localhost:8000/health>
- 合并接口：`POST http://localhost:8000/merge`
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

停止：`docker compose down`；查看日志：`docker compose logs -f`。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAX_REQUEST_SIZE_BYTES` | `10M` | 请求体大小上限。纯字节数（如 `1048576`），或带 `K` / `M` / `G`（1024 进制）后缀，如 `256K`、`10M`、`1G`。`0` 表示不限制。超限返回 `413`。 |
| `PUBLISH_PORT` | `8000` | 仅 Compose 使用：宿主机映射端口（容器内固定监听 8000）。 |

可以直接在环境里设置，或在仓库根目录放一份 `.env`（`docker compose` 会自动读取）：

```dotenv
MAX_REQUEST_SIZE_BYTES=10M
PUBLISH_PORT=8000
```

## 合并语义

沿 JSON Pointer 递归，对每个位置比较 `base` / `left` / `right`：

| 情况 | 结果 |
| --- | --- |
| 只有一侧改动（另一侧与 base 相同） | 自动采纳改动 |
| 两侧做出相同改动 | 合并该改动（只生效一次） |
| 两侧都删除同一字段/项 | 删除 |
| 一侧删除、另一侧修改（或一侧缺失时两侧新增不同值） | **保留 base**，报告冲突 |
| 两侧把同一值改成不同结果（叶子值/普通数组） | **保留 base**，报告冲突 |
| 两侧修改同一对象的不同字段 | 各自自动合并；仅冲突字段报错 |
| 未配置身份字段的数组 | 视为**整体值**（整体替换 / 整体冲突，不逐项 diff） |

冲突对象的 `kind`：

- `both_modified`：两侧把同一值改成不同结果（或 `null` 与其他值互斥修改）；
- `delete_vs_modify`：一侧删除、另一侧修改；
- `both_added`：base 中不存在该路径，两侧新增了不同的值（含身份数组中同 ID 新增内容不同）；
- `protected_change`：命中 `protectedPaths` 的位置被任一侧改动（包括单侧改动、或两侧相同改动），
  默认保留 base，等待裁决。

冲突一律保留 base 值，冲突记录包含五个字段：

```json
{ "pointer": "/servers/0/port", "kind": "both_modified",
  "base": 8080, "left": 9090, "right": 80 }
```

被删除的一侧在冲突里显示为 `null`。冲突按 `pointer`（同指针再按 `kind`）升序排列；
相同输入输出完全确定。

### 冲突裁决（conflictResolutions）

默认情况下所有冲突都保留 base 值。请求可携带可选的 `conflictResolutions`
（也接受别名 `conflict_resolutions`）逐项裁决，将**响应中报告的冲突指针**
（RFC 6901，身份数组已解析为最终下标）映射为胜出一方：

```json
"conflictResolutions": [
  { "pointer": "/servers/0/port", "resolution": "right" },
  { "pointer": "/servers/1", "resolution": "left" }
]
```

- `resolution` 取 `"base"` / `"left"` / `"right"`：胜出方的值被放入合并结果的
  冲突位置；选中已删除该位置的一侧即删除该位置（对 `both_added` 选 `"base"`
  表示该值不加入）；
- 未列出的冲突保持默认行为（保留 base）；
- 裁决适用于嵌套对象字段、身份数组的整项与项内字段、以及 `*` 通配路径解析后
  的最终指针（如 `/servers/0/disks/1/size`）；接受数组项删除后，其余裁决仍按
  **原报告指针**对应到正确的身份项，不受下标前移影响；
- 响应 `conflicts` 不受裁决影响，始终完整保留原三方值与指针，便于审计。

以下情况返回 **400**：指针格式非法、同一指针重复出现、指针在合并结果中不存在、
或指针指向的位置没有冲突。`resolution` 取其他值返回 422。

### 保护路径（protectedPaths）

有些位置（如敏感配置、审计字段）不允许被任何一侧“顺手改掉”。请求可携带可选的
`protectedPaths`（也接受别名 `protected_paths`），值为 [JSON Pointer (RFC 6901)](https://www.rfc-editor.org/rfc/rfc6901.html)
字符串数组，其中单段 `*` 通配任意一个路径段：

```json
"protectedPaths": ["/db/password", "/servers/*/port"]
```

命中路径只要发生**任一左右版本变更**，即使是单侧改动、或两侧做出相同改动，也**不会自动写入**
`merged`：服务保留 base 值，并在 `conflicts` 中追加一条三方值记录，其 `kind` 为
`protected_change`（`base` / `left` / `right` 分别为该位置的三方原值，删除侧记为 `null`）。

匹配规则：

- **对象字段、身份数组项及其字段**按最终报告指针判断（身份项下标为合并完成后的最终下标，
  例如先于它的项被删除而下标前移，以最终下标为准）；
- **删除按原路径处理**：身份数组项被删除时，以它在 base 中的原始下标匹配；
- 未配置身份字段的普通数组仍是整体值，只能保护数组本身（如 `/a`），数组元素级指针
  （如 `/a/1`）不参与逐项 diff；
- 未发生任何变更的保护路径不产生冲突。

`protectedPaths` 与 `conflictResolutions` **共存**：保护冲突与普通冲突一样可以被明确裁决，
裁决胜出方覆盖保护（选择已删除的一侧即删除该位置）；**未裁决的保护冲突始终保留 base**。
响应 `conflicts` 同样不受裁决影响，始终保留原三方值。

以下情况返回 **400**：指针格式非法（不以 `/` 开头等）、同一指针重复出现（按解码后的
reference token 比较，`/a~0b` 与等价写法视为重复）。数组元素不是字符串时返回 422。

### 对象数组与身份字段

在 `arrayIdentity` 中声明“数组指针 → 身份字段名”：

- 数组按身份字段识别同一项，进行**增 / 删 / 改**合并；
- 既有项**保持 base 中的顺序**；一侧删除而另一侧修改时保留 base 项并报告
  `delete_vs_modify`，冲突指针指向该项的最终下标（如 `/srv/1`）；
- 新增项（单侧新增，或两侧同 ID 同内容）按身份值排序后追加，排序次序：
  `null < 布尔 < 数字（按数值） < 字符串（按字典序）`；
- 指针段 `*` 通配单个位置下标，可用于嵌套数组，例如
  `"/servers/*/disks": "name"`；
- 任一侧出现非对象元素、缺少身份字段、或身份重复时，该数组自动退化为整体值比较。

## 请求与响应

`POST /merge`，字段 `base` / `left` / `right` 为任意 JSON 文档；
`arrayIdentity`（也接受别名 `array_identity`）为“指针 → 字段名”映射，可省略；
`conflictResolutions`（也接受别名 `conflict_resolutions`）为冲突裁决列表，可省略；
`protectedPaths`（也接受别名 `protected_paths`）为受保护指针数组，可省略。

请求：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
    "left":  {"servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 80}]},
    "right": {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 81}]},
    "arrayIdentity": {"/servers": "id"}
  }'
```

响应：

```json
{
  "merged": {
    "servers": [
      {"id": "a", "port": 9090},
      {"id": "b", "port": 81}
    ]
  },
  "conflicts": []
}
```

冲突示例（a 被 left 删除、被 right 修改）：

```json
{
  "merged": {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
  "conflicts": [
    {
      "pointer": "/servers/0",
      "kind": "delete_vs_modify",
      "base": {"id": "a", "port": 8080},
      "left": null,
      "right": {"id": "a", "port": 9090}
    }
  ]
}
```

指针遵循 RFC 6901 转义：`~` → `~0`，`/` → `~1`；根文档冲突的指针为空字符串 `""`。

裁决示例（沿用上面的冲突，裁决 `/servers/0` 由 right 胜出）：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
    "left":  {"servers": [{"id": "b", "port": 80}]},
    "right": {"servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 80}]},
    "arrayIdentity": {"/servers": "id"},
    "conflictResolutions": [{"pointer": "/servers/0", "resolution": "right"}]
  }'
```

响应（`merged` 中 a 取 right 的值；`conflicts` 保持原三方值不变）：

```json
{
  "merged": {"servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 80}]},
  "conflicts": [
    {
      "pointer": "/servers/0",
      "kind": "delete_vs_modify",
      "base": {"id": "a", "port": 8080},
      "left": null,
      "right": {"id": "a", "port": 9090}
    }
  ]
}
```

保护路径示例（`/db/password` 即使只被 left 单侧修改也不自动写入，保留 base）：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"db": {"host": "a", "password": "secret"}},
    "left":  {"db": {"host": "b", "password": "rotated"}},
    "right": {"db": {"host": "b", "password": "secret"}},
    "protectedPaths": ["/db/password"]
  }'
```

响应（`host` 的单侧改动照常合并；`password` 保留 base，并报告 `protected_change`）：

```json
{
  "merged": {"db": {"host": "b", "password": "secret"}},
  "conflicts": [
    {
      "pointer": "/db/password",
      "kind": "protected_change",
      "base": "secret",
      "left": "rotated",
      "right": "secret"
    }
  ]
}
```

该冲突同样可用 `conflictResolutions` 裁决（如
`{"pointer": "/db/password", "resolution": "left"}`）；不裁决则一直保留 base。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

## 项目结构

```
app/
  main.py         FastAPI 路由（/health、/merge）
  merger.py       三方合并引擎（JSON Pointer、身份数组、冲突建模）
  middleware.py   请求体大小上限 ASGI 中间件
  config.py       环境变量配置与大小解析
  models.py       请求/响应 Pydantic 模型
tests/            引擎与 HTTP 层测试（pytest）
Dockerfile        精简镜像（python:3.12-slim，非 root，内置 HEALTHCHECK）
docker-compose.yml 一键启动
```
