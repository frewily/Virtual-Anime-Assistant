# DeepSeek 工具调用协议兼容实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 OpenAI 兼容网关以安全工具别名和内存态 `reasoning_content` 回传支持 DeepSeek V4 工具调用，同时保持内部工具名、权限、审计与持久化边界不变。

**架构：** 兼容逻辑只位于 `OpenAICompatibleGateway` 的供应商协议边界：每次请求建立内部名与供应商别名的双向映射，序列化时正向转换，解析时严格反向转换。模型契约承载有界的可选思考内容，编排器仅把工具调用轮次的该字段放入下一轮 assistant 消息；最终文字和所有存储、接口、日志仍只处理公开回复。

**技术栈：** Python 3、Pydantic v2、httpx、FastAPI、SQLite、`unittest`、Node.js/Electron

---

## 文件结构

- 修改：`backend/llm/models.py`——定义 `reasoning_content` 的长度、角色和工具调用约束。
- 修改：`backend/llm/openai_compatible.py`——生成单次请求工具别名、序列化正向映射、响应严格反向映射以及思考内容解析。
- 修改：`backend/application/model_tools.py`——只在工具调用后的下一轮 assistant 消息中回传思考内容。
- 修改：`backend/tests/test_llm_models_config.py`——覆盖模型契约的合法形状、空白、超长和最终回复边界。
- 修改：`backend/tests/test_openai_compatible.py`——覆盖别名、冲突、重复声明、未知别名、消息映射和思考内容协议。
- 修改：`backend/tests/test_model_tool_orchestrator.py`——覆盖编排器的单轮内存回传和无思考内容兼容路径。
- 修改：`backend/tests/test_integration.py`——证明 HTTP、消息、模型调用和 SQLite 不泄露思考内容。
- 修改：`docs/superpowers/plans/2026-08-11-deepseek-tool-calling-compat.md`——在真实验收后记录命令、结果和隐私检查，不记录供应商正文或思考内容。

### 任务 1：收紧模型契约并承载思考内容

**文件：**
- 修改：`backend/llm/models.py:47-100`
- 测试：`backend/tests/test_llm_models_config.py:374-599`

- [ ] **步骤 1：编写失败的模型契约测试**

在 `ModelContractTests` 中加入以下测试，使用测试哨兵而非真实供应商思考内容：

```python
def test_reasoning_content_requires_assistant_tool_calls(self):
    call = ModelToolCall(
        id="call-1",
        name="system.current_time",
        arguments={},
    )
    message = ModelMessage(
        role=ModelRole.ASSISTANT,
        tool_calls=[call],
        reasoning_content="private-reasoning-sentinel",
    )
    reply = ModelReply(
        tool_calls=[call],
        reasoning_content="private-reasoning-sentinel",
        model="model",
    )
    self.assertEqual(message.reasoning_content, "private-reasoning-sentinel")
    self.assertEqual(reply.reasoning_content, "private-reasoning-sentinel")

    invalid_messages = (
        {"role": ModelRole.USER, "content": "hello", "reasoning_content": "x"},
        {"role": ModelRole.ASSISTANT, "content": "answer", "reasoning_content": "x"},
        {"role": ModelRole.TOOL, "content": "{}", "tool_call_id": "call-1", "name": call.name, "reasoning_content": "x"},
        {"role": ModelRole.ASSISTANT, "tool_calls": [call], "reasoning_content": "   "},
        {"role": ModelRole.ASSISTANT, "tool_calls": [call], "reasoning_content": "x" * 64_001},
    )
    for values in invalid_messages:
        with self.subTest(values=values), self.assertRaises(ValidationError):
            ModelMessage(**values)


def test_reply_reasoning_content_requires_tool_calls(self):
    tool_call = ModelToolCall(
        id="call-1",
        name="system.current_time",
        arguments={},
    )
    invalid_replies = (
        {"text": "answer", "reasoning_content": "x", "model": "model"},
        {"tool_calls": [tool_call], "reasoning_content": "   ", "model": "model"},
        {"tool_calls": [tool_call], "reasoning_content": "x" * 64_001, "model": "model"},
    )
    for values in invalid_replies:
        with self.subTest(values=values), self.assertRaises(ValidationError):
            ModelReply(**values)
```

