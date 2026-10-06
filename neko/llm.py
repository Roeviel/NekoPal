"""大模型适配层：DeepSeek / 任意 OpenAI 兼容接口。

统一走 `POST {base_url}/chat/completions`，所以 DeepSeek、OpenAI、
各种中转站、Ollama（OpenAI 兼容模式）都能直接用。

没有配 Key 时不抛异常，而是让 `available()` 返回 False，
上层可以降级到"离线模板回复"——保证程序永远能起来。
"""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from .config import load_config


class LLMError(Exception):
    """模型调用失败。"""


class LLM:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = cfg or load_config()
        conf = cfg.get("llm", {})
        self.base_url = str(conf.get("base_url", "https://api.deepseek.com")).rstrip("/")
        self.model = conf.get("model", "deepseek-chat")
        self.api_key = (conf.get("api_key") or "").strip()
        self.temperature = float(conf.get("temperature", 1.1))
        self.max_tokens = int(conf.get("max_tokens", 1024))
        self.timeout = float(conf.get("timeout", 90))
        self.cfg = cfg          # 计量需要，见 neko/meter.py
        self._client: httpx.Client | None = None

    # ---------- 基础 ----------

    @property
    def endpoint(self) -> str:
        # 有些人会把 base_url 写成 .../v1 或 .../v1/chat/completions，都兼容
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def available(self) -> bool:
        """能不能调模型。

        **这是总开关的落点**：`llm.enabled=false` 时这里返回 False，
        于是所有调用方（聊天、学习总结、测验出题）都会自动走各自的离线兜底，
        一次 API 请求都不会发出去。改一处就够，不用在每个调用点判断。
        """
        if not self.api_key:
            return False
        return bool((self.cfg.get("llm") or {}).get("enabled", True))

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=15.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "LLM":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        stream: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if stream:
            # 让流式也返回 usage（实测 DeepSeek 支持）。没有它就没法按 token
            # 精确统计"蕴自己花了多少"，只能退回用余额差 —— 而余额差会把
            # 同一个 key 在别处的消耗也算进来。
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ---------- 调用前的统一检查 ----------

    def _preflight(self) -> None:
        """调用前的三件事，一次查清并且**如实说明是哪一种**。

        1. 有没有 key
        2. 大模型总开关（设置里的那个）
        3. **每日额度** —— 这一条放在这里，学习/测验/定时自学就都绕不过去。
           之前它只写在两个聊天接口上，于是额度用完之后
           `learner.summarize()`（学习总结）和 `learner.quiz()`（测验出题）
           照样在调模型，用户反馈的"到达设定额度后没有进行措施"就是这个。
        """
        if not self.api_key:
            raise LLMError("未配置 llm.api_key，无法调用模型")
        if not bool((self.cfg.get("llm") or {}).get("enabled", True)):
            raise LLMError("大模型总开关已关闭（设置 → 用量与预算）")
        try:
            from . import meter
        except Exception:  # noqa: BLE001
            return
        ok, why = meter.check_cap(self.cfg)
        if not ok:
            raise LLMError(f"已达每日额度上限，本次不调用模型。（{why}）")

    # ---------- 非流式 ----------

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> str:
        """一次性拿到完整回复。"""
        self._preflight()          # key / 总开关 / 每日额度，一次查清
        payload = self._payload(
            messages, stream=False, temperature=temperature, max_tokens=max_tokens
        )
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._http().post(self.endpoint, json=payload)
                if resp.status_code == 401:
                    raise LLMError("API Key 无效或已过期（401）")
                if resp.status_code == 402:
                    raise LLMError("账户余额不足（402）")
                if resp.status_code == 429:
                    raise LLMError("请求过于频繁，被限流（429）")
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(f"模型返回为空：{json.dumps(data, ensure_ascii=False)[:300]}")
                # 记一笔用量。**花费按 token 估算，只算蕴自己发起的调用** ——
                # 用余额差会把同一个 key 在别处的消耗也算进来（用户反馈过）。
                # 记账绝不能影响这次回复，所以整段吞异常。
                _usage = data.get("usage") or {}
                try:
                    from . import meter

                    meter.record(
                        self.cfg,
                        prompt_tokens=_usage.get("prompt_tokens", 0),
                        completion_tokens=_usage.get("completion_tokens", 0),
                        cache_hit=_usage.get("prompt_cache_hit_tokens"),
                        cache_miss=_usage.get("prompt_cache_miss_tokens"),
                        model=self.model, source="chat",
                    )
                except Exception:  # noqa: BLE001
                    pass
                return (choices[0].get("message") or {}).get("content", "") or ""
            except LLMError:
                raise
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_error = exc
                if attempt < retries:
                    continue
        raise LLMError(f"调用模型失败：{last_error}")

    # ---------- 流式 ----------

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """逐段吐出增量文本（SSE）。"""
        self._preflight()          # key / 总开关 / 每日额度，一次查清

        payload = self._payload(
            messages, stream=True, temperature=temperature, max_tokens=max_tokens
        )
        usage: dict[str, Any] = {}
        try:
            with self._http().stream("POST", self.endpoint, json=payload) as resp:
                if resp.status_code == 401:
                    raise LLMError("API Key 无效或已过期（401）")
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        if line == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # 开了 stream_options 之后，最后一个 chunk 会带 usage
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
            # 流式也能拿到 usage（payload 里开了 stream_options）。
            # 这样"蕴自己花了多少"就是精确统计，不再依赖余额差。
            try:
                from . import meter

                meter.record(
                    self.cfg,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    cache_hit=usage.get("prompt_cache_hit_tokens"),
                    cache_miss=usage.get("prompt_cache_miss_tokens"),
                    model=self.model, source="chat_stream",
                )
            except Exception:  # noqa: BLE001
                pass
        except LLMError:
            raise
        except httpx.HTTPError as exc:
            raise LLMError(f"流式调用失败：{exc}") from exc

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
        **kw: Any,
    ) -> str | Iterator[str]:
        return self.chat_stream(messages, **kw) if stream else self.complete(messages, **kw)

    # ---------- 便利方法 ----------

    def ask(self, system: str, user: str, **kw: Any) -> str:
        return self.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], **kw
        )


if __name__ == "__main__":  # 自检：python -m neko.llm
    import sys

    llm = LLM()
    print(f"endpoint = {llm.endpoint}")
    print(f"model    = {llm.model}")
    print(f"可用     = {llm.available()}")
    if llm.available():
        print("测试回复:", llm.ask("你是一只猫娘，回答要短。", "用一句话打招呼"))
    else:
        print("未配置 api_key，跳过实际调用（这是预期行为）", file=sys.stderr)
