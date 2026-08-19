# 设计：路由目标改为下拉选择

> 2026-08-09 头脑风暴产出，用户已确认。范围：路由策略页 4 个场景 + 模型规则页「转发目标」列。

## 背景与目标

路由策略页和模型规则页的目标字段（格式 `provider/model`）目前是自由文本输入（`<input list="model-list">`，datalist 仅提供残缺的自动补全）。用户要求改为下拉选择，无需人工输入。

模型清单的来源：`ProviderConfig` 目前只有 `base_url`/`api_key` 等字段，没有模型清单。决策：**模型列表存进供应商配置，供应商页提供「从 API 拉取」按钮自动填充**——离线可用、无运行时外部依赖，拉取失败仍可手动维护。

## 设计

### 1. 配置 schema（`suanpan/config.py`）

`ProviderConfig` 新增：

```python
models: list[str] = Field(default_factory=list)
```

随 `~/.suanpan.yaml` 往返序列化（`dump_config` 自动携带）。路由目标校验（`_check_route_targets`）不变，仍只校验 provider 段。

### 2. 模型拉取（`config_server.py` + 复用 `balance_usage.py`）

config_server 新增端点：

```
POST /api/fetch-models
Body: {"provider": "<name>"}
→ 200 {"models": ["deepseek-v4-flash", ...]}
→ 200 {"error": "<人类可读错误>"}   # 业务失败也返回 200 + error 字段，与 /api/balance 一致
```

服务端逻辑：

1. 按 name 查 `providers`，不存在则返回 error。
2. 复用 balance 的 key 解析（`api_key` 内联优先，`api_key_env` 读环境变量），按 `auth_header` 构造认证头：`x-api-key: <key>`（另带 `anthropic-version: 2023-06-01`）或 `Authorization: Bearer <key>`。
3. `GET {base_url.rstrip('/')}/v1/models`；404 时依次回退 `{base_url}/models` → `{origin}/v1/models` → `{origin}/models`（`/v1/messages` 约定 + 冒烟发现：DeepSeek 的 Messages API 挂在 `/anthropic` 前缀下，模型清单只在站点根路径提供）。
4. 解析响应 `data[].id`（Anthropic 与 OpenAI 风格同构），去重、保持原顺序返回。
5. 超时 10s；网络错误、非 2xx（非 404）、JSON 解析失败均返回结构化 error。

前端收到 `models` 后写入 `S.sp.providers[name].models`，`markDirty()`，走既有「保存更改」随 `/api/state` PUT 落盘。

### 3. 供应商页 UI（`config_ui.html` providerHTML）

详情区新增「模型」块：

- 已有模型渲染为只读标签列表（chips）。
- 「从 API 拉取」按钮：调用 `/api/fetch-models`，成功则替换 models 列表；拉取中禁用按钮并显示加载态；失败在块内显示错误文本。
- 手动添加：一个小输入框 + 添加按钮（兜底逃生口，防止供应商不实现 `/models` 时无路可走）。每个 chip 带删除按钮。
- 已停用供应商也可拉取。

### 4. 路由策略页 + 模型规则页 UI

4 个场景输入框（`route-default/background/long_context/think`）与规则表「转发目标」列，统一换成：

```html
<select class="control mono">
  <option value="">（不路由/留空）</option>
  <optgroup label="deepseek">
    <option value="deepseek/deepseek-v4-flash">deepseek/deepseek-v4-flash</option>
    ...
  </optgroup>
  ...
</select>
```

规则：

- 选项来自 `S.sp.providers` 中各供应商的 `models` 清单；`enabled === false` 的供应商不出现在分组里。
- 当前值不在任何选项中时（models 清单为空、旧配置残留、引用了已停用供应商），追加一项 `<option>` 显示当前值并标注「（未在清单）」，保证保存不静默丢值。
- 场景首项文案「（不路由该场景）」；规则表目标列首项「（未设置）」。
- `collectRouting()` / `collectRules()` 改读 select 的 `value`；场景空值存 `null`，规则空值存空串（均沿用现有行为）。
- 删除 `modelOptions()` 与两处 `<datalist id="model-list">`。
- 路由策略页说明文案从「格式：provider/model」改为「从下拉选择已配置供应商的模型」。

### 5. 测试

- `tests/suanpan/test_config.py`：`models` 字段缺省为空列表、YAML 往返保留、路由校验不受 models 影响。
- `tests/test_config_server.py`（或新文件）fetch-models 端点，在边界 mock `urllib.request.urlopen`（balance_usage 用 stdlib urllib，不用 httpx）：
  - 成功解析 `data[].id`
  - 404 → 回退无 `/v1` 路径再成功
  - 两种 `auth_header` 的请求头构造
  - provider 不存在 → error
  - 网络超时 / 非 2xx / 响应缺 `data` → error
- UI 无测试基建，手动冒烟：webview 打开 :9528，验证下拉渲染、选择、保存后 YAML 内容。

## 不做（YAGNI）

- 不做双下拉级联（单下拉 + optgroup 已确认）。
- 不做启动时自动拉取（只在用户点按钮时拉）。
- 不校验路由目标的 model 段是否在 models 清单内（「未在清单」标注已足够，保留灵活度）。
- 模型规则「匹配前缀」列保持自由输入（它匹配的是客户端请求的模型名，不是 provider/model）。