- [ ] **步骤 2：运行测试并确认缺失字段导致失败**

运行：

```bash
python3 -m unittest backend.tests.test_llm_models_config.ModelContractTests.test_reasoning_content_requires_assistant_tool_calls backend.tests.test_llm_models_config.ModelContractTests.test_reply_reasoning_content_requires_tool_calls -v
```

预期：`ERROR` 或 `FAIL`，指出 `ModelMessage` / `ModelReply` 尚无 `reasoning_content` 字段或未拒绝非法形状。

- [ ] **步骤 3：实现最小模型字段和校验**

在 `backend/llm/models.py` 中增加常量，并扩展两个模型：

```python
_MAX_REASONING_CONTENT_CHARS = 64_000


class ModelMessage(BaseModel):
    role: ModelRole
    content: str | None = Field(default=None, min_length=1, max_length=12000)
    reasoning_content: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_REASONING_CONTENT_CHARS,
    )
    tool_calls: list[ModelToolCall] = Field(default_factory=list, max_length=4)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, pattern=_SAFE_NAME_PATTERN)

    @model_validator(mode="after")
    def require_role_shape(self) -> "ModelMessage":
        if self.reasoning_content is not None:
            if not self.reasoning_content.strip():
                raise ValueError("reasoning content cannot be blank")
            if self.role is not ModelRole.ASSISTANT or not self.tool_calls:
                raise ValueError(
                    "reasoning content requires assistant tool calls"
                )
        if self.role in (ModelRole.SYSTEM, ModelRole.USER):
            if self.content is None:
                raise ValueError("system and user messages require content")
            if self.tool_calls or self.tool_call_id is not None or self.name is not None:
                raise ValueError("system and user messages cannot carry tool fields")
        elif self.role is ModelRole.ASSISTANT:
            if self.content is None and not self.tool_calls:
                raise ValueError("assistant messages require content or tool calls")
            if self.tool_call_id is not None or self.name is not None:
                raise ValueError("assistant messages cannot be tool results")
        elif self.role is ModelRole.TOOL:
            if (
                self.content is None
                or self.tool_call_id is None
                or self.name is None
                or self.tool_calls
            ):
                raise ValueError(
                    "tool messages require content, tool_call_id, and name only"
                )
        return self


class ModelReply(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    reasoning_content: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_REASONING_CONTENT_CHARS,
    )
    tool_calls: list[ModelToolCall] = Field(default_factory=list, max_length=4)
    model: str = Field(min_length=1, max_length=200)
    finish_reason: str | None = Field(default=None, max_length=100)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def require_output(self) -> "ModelReply":
        if self.text is None and not self.tool_calls:
            raise ValueError("model reply requires text or tool calls")
        if self.reasoning_content is not None:
            if not self.reasoning_content.strip():
                raise ValueError("reasoning content cannot be blank")
            if not self.tool_calls:
                raise ValueError("reasoning content requires tool calls")
        return self
```

- [ ] **步骤 4：运行模型测试确认通过**

运行：

```bash
python3 -m unittest backend.tests.test_llm_models_config -v
```

预期：全部 `OK`。

- [ ] **步骤 5：提交模型契约变更**

```bash
git add backend/llm/models.py backend/tests/test_llm_models_config.py
git commit -m "feat: 承载工具调用思考上下文"
```

### 任务 2：在网关边界映射安全工具别名

**文件：**
- 修改：`backend/llm/openai_compatible.py:1-250`
- 测试：`backend/tests/test_openai_compatible.py:48-510`

- [ ] **步骤 1：编写失败的别名与请求映射测试**

新增网关测试，先断言 `system.current_time` 被映射为确定的合法别名：

