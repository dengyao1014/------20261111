from __future__ import annotations

import json
import logging
import os
import re
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError, field_validator

from .models import Candidate
from .websearch import format_results

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是给独立开发者做产品机会分析的分析师。输入是一个社区论坛帖子（标题 + 正文 + 部分回复），任务是从中提取一个"可以被独立开发者做成付费产品"的需求；如果帖子不构成这种需求，也要如实标出。

判断规则（必须严格执行）：
1. 痛点必须来自用户自己的抱怨或请求，不能是分析者替用户想象出来的。
2. quote 必须从输入的原文里逐字复制一句英文原句（不能改写、不能翻译、不能拼接）。如果找不到能代表痛点的原句，把 is_pain_point 设为 false。
3. 如果这是某个免费/开源产品的 bug、崩溃、兼容性问题或很小的功能缺失，且没有人表达付费意愿，把 is_pain_point 设为 false —— 这类问题应该给项目提 issue，不是产品机会。
4. 已有免费、成熟、容易获得的替代方案时，competition_gap 必须不高于 3。判断时参考输入里的「联网检索结果」：如果出现多个同类商业产品或成熟免费方案，competition_gap 不高于 3；如果只有零散讨论或个人项目，可给 5-7；没有结果也不要因此给高分。existing_solutions 只能填帖子里或检索结果中真实出现的产品名。
5. 所有 0-10 分要保守校准：
   - 9-10：有明确付费证据 + 多个用户 + 没有像样的竞品
   - 6-8：有真实痛点，但证据有限或存在竞品
   - 3-5：普通的功能请求 / 使用问题
   - 0-2：几乎不构成需求
   大多数帖子的分数应该落在 3-6 之间。不要为了显得有用而给高分。
6. paid_evidence 只能引用原文，没有就填空字符串，禁止编造。
7. existing_solutions 只列帖子里真实提到的产品；没提到就给空数组。
8. 除 quote、title_en 外，所有文本用简体中文。

字段含义：
- title_en：一句英文需求标题，像一句话产品需求
- problem_zh：3 句话，说明问题是什么、影响谁、用户现在怎么绕过
- analysis_zh：2-3 句，基于帖子里的具体证据说明为什么这是真需求（用户规模、重复出现、已有付费、变通方案）
- opportunity_zh：给独立开发者的产品机会，一句话说清"做什么 + 怎么收钱"
- business_model：subscription / one-time / usage-based / ads / open-core 之一，可加一句说明
- user_segment：受影响用户
- workaround：用户当前的替代做法
- demand_kind：feature_gap / bug / missing_integration / usability / pricing / docs / other
- severity：痛点严重程度（丢钱 / 停工 / 数据风险最高）
- frequency：问题出现频率与受影响人数（结合回复数、浏览量）
- pay：付费意愿强度（不是"帖子里有没有明说要付钱"）。满足以下任意两条给 4-6 分：用户是商家/团队/企业而不是个人爱好者；问题直接影响收入、订单、合规或客户信任；用户已经在为同类工具或服务付费；问题反复出现且每周耗费数小时。如果用户是商家/团队/企业，且问题影响生产、收入或合规，pay 至少给 4（除非帖子里明确说只是个人爱好）。有明确付费证据（已付费、提到预算、愿意买）给 7-10；个人用户的免费需求、纯体验问题给 0-2。
- competition_gap：竞争空白程度
- buildable：独立开发者 4-8 周能否做出可用 MVP
- channel：有没有现成的分发渠道（App Store / 插件市场 / 社区）
- buildable_by_solo_dev：yes / maybe / no
- why_not_buildable：不能做的话，一句话说清原因
- action_plan：4 条具体行动步骤（验证 → MVP → 上线 → 推广），每条一句话"""

# OpenAI 兼容接口没有 schema 强约束，把 schema 塞进 system prompt
JSON_FORMAT_PROMPT = """只输出一个 JSON 对象，不要输出 markdown 代码块、前言或解释。JSON 必须严格符合下面的 schema，所有字段必填：

