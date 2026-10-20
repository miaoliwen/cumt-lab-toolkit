#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
考试答案库未命中时的 AI 兜底
============================

设计原则：
  - **只兜底**：先查本地答案库（ID -> 题干），未命中才调用 AI；
  - **默认关闭**：命令行 `--ai` 或 GUI「AI兜底」勾选后才启用；
  - **凭据只读环境变量**，源码/日志里不出现 Key：
        AQZRUI_AI_KEY   （首选）
        OPENAI_API_KEY / DEEPSEEK_API_KEY（兼容）
    可选：
        AQZRUI_AI_BASE   默认 https://api.openai.com/v1
        AQZRUI_AI_MODEL  默认 gpt-4o-mini
        AQZRUI_AI_TIMEOUT
  - **接口兼容 OpenAI**：POST {base}/chat/completions；
  - **输出严格校验**：单选返回 A-F 单字母，多选返回 `A|C`，判断返回 Y/N；
    解析失败或格式不合法就放弃，交回调用方走原来的猜测逻辑；
  - **单独缓存**：AI 结果写入 ai_answers.json，不写进已核对的 answer_bank.json。

安全约束：
  - base URL 仅允许 http/https；
  - 发请求前校验 host，拒绝 localhost、环回、私有、保留地址（含 DNS 解析结果）。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
KEY_ENVS = ("AQZRUI_AI_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
LETTERS = "ABCDEF"
_DNS_OK: set[str] = set()


class AIError(Exception):
    pass


# ---------------------------------------------------------------------------
# 目标校验
# ---------------------------------------------------------------------------
def _forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_reserved
            or ip.is_link_local or ip.is_multicast or ip.is_unspecified)