```python
async def test_complete_maps_internal_tool_names_in_every_request_position(self):
    captured_payload = None

    def handler(request):
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return _json_response(
            {"choices": [{"message": {"content": "done"}}]}
        )

    tool = _request_with_time_tool().tools[0]
    call = ModelToolCall(id="call-1", name=tool.name, arguments={})
    request = ModelRequest(
        correlation_id="message-1",
        tools=[tool],
        messages=[
            ModelMessage(role=ModelRole.USER, content="几点了"),
            ModelMessage(
                role=ModelRole.ASSISTANT,
                tool_calls=[call],
                reasoning_content="private-reasoning-sentinel",
            ),
            ModelMessage(
                role=ModelRole.TOOL,
                content='{"state":"succeeded"}',
                tool_call_id=call.id,
                name=call.name,
            ),
        ],
    )
    gateway = OpenAICompatibleGateway(
        _settings(tool_calling_enabled=True),
        transport=httpx.MockTransport(handler),
    )

    await gateway.complete(request)

    alias = "system_current_time_2a9c83b2"
    self.assertRegex(alias, r"^[A-Za-z0-9_-]{1,64}$")
    self.assertEqual(captured_payload["tools"][0]["function"]["name"], alias)
    self.assertEqual(
        captured_payload["messages"][1]["tool_calls"][0]["function"]["name"],
        alias,
    )
    self.assertEqual(captured_payload["messages"][2]["name"], alias)
    self.assertEqual(
        captured_payload["messages"][1]["reasoning_content"],
        "private-reasoning-sentinel",
    )
```

再加入以下覆盖：

```python
async def test_complete_keeps_legal_short_tool_name(self):
    captured_payload = None

    def handler(request):
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return _json_response(
            {"choices": [{"message": {"content": "done"}}]}
        )

    tool = ModelToolDefinition(
        name="current_time",
        description="读取当前时间",
        parameters={"type": "object", "properties": {}},
    )
    gateway = OpenAICompatibleGateway(
        _settings(tool_calling_enabled=True),
        transport=httpx.MockTransport(handler),
    )

    await gateway.complete(ModelRequest(
        correlation_id="message-1",
        messages=[ModelMessage(role=ModelRole.USER, content="几点了")],
        tools=[tool],
    ))

    self.assertEqual(
        captured_payload["tools"][0]["function"]["name"],
        "current_time",
    )

async def test_complete_generates_unique_bounded_aliases_for_long_and_colliding_names(self):
    captured_payload = None

    def handler(request):
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return _json_response(
            {"choices": [{"message": {"content": "done"}}]}
        )

    names = [
        "system.current_time",
        "system_current_time_2a9c83b2",
        "memory." + "x" * 80,
    ]
    tools = [
        ModelToolDefinition(
            name=name,
            description=f"工具 {index}",
            parameters={"type": "object", "properties": {}},
        )
        for index, name in enumerate(names)
    ]
    gateway = OpenAICompatibleGateway(
        _settings(tool_calling_enabled=True),
        transport=httpx.MockTransport(handler),
    )

    await gateway.complete(ModelRequest(
        correlation_id="message-1",
        messages=[ModelMessage(role=ModelRole.USER, content="运行工具")],
        tools=tools,
    ))

    aliases = [
        item["function"]["name"] for item in captured_payload["tools"]
    ]
    self.assertEqual(len(set(aliases)), len(names))
    self.assertEqual(aliases[1], "system_current_time_2a9c83b2")
    for alias in aliases:
        self.assertRegex(alias, r"^[A-Za-z0-9_-]{1,64}$")
        self.assertLessEqual(len(alias), 64)

async def test_complete_rejects_duplicate_internal_tool_names_before_network(self):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return _json_response(
            {"choices": [{"message": {"content": "done"}}]}
        )

    tool = _request_with_time_tool().tools[0]
    gateway = OpenAICompatibleGateway(
        _settings(tool_calling_enabled=True),
        transport=httpx.MockTransport(handler),
    )
    request = ModelRequest(
        correlation_id="message-1",
        messages=[ModelMessage(role=ModelRole.USER, content="几点了")],
        tools=[tool, tool],
    )

    with self.assertRaises(ModelConfigurationError):
        await gateway.complete(request)

    self.assertEqual(calls, 0)

async def test_complete_rejects_messages_referencing_undeclared_tools_before_network(self):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return _json_response(
            {"choices": [{"message": {"content": "done"}}]}
        )

    declared = _request_with_time_tool().tools[0]
    undeclared_call = ModelToolCall(
        id="call-1",
        name="memory.search",
        arguments={},
    )
    invalid_message_sets = (
        [
            ModelMessage(role=ModelRole.USER, content="搜索"),
            ModelMessage(
                role=ModelRole.ASSISTANT,
                tool_calls=[undeclared_call],
            ),
        ],
        [
            ModelMessage(role=ModelRole.USER, content="搜索"),
            ModelMessage(
                role=ModelRole.TOOL,
                content='{"state":"succeeded"}',
                tool_call_id="call-1",
                name="memory.search",
            ),
        ],
    )
    gateway = OpenAICompatibleGateway(
        _settings(tool_calling_enabled=True),
        transport=httpx.MockTransport(handler),
    )

    for messages in invalid_message_sets:
        with self.subTest(role=messages[-1].role):
            with self.assertRaises(ModelConfigurationError):
                await gateway.complete(ModelRequest(
                    correlation_id="message-1",
                    messages=messages,
                    tools=[declared],
                ))

    self.assertEqual(calls, 0)
```