{schema}"""


class Demand(BaseModel):
    is_pain_point: bool = Field(description="是否是真实、可被独立开发者解决的软件需求")
    confidence: float = Field(ge=0, le=1)
    quote: str = Field(description="从帖子原文逐字摘录的一句英文原句，禁止改写或翻译")
    title_en: str
    problem_zh: str
    analysis_zh: str
    user_segment: str
    workaround: str
    paid_evidence: str
    demand_kind: Literal[
        "feature_gap", "bug", "missing_integration", "usability", "pricing", "docs", "other"
    ]
    existing_solutions: list[str]
    opportunity_zh: str
    business_model: str
    category: str
    severity: int = Field(ge=0, le=10)
    frequency: int = Field(ge=0, le=10)
    pay: int = Field(ge=0, le=10)
    competition_gap: int = Field(ge=0, le=10)
    buildable: int = Field(ge=0, le=10)
    channel: int = Field(ge=0, le=10)
    buildable_by_solo_dev: Literal["yes", "maybe", "no"]
    why_not_buildable: str
    action_plan: list[str]

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @field_validator(
        "severity", "frequency", "pay", "competition_gap", "buildable", "channel",
        mode="before",
    )
    @classmethod
    def _clamp(cls, value: int) -> int:
        return max(0, min(10, int(value)))


def build_prompt(candidate: Candidate) -> str:
    body = candidate.body[:6000]
    truncated = "\n[正文已截断]" if len(candidate.body) > 6000 else ""
    replies = "\n\n".join(
        f"[回复 {i}] {text}" for i, text in enumerate(candidate.replies[:6], 1)
    )
    search_results = candidate.meta.get("search_results") or []
    web_block = (
        "\n联网检索结果（用于判断竞品，可能不准确）：\n"
        + format_results(search_results)
        + "\n"
        if search_results
        else ""
    )
    return f"""来源：{candidate.platform} 社区
浏览量：{candidate.meta.get('views', 0)}　回复数：{candidate.meta.get('posts_count', 0)}
标题：{candidate.title}

正文：
{body}{truncated}

{('回复摘录：\n' + replies[:4000]) if replies else ''}
{web_block}
请严格按 schema 输出分析结果。"""


def _norm(text: str) -> str:
    text = (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("—", "-")
        .replace("–", "-")
        .replace("​", "")
    )
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in_source(quote: str, candidate: Candidate) -> bool:
    """校验 quote 确实是原文子串，防止模型改写或编造。"""
    needle = _norm(quote.strip().strip('"').strip("'").rstrip("…").rstrip("..."))
    if len(needle) < 12:
        return False
    haystack = _norm(
        "\n".join([candidate.title, candidate.body, *candidate.replies])
    )
    return needle in haystack


DUPLICATE_SYSTEM = """你是产品需求去重助手。输入是一批从论坛抽取出来的需求，每条有下标、标题、用户原话和问题描述。
请找出"其实是同一个产品机会"的条目，返回它们的下标分组。

