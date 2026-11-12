"""
Перевод английских текстов лейблов на русский (учебный режим).

Бэкенд: бесплатный API MyMemory (api.mymemory.translated.net),
без ключей. Анонимный лимит — около 5000 символов в сутки на IP,
поэтому перевод идёт порциями с кэшем на диске: повторные запуски
и одинаковые фрагменты не тратят лимит.

При исчерпании лимита или недоступности API функция возвращает None,
и интерфейс показывает оригинал на английском — приложение не падает.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

API = "https://api.mymemory.translated.net/get"
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "translate_cache.json")
DAILY_BUDGET = 4500          # символов в сутки (запас до лимита API)
BLOCK = 450                  # символов за один запрос

_cache: dict | None = None
_quota_exhausted = False


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            _cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        _cache = {"texts": {}, "used": 0, "date": ""}
    if _cache.get("date") != str(date.today()):
        _cache["used"] = 0
        _cache["date"] = str(date.today())
    return _cache


def _save() -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except OSError:
        pass


def _translate_block(text: str) -> str | None:
    """Один запрос к API. None — лимит исчерпан или сеть недоступна."""
    global _quota_exhausted
    if _quota_exhausted:
        return None
    try:
        import requests
        r = requests.get(API,
                           params={"q": text, "langpair": "en|ru"},
                           timeout=30)
        data = r.json()
    except Exception:
        return None
    out = (data.get("responseData") or {}).get("translatedText") or ""
    # API сообщает об исчерпании квоты прямо в тексте ответа
    if not out or "MYMEMORY" in out.upper() and "WARNING" in out.upper():
        _quota_exhausted = True
        return None
    _load()["used"] += len(text)
    _save()
    return out


def _split_blocks(text: str, size: int = BLOCK) -> list[str]:
    """Разбивает текст на блоки по границам предложений."""
    sentences = re.split(r"(?<=[.!?;:\n])\s+", text)
    blocks, cur = [], ""
    for s in sentences:
        if not s.strip():
            continue
        if len(cur) + len(s) + 1 > size and cur:
            blocks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        blocks.append(cur)
    return blocks


def to_ru(text: str, max_chars: int = 1200) -> str | None:
    """
    Переводит текст на русский. Возвращает None, если перевести
    не удалось (квота/сеть) — тогда показываем оригинал.
    Длинные тексты переводятся с начала до max_chars.
    """
    text = (text or "").strip()
    if not text:
        return None
    c = _load()
    if text in c["texts"]:
        return c["texts"][text]

    head, tail = text[:max_chars], text[max_chars:]
    result_parts = []
    remaining = DAILY_BUDGET - c["used"]
    for block in _split_blocks(head):
        if len(block) > remaining:
            break
        ru = _translate_block(block)
        if ru is None:
            break
        result_parts.append(ru)
        remaining = DAILY_BUDGET - c["used"]
    if not result_parts:
        return None
    result = " ".join(result_parts).strip()
    if tail:
        result += " … [далее — оригинал ниже]"
    c["texts"][text] = result
    _save()
    return result


RU_SECTION_TITLES = {
    "boxed_warning": "Предупреждение FDA (boxed warning)",
    "contraindications": "Противопоказания",
    "warnings": "Предупреждения",
    "precautions": "Меры предосторожности",
    "interactions": "Лекарственные взаимодействия",
    "adverse_reactions": "Побочные действия",
    "dosage": "Дозировка и способ применения",
}