- [ ] **步骤 2：运行新增映射测试并确认失败**

运行：

```bash
python3 -m unittest backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_maps_internal_tool_names_in_every_request_position backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_keeps_legal_short_tool_name backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_generates_unique_bounded_aliases_for_long_and_colliding_names backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_rejects_duplicate_internal_tool_names_before_network backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_rejects_messages_referencing_undeclared_tools_before_network -v
```

预期：`FAIL`，现有请求仍包含带句点的内部工具名，并且重复声明未在本地拒绝。

- [ ] **步骤 3：实现单次请求双向别名表**

在 `backend/llm/openai_compatible.py` 顶部新增依赖和私有类型：

```python
import re
from dataclasses import dataclass
from hashlib import sha256

from .models import ModelToolDefinition

_PROVIDER_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INVALID_PROVIDER_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class _ToolNameAliases:
    internal_to_provider: dict[str, str]
    provider_to_internal: dict[str, str]

    @classmethod
    def build(
        cls,
        tools: list[ModelToolDefinition],
    ) -> "_ToolNameAliases":
        internal_names = [tool.name for tool in tools]
        if len(set(internal_names)) != len(internal_names):
            raise ModelConfigurationError("duplicate model tool name")

        used = {
            name for name in internal_names if _PROVIDER_TOOL_NAME.fullmatch(name)
        }
        forward: dict[str, str] = {}
        reverse: dict[str, str] = {}
        for index, internal_name in enumerate(internal_names, start=1):
            if _PROVIDER_TOOL_NAME.fullmatch(internal_name):
                candidate = internal_name
            else:
                cleaned = _INVALID_PROVIDER_TOOL_NAME.sub("_", internal_name)
                cleaned = cleaned.strip("_") or "tool"
                digest = sha256(internal_name.encode("utf-8")).hexdigest()[:8]
                for offset in range(len(tools) + 1):
                    suffix = (
                        f"_{digest}"
                        if offset == 0
                        else f"_{index + offset - 1}_{digest}"
                    )
                    candidate = f"{cleaned[:64 - len(suffix)]}{suffix}"
                    if candidate not in used:
                        break
                else:
                    raise ModelConfigurationError(
                        "model tool aliases are not unique"
                    )
            forward[internal_name] = candidate
            reverse[candidate] = internal_name
            used.add(candidate)
        return cls(forward, reverse)

    def to_provider(self, internal_name: str) -> str:
        try:
            return self.internal_to_provider[internal_name]
        except KeyError:
            raise ModelConfigurationError(
                "model message references an undeclared tool"
            ) from None

    def to_internal(self, provider_name: str) -> str:
        try:
            return self.provider_to_internal[provider_name]
        except KeyError:
            raise ModelProtocolError(
                "model service returned an invalid response"
            ) from None
```

同时把现有 `.models` 导入列表中的 `ModelToolDefinition` 与上方示例合并为一处。按以下代码改造请求构建和消息序列化：