规则：
1. 只有核心问题和受影响用户都相同时才算重复；只是主题相近不算。
2. 不同平台上描述同一个问题（例如多个论坛都在找 Stocky 替代品）应当合并。
3. 同一平台上的不同问题不要合并。
4. 每组至少 2 个下标；每个下标最多出现在一个组里。
5. 没有重复就返回 {"groups": []}。
6. 示例："Automatically monitor Google/Meta product feed errors" 和 "Google Merchant Center feed diagnostics and silent-disapproval alerting" 是同一个机会，应合并。
7. 只输出 JSON，不要解释。"""


class DuplicateGroups(BaseModel):
    groups: list[list[int]] = Field(default_factory=list)


def build_duplicate_prompt(entries: list[dict]) -> str:
    lines = []
    for index, entry in enumerate(entries):
        lines.append(
            f"[{index}] {entry['title']}\n"
            f"    用户原话：{entry['quote'][:200]}\n"
            f"    问题：{entry['problem'][:200]}"
        )
    return "需求列表：\n\n" + "\n\n".join(lines) + "\n\n请找出重复分组。"


def validate_groups(groups: list[list[int]], size: int) -> list[list[int]]:
    seen: set[int] = set()
    result: list[list[int]] = []
    for group in groups or []:
        valid = []
        for index in group:
            if isinstance(index, int) and 0 <= index < size and index not in seen:
                valid.append(index)
                seen.add(index)
        if len(valid) >= 2:
            result.append(valid)
    return result


class BaseExtractor:
    """两种 provider 共用的流程：抽取 → quote 校验 → 失败重试一次。"""

    def group_duplicates(self, entries: list[dict]) -> list[list[int]]:
        return []

    model: str

    def _call(self, prompt: str) -> Demand | None:
        raise NotImplementedError

    def extract(self, candidate: Candidate) -> Demand | None:
        prompt = build_prompt(candidate)
        demand = self._call(prompt)
        if demand is None:
            return None

        if demand.is_pain_point and not quote_in_source(demand.quote, candidate):
            retry_prompt = (
                prompt
                + "\n\n注意：你上一次摘录的句子不在原文中："
                + repr(demand.quote)
                + "。请重新逐字摘录一句原文；如果找不到，把 is_pain_point 设为 false。"
            )
            demand = self._call(retry_prompt)
            if demand is None:
                return None
            if demand.is_pain_point and not quote_in_source(demand.quote, candidate):
                log.warning("  ! quote 两次校验失败，丢弃：%s", candidate.title[:60])
                return None

        return demand


class AnthropicExtractor(BaseExtractor):
    def __init__(self, model: str) -> None:
        self.model = model
        self.client = anthropic.Anthropic()
        self.system = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _call(self, prompt: str) -> Demand | None:
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=6000,
                system=self.system,
                messages=[{"role": "user", "content": prompt}],
                output_format=Demand,
            )
        except anthropic.APIError as exc:
            log.warning("  ! Claude API 错误: %s", exc)
            return None
        except ValueError as exc:
            log.warning("  ! 结构化输出解析失败: %s", exc)
            return None
        return response.parsed_output

    def group_duplicates(self, entries: list[dict]) -> list[list[int]]:
        if len(entries) < 2:
            return []
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=2000,
                system=DUPLICATE_SYSTEM,
                messages=[
                    {"role": "user", "content": build_duplicate_prompt(entries)}
                ],
                output_format=DuplicateGroups,
            )
        except (anthropic.APIError, ValueError) as exc:
            log.warning("  ! 需求去重调用失败: %s", exc)
            return []
        groups = response.parsed_output.groups if response.parsed_output else []
        return validate_groups(groups, len(entries))


class DeepSeekExtractor(BaseExtractor):
    def __init__(self, model: str) -> None:
        from openai import OpenAI

        self.model = model
        self.client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        schema = json.dumps(Demand.model_json_schema(), ensure_ascii=False, indent=2)
        self.system = SYSTEM_PROMPT + "\n\n" + JSON_FORMAT_PROMPT.format(schema=schema)

    def _call(self, prompt: str) -> Demand | None:
        import openai

        messages = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": prompt},
        ]
        # deepseek-reasoner 不支持 temperature
        extra = {} if "reasoner" in self.model else {"temperature": 0.3}
        content = ""

        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=4000,
                    **extra,
                )
                content = response.choices[0].message.content or ""
                return _parse_demand(content)
            except ValidationError as exc:
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": f"上面的输出不能通过校验：{exc}\n请修正后只输出合法 JSON。",
                        },
                    ]
                    continue
                log.warning("  ! DeepSeek JSON 校验失败: %s", exc)
                return None
            except openai.OpenAIError as exc:
                log.warning("  ! DeepSeek API 错误: %s", exc)
                return None

        return None

    def group_duplicates(self, entries: list[dict]) -> list[list[int]]:
        if len(entries) < 2:
            return []
        import openai

        schema = json.dumps(DuplicateGroups.model_json_schema(), ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": DUPLICATE_SYSTEM + "\n\n只输出 JSON，schema：\n" + schema,
            },
            {"role": "user", "content": build_duplicate_prompt(entries)},
        ]
        extra = {} if "reasoner" in self.model else {"temperature": 0.0}
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=2000,
                **extra,
            )
            raw = response.choices[0].message.content or "{}"
            groups = json.loads(raw).get("groups", [])
        except (openai.OpenAIError, ValueError) as exc:
            log.warning("  ! 需求去重调用失败: %s", exc)
            return []
        return validate_groups(groups, len(entries))


def _parse_demand(raw: str) -> Demand:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return Demand.model_validate_json(text)


def build_extractor(provider: str, model: str) -> BaseExtractor:
    if provider == "deepseek":
        return DeepSeekExtractor(model)
    if provider == "anthropic":
        return AnthropicExtractor(model)
    raise ValueError(f"未知 provider: {provider}，只支持 anthropic / deepseek")