def validate_public_url(url: str) -> None:
    """仅 http/https；拒绝 localhost / 环回 / 私有 / 保留地址（含 DNS 结果）。"""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise AIError(f"AI 接口仅允许 http/https，收到 {p.scheme!r}")
    host = (p.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise AIError("AI 接口缺少主机名")
    if (host in ("localhost", "localhost.localdomain", "0.0.0.0", "::", "::1")
            or host.endswith(".localhost") or host.endswith(".local")):
        raise AIError(f"拒绝本地/内网 AI 地址: {host}")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host in _DNS_OK:
            return
        port = p.port or (443 if p.scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as e:
            raise AIError(f"无法解析 AI 主机 {host}: {e}") from e
        for info in infos:
            addr = str(info[4][0]).split("%", 1)[0]
            try:
                rip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if _forbidden_ip(rip):
                raise AIError(f"AI 主机 {host} 解析到内网/保留地址 {addr}，已拒绝")
        _DNS_OK.add(host)
    else:
        if _forbidden_ip(ip):
            raise AIError(f"拒绝环回/私有/保留地址: {host}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class AIConfig:
    base_url: str
    model: str
    api_key: str
    timeout: int = 25
    max_calls: int = 30

    @classmethod
    def from_env(cls, base: Optional[str] = None, model: Optional[str] = None,
                 max_calls: Optional[int] = None,
                 timeout: Optional[int] = None) -> Optional["AIConfig"]:
        key = ""
        for name in KEY_ENVS:
            v = (os.environ.get(name) or "").strip()
            if v:
                key = v
                break
        if not key:
            return None
        try:
            tmo = int(timeout or os.environ.get("AQZRUI_AI_TIMEOUT") or 25)
        except ValueError:
            tmo = 25
        try:
            limit = int(max_calls or os.environ.get("AQZRUI_AI_MAX_CALLS") or 30)
        except ValueError:
            limit = 30
        return cls(
            base_url=(base or os.environ.get("AQZRUI_AI_BASE") or DEFAULT_BASE).strip(),
            model=(model or os.environ.get("AQZRUI_AI_MODEL") or DEFAULT_MODEL).strip(),
            api_key=key,
            timeout=tmo,
            max_calls=limit,
        )


# ---------------------------------------------------------------------------
# 题面提取与答案归一化
# ---------------------------------------------------------------------------
def _plain(s: Any) -> str:
    """题干/选项里的 HTML 和多余空白去掉，减少无意义 token。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s or ""))).strip()


def extract_question(q: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """从考试题目 dict 里取题干与选项（兼容 Caption/optionA 与 stem/optiona）。"""
    stem = ""
    for key in ("Caption", "caption", "stem", "Stem", "Question", "question", "Title"):
        v = q.get(key)
        if v not in (None, ""):
            stem = _plain(v)
            break
    opts: Dict[str, str] = {}
    for letter in LETTERS:
        for key in (f"option{letter}", f"Option{letter}",
                    f"option{letter.lower()}", letter, letter.lower()):
            v = q.get(key)
            if v not in (None, "") and not isinstance(v, (list, dict)):
                opts[letter] = _plain(v)
                break
    return stem, opts


def _letters_in(text: str) -> list[str]:
    return sorted(set(re.findall(r"[A-F]", (text or "").upper())))


def normalize_answer(raw: Any, section: str, options: Dict[str, str]) -> Optional[str]:
    """
    section: A=单选, B=多选, C=判断。
    返回平台提交串格式：A / B|C / Y|N。
    """
    section = (section or "A").upper()
    allowed = sorted(options) if options else list(LETTERS)

    if section == "C":
        if isinstance(raw, bool):
            return "Y" if raw else "N"
        s = str(raw).strip().lower()
        if s in ("y", "yes", "true", "正确", "对", "是", "t", "1"):
            return "Y"
        if s in ("n", "no", "false", "错误", "错", "否", "f", "0"):
            return "N"
        if "正确" in s or "对" in s:
            return "Y"
        if "错误" in s or "错" in s:
            return "N"
        return None

    if isinstance(raw, (list, tuple, set)):
        got = [str(x).strip().upper() for x in raw]
    else:
        got = _letters_in(str(raw))

    got = [x for x in got if len(x) == 1 and x in allowed]
    if not got:
        return None
    if section == "B":
        return "|".join(sorted(set(got)))
    return got[0]


def build_prompt(stem: str, options: Dict[str, str], section: str) -> Tuple[str, str]:
    type_name = {"A": "单项选择题", "B": "多项选择题", "C": "判断题"}.get(
        (section or "A").upper(), "选择题")
    lines = [f"题型：{type_name}", f"题干：{stem}"]
    for letter in sorted(options):
        lines.append(f"{letter}. {options[letter]}")
    if section == "B":
        rule = (f"从 {', '.join(sorted(options) or list('ABCD'))} 中选出所有正确选项，"
                'answer 用字母数组表示，例如 ["A","C"]。')
    elif section == "C":
        rule = "判断对错：正确输出 Y，错误输出 N。"
    else:
        rule = (f"从 {', '.join(sorted(options) or list('ABCD'))} 中选出唯一正确选项，"
                "answer 用单个字母表示。")
    user = ("\n".join(lines) + "\n\n" + rule +
            '\n只输出 JSON，不要解释：{"answer": "B", "confidence": 0.0}')
    system = ("你是实验室安全考试答题助手。只根据题干和选项作答；"
              "信息不足时给出最可能答案并降低 confidence。输出必须是 JSON。")
    return system, user


def _parse_model_json(text: str) -> Tuple[Any, Optional[float]]:
    """从模型输出里抠 JSON（容忍 ```json 围栏和前后废话）。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I | re.S).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj.get("answer"), _as_float(obj.get("confidence"))
    except ValueError:
        pass
    m = re.search(r"\{.*?\}", s, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj.get("answer"), _as_float(obj.get("confidence"))
        except ValueError:
            pass
    return s, None


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# AI 客户端
# ---------------------------------------------------------------------------
class AIAnswerer:
    def __init__(self, cfg: AIConfig,
                 log: Callable[[str], None] = print,
                 cache: Optional[Dict[str, str]] = None,
                 on_save: Optional[Callable[[Dict[str, str]], None]] = None) -> None:
        validate_public_url(cfg.base_url)
        self.cfg = cfg
        self.log = log
        self.cache: Dict[str, str] = dict(cache or {})
        self.on_save = on_save
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self._limit_logged = False

    @classmethod
    def from_env(cls, log: Callable[[str], None] = print,
                 base: Optional[str] = None, model: Optional[str] = None,
                 max_calls: Optional[int] = None,
                 cache: Optional[Dict[str, str]] = None,
                 on_save: Optional[Callable[[Dict[str, str]], None]] = None
                 ) -> Optional["AIAnswerer"]:
        cfg = AIConfig.from_env(base=base, model=model, max_calls=max_calls)
        if cfg is None:
            return None
        return cls(cfg, log=log, cache=cache, on_save=on_save)

    # ---------- 对外 ----------
    def answer(self, q: Dict[str, Any], section: str) -> Optional[str]:
        stem, options = extract_question(q)
        if not stem and not options:
            return None
        section = (section or "A").upper()
        key = self._key(section, stem, options)

        if key in self.cache:
            ans = normalize_answer(self.cache[key], section, options)
            if ans:
                self.cache_hits += 1
                return ans

        if self.calls >= self.cfg.max_calls:
            if not self._limit_logged:
                self.log(f"AI 兜底：本次调用上限 {self.cfg.max_calls} 次已用完，"
                         "其余未命中题走本地猜测")
                self._limit_logged = True
            return None

        system, user = build_prompt(stem, options, section)
        self.calls += 1
        raw = self._chat(system, user)
        if raw is None:
            self.failures += 1
            return None
        value, conf = _parse_model_json(raw)
        ans = normalize_answer(value, section, options)
        if not ans:
            self.failures += 1
            self.log(f"AI 兜底：返回格式不合法，已丢弃：{str(raw)[:80]}")
            return None

        self.cache[key] = ans
        if self.on_save:
            try:
                self.on_save(self.cache)
            except OSError as e:
                self.log(f"AI 缓存写入失败：{e}")
        qid = str(q.get("ID") or q.get("id") or "").strip()
        extra = f"（置信度 {conf:.2f}）" if conf is not None else ""
        self.log(f"AI 兜底 题{qid or '?'} -> {ans}{extra}")
        return ans

    def stats(self) -> Dict[str, Any]:
        return {"calls": self.calls, "cache_hits": self.cache_hits,
                "failures": self.failures,
                "tokens_in": self.tokens_in, "tokens_out": self.tokens_out}

    # ---------- 内部 ----------
    @staticmethod
    def _key(section: str, stem: str, options: Dict[str, str]) -> str:
        raw = json.dumps({"s": section, "q": stem, "o": options},
                         ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _chat(self, system: str, user: str) -> Optional[str]:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": 120,
        }
        headers = {
            "Authorization": "Bearer " + self.cfg.api_key,
            "Content-Type": "application/json",
        }
        last = ""
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, headers=headers,
                                  timeout=self.cfg.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                j = r.json()
                usage = j.get("usage") or {}
                self.tokens_in += int(usage.get("prompt_tokens") or 0)
                self.tokens_out += int(usage.get("completion_tokens") or 0)
                return str(j["choices"][0]["message"]["content"] or "").strip()
            except (requests.RequestException, KeyError, IndexError, ValueError) as e:
                last = f"{type(e).__name__}: {e}"
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        self.log(f"AI 请求失败：{last}")
        return None