```python
async def complete(self, request: ModelRequest) -> ModelReply:
    aliases = _ToolNameAliases.build(request.tools)
    payload: dict[str, Any] = {
        "model": self._model_name,
        "messages": [
            self._message_payload(message, aliases)
            for message in request.messages
        ],
        "stream": False,
    }
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": aliases.to_provider(tool.name),
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_output_tokens is not None:
        payload["max_tokens"] = request.max_output_tokens

    headers = {"Content-Type": "application/json"}
    if self._api_key:
        headers["Authorization"] = f"Bearer {self._api_key}"

    try:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            trust_env=False,
            transport=self._transport,
        ) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
    except httpx.TimeoutException:
        raise ModelTimeoutError("model service request timed out") from None
    except httpx.RequestError:
        raise ModelServiceError("model service request failed") from None

    self._raise_for_status(response.status_code)
    return self._parse_reply(
        response,
        allow_tool_calls=bool(request.tools),
        aliases=aliases,
    )

@staticmethod
def _message_payload(
    message: ModelMessage,
    aliases: _ToolNameAliases,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": message.role.value,
        "content": message.content,
    }
    if message.role is ModelRole.ASSISTANT and message.tool_calls:
        payload["content"] = None
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": aliases.to_provider(call.name),
                    "arguments": json.dumps(
                        call.arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            }
            for call in message.tool_calls
        ]
        if message.reasoning_content is not None:
            payload["reasoning_content"] = message.reasoning_content
        return payload
    if message.role is ModelRole.TOOL:
        payload["tool_call_id"] = message.tool_call_id
        payload["name"] = aliases.to_provider(message.name)
    return payload
```

将解析签名改为接收同一别名表：

```python
def _parse_reply(
    self,
    response: httpx.Response,
    *,
    allow_tool_calls: bool,
    aliases: _ToolNameAliases,
) -> ModelReply:
```

并把现有 `tool_calls.append(...)` 完整替换为：

```python
    tool_calls.append(
        ModelToolCall(
            id=call_id,
            name=aliases.to_internal(function.name),
            arguments=arguments,
        )
    )
```

- [ ] **步骤 4：编写失败的响应反向映射和思考内容测试**

新增测试：

```python
async def test_complete_maps_response_alias_back_and_keeps_tool_reasoning(self):
    gateway = OpenAICompatibleGateway(
        _settings(tool_calling_enabled=True),
        transport=httpx.MockTransport(
            lambda request: _json_response({
                "choices": [{
                    "message": {
                        "content": None,
                        "reasoning_content": "private-reasoning-sentinel",
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "system_current_time_2a9c83b2",
                                "arguments": "{}",
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            })
        ),
    )

    reply = await gateway.complete(_request_with_time_tool())

    self.assertEqual(reply.tool_calls[0].name, "system.current_time")
    self.assertEqual(reply.reasoning_content, "private-reasoning-sentinel")


async def test_complete_discards_reasoning_from_final_text_reply(self):
    gateway = OpenAICompatibleGateway(
        _settings(),
        transport=httpx.MockTransport(
            lambda request: _json_response({
                "choices": [{
                    "message": {
                        "content": "done",
                        "reasoning_content": "private-reasoning-sentinel",
                    },
                }],
            })
        ),
    )

    reply = await gateway.complete(_request())

    self.assertEqual(reply.text, "done")
    self.assertIsNone(reply.reasoning_content)

async def test_complete_rejects_unknown_provider_tool_alias(self):
    gateway = OpenAICompatibleGateway(
        _settings(tool_calling_enabled=True),
        transport=httpx.MockTransport(
            lambda request: _json_response({
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "unknown_tool",
                                "arguments": "{}",
                            },
                        }],
                    },
                }],
            })
        ),
    )

    with self.assertRaises(ModelProtocolError):
        await gateway.complete(_request_with_time_tool())

async def test_complete_rejects_invalid_reasoning_content(self):
    invalid_values = ("   ", "x" * 64_001, {"private": "value"})
    for invalid_value in invalid_values:
        with self.subTest(value_type=type(invalid_value).__name__):
            gateway = OpenAICompatibleGateway(
                _settings(tool_calling_enabled=True),
                transport=httpx.MockTransport(
                    lambda request, value=invalid_value: _json_response({
                        "choices": [{
                            "message": {
                                "content": None,
                                "reasoning_content": value,
                                "tool_calls": [{
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "system_current_time_2a9c83b2",
                                        "arguments": "{}",
                                    },
                                }],
                            },
                        }],
                    })
                ),
            )

            with self.assertRaises(ModelProtocolError) as raised:
                await gateway.complete(_request_with_time_tool())

            self.assertEqual(
                str(raised.exception),
                "model service returned an invalid response",
            )
```

- [ ] **步骤 5：运行响应测试并确认失败**

运行：

```bash
python3 -m unittest backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_maps_response_alias_back_and_keeps_tool_reasoning backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_discards_reasoning_from_final_text_reply backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_rejects_unknown_provider_tool_alias backend.tests.test_openai_compatible.OpenAICompatibleGatewayTests.test_complete_rejects_invalid_reasoning_content -v
```

预期：`FAIL`，响应名称尚未反向映射，`ModelReply` 尚未接收合法工具轮思考内容。

- [ ] **步骤 6：实现思考内容校验和工具轮保留**

在 `_parse_reply()` 中解析消息后、构建 `ModelReply` 前加入：

```python
reasoning_content = choice.message.reasoning_content
if reasoning_content is not None:
    if not reasoning_content.strip() or len(reasoning_content) > 64_000:
        raise ValueError("invalid reasoning content")
if not tool_calls:
    reasoning_content = None
```

然后把 `reasoning_content=reasoning_content` 传给 `ModelReply`。继续让 `_ResponseMessage` 的 `StrictStr` 拒绝数字、对象和数组；异常边界仍统一转换为不带正文的 `ModelProtocolError("model service returned an invalid response")`。

- [ ] **步骤 7：更新原有工具名断言并运行完整网关测试**

把现有请求断言里的 `system.current_time` 更新为供应商别名 `system_current_time_2a9c83b2`；返回工具调用的 mock 响应也使用供应商别名，向上层断言继续使用内部名 `system.current_time`。

运行：

```bash
python3 -m unittest backend.tests.test_openai_compatible -v
```

预期：全部 `OK`，且基本无工具请求测试仍断言 payload 中没有 `tools`、`tool_choice` 或 `reasoning_content`。

- [ ] **步骤 8：提交网关兼容变更**

```bash
git add backend/llm/openai_compatible.py backend/tests/test_openai_compatible.py
git commit -m "fix: 兼容 DeepSeek 工具调用协议"
```

### 任务 3：编排器回传思考内容且不持久化

**文件：**
- 修改：`backend/application/model_tools.py:76-141`
- 测试：`backend/tests/test_model_tool_orchestrator.py:174-272`
- 测试：`backend/tests/test_integration.py:363-463`

- [ ] **步骤 1：编写失败的编排回传测试**

在 `test_time_tool_result_is_returned_to_model` 的第一轮回复加入 `reasoning_content="private-reasoning-sentinel"`，并添加断言：

```python
assistant_message = gateway.requests[1].messages[-2]
self.assertEqual(assistant_message.role, ModelRole.ASSISTANT)
self.assertEqual(
    assistant_message.reasoning_content,
    "private-reasoning-sentinel",
)
self.assertIsNone(result.reply.reasoning_content)
```

再新增标准供应商兼容测试：

```python
async def test_tool_round_without_reasoning_content_keeps_existing_path(self):
    call = tool_call("call-without-reasoning")
    gateway = FakeGateway([
        ModelReply(tool_calls=[call], model="fake"),
        ModelReply(text="完成", model="fake"),
    ])
    service = AsyncMock()
    service.request.side_effect = lambda request: succeeded_view(request)

    await configured_orchestrator(gateway, service).run(base_request())

    assistant_message = gateway.requests[1].messages[-2]
    self.assertIsNone(assistant_message.reasoning_content)
```

- [ ] **步骤 2：运行编排测试并确认失败**

运行：

```bash
python3 -m unittest backend.tests.test_model_tool_orchestrator.ModelToolOrchestratorTests.test_time_tool_result_is_returned_to_model backend.tests.test_model_tool_orchestrator.ModelToolOrchestratorTests.test_tool_round_without_reasoning_content_keeps_existing_path -v
```

预期：第一个测试 `FAIL`，第二轮 assistant 消息的思考内容仍为 `None`。

- [ ] **步骤 3：实现局部消息回传**

只改编排器创建 assistant 工具消息的位置：

```python
messages.append(
    ModelMessage(
        role=ModelRole.ASSISTANT,
        content=None,
        reasoning_content=reply.reasoning_content,
        tool_calls=calls,
    )
)
```

不要把该字段加入 `ModelAttempt`、`ModelOrchestrationResult` 以外的新对象、日志或异常。

- [ ] **步骤 4：运行完整编排器测试**

运行：

```bash
python3 -m unittest backend.tests.test_model_tool_orchestrator -v
```

预期：全部 `OK`，包括 3 次模型请求上限、4 个工具调用上限、重复调用 ID 和工具权限来源测试。

- [ ] **步骤 5：扩展集成测试证明不落库和不出接口**

在 `test_desktop_chat_uses_model_time_tool_without_confirmation` 第一轮 `ModelReply` 加入测试哨兵，并在已有数据库检查后加入：

```python
self.assertNotIn("reasoning", response.json())
self.assertNotIn("private-reasoning-sentinel", response.text)
self.assertEqual(
    self.llm.requests[1].messages[-2].reasoning_content,
    "private-reasoning-sentinel",
)
with closing(sqlite3.connect(self.store.database_path)) as connection:
    database_dump = "\n".join(connection.iterdump())
self.assertNotIn("private-reasoning-sentinel", database_dump)
```

这同时证明字段只存在于 FakeGateway 捕获的第二轮内存请求，不进入消息、模型调用、工具请求、审计或 HTTP 响应。

- [ ] **步骤 6：运行集成测试确认通过**

运行：

```bash
python3 -m unittest backend.tests.test_integration.ApiIntegrationTests.test_desktop_chat_uses_model_time_tool_without_confirmation -v
```

预期：`OK`；HTTP 200，工具状态 `succeeded`，模型调用记录为两次，SQLite dump 不含测试哨兵。

- [ ] **步骤 7：提交编排和隐私边界变更**

```bash
git add backend/application/model_tools.py backend/tests/test_model_tool_orchestrator.py backend/tests/test_integration.py
git commit -m "fix: 回传工具轮思考上下文"
```

### 任务 4：全量回归与真实 DeepSeek 验收

**文件：**
- 修改：`docs/superpowers/plans/2026-08-11-deepseek-tool-calling-compat.md`

- [ ] **步骤 1：执行静态检查和全量后端测试**

运行：

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
node --check backend/settings/static/settings.js
```

预期：命令退出码均为 0，后端测试全部 `OK`。

- [ ] **步骤 2：执行桌面端回归测试**

运行：

```bash
npm --prefix desktop-app run test:unit
npm --prefix desktop-app test
```

预期：单元测试全部通过，renderer 构建以及 `main.js`、`preload.js` 语法检查成功。

- [ ] **步骤 3：检查敏感字段只存在于允许的内存路径**

运行：

```bash
rg -n "reasoning_content" backend --glob '!tests/**'
```

预期：只命中 `backend/llm/models.py`、`backend/llm/openai_compatible.py` 和 `backend/application/model_tools.py`；不命中 `memory`、`infrastructure`、`api`、`settings`、`audio` 或日志代码。

- [ ] **步骤 4：准备真实验收但不读取秘密**

确认本机设置页 `http://127.0.0.1:8080/settings` 中模型连接测试成功，开启“允许模型调用工具”并保存。只观察开关、保存成功状态和重启提示；不读取、复制、打印或记录 API Key。

重启后端后，只通过 `/api/settings/config` 的脱敏布尔状态或运行时行为确认工具调用开关已启用，不输出配置正文中的任何凭据字段。

- [ ] **步骤 5：用低风险当前时间工具完成真实闭环**

向 `POST /api/chat/message` 发送新的桌面消息 ID 和“请调用工具读取上海当前时间，只回复最终时间。”。验收过程中只记录：

```text
HTTP 状态：200
工具：system.current_time
工具状态：succeeded
模型请求次数：2
最终回复：包含可识别的当前时间
```

不得抓取或显示请求 Authorization、API Key、供应商响应正文、`reasoning_content` 或完整调试日志。若失败，只记录应用归一化错误码和 HTTP 状态，再回到对应本地测试定位。

- [ ] **步骤 6：记录验收结果并提交**

在本计划末尾增加“完成记录”，仅写测试数量、命令退出状态、上述四项脱敏结果以及验证日期；不得写入供应商原始请求或响应。

```bash
git add docs/superpowers/plans/2026-08-11-deepseek-tool-calling-compat.md
git commit -m "docs: 记录 DeepSeek 工具调用验收"
```

- [ ] **步骤 7：检查最终差异和提交历史**

运行：

```bash
git status --short
git diff main...HEAD --check
git log --oneline main..HEAD
```

预期：工作区干净，`git diff --check` 无输出，分支包含设计、模型契约、网关兼容、编排回传和验收记录提交。
