"""
Агенты мультиагентной системы «ИИ-клинический фармаколог».

Источники:
  - RxNorm (NLM, RxNav API)   — нормализация названий лекарств
  - DailyMed (NLM, REST API)  — официальные лейблы FDA (SPL): взаимодействия,
                                противопоказания, дозировки, предупреждения
  - openFDA / FAERS (FDA)     — сигналы фармаконадзора: нежелательные явления
                                из базы FAERS

Ограничение: сервис — учебный, не является медицинской рекомендацией.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

HEADERS = {"User-Agent": "DDI-Pharmacist/1.0 (educational project)"}
TIMEOUT = 30

RXNORM_BASE = "https://rxnav.nlm.nih.gov/REST"
DAILYMED_BASE = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
OPENFDA_BASE = "https://api.fda.gov/drug/event.json"

# Секции лейбла, которые мы извлекаем (ключ -> regex для поиска по заголовку)
SECTION_PATTERNS = {
    "boxed_warning": r"boxed",
    "contraindications": r"contraindications?",
    "warnings": r"warnings?",
    "precautions": r"precautions?",
    "interactions": r"drug interactions?",
    "adverse_reactions": r"adverse reactions?",
    "dosage": r"dosage and administration",
}

# Стоп-слова для очистки XML-текста
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# Агент 1. RxNorm — нормализация названий лекарственных средств
# --------------------------------------------------------------------------

@dataclass
class DrugIdentity:
    query: str
    rxcui: str | None = None
    name: str = ""          # каноническое название из RxNorm
    ingredient: str = ""    # действующее вещество (TTY=IN)
    found: bool = False
    note: str = ""


class RxNormAgent:
    """Переводит пользовательский ввод в канонические RxCUI и INCI (ingredient)."""

    name = "Агент нормализации (RxNorm)"

    @staticmethod
    def _variants(query: str) -> list[str]:
        """Варианты написания: латинские INN ('enoxaparinum natrium')
        превращаем в английские ('enoxaparin sodium'), без которых
        RxNorm отдаёт пустые иностранные концепты без ингредиента."""
        q = query.strip().lower()
        variants = [q]
        toks = q.split()
        if "natrium" in toks:
            variants.append(" ".join("sodium" if t == "natrium" else t
                                     for t in toks))
        stripped = [t[:-2] if t.endswith("um") and len(t) > 6 else t
                    for t in toks]
        if stripped != toks:
            variants.append(" ".join(stripped))
            if "natrium" in toks:
                variants.append(" ".join(
                    "sodium" if t == "natrium" else
                    (t[:-2] if t.endswith("um") and len(t) > 6 else t)
                    for t in toks))
        return list(dict.fromkeys(variants))

    def resolve(self, raw_name: str) -> DrugIdentity:
        ident = DrugIdentity(query=raw_name.strip())
        if not ident.query:
            ident.note = "пустое название"
            return ident

        # 1) собираем кандидатов по всем вариантам написания:
        # сначала точный поиск, потом приблизительный
        ids: list[str] = []
        for variant in self._variants(ident.query):
            try:
                r = _get(f"{RXNORM_BASE}/rxcui.json",
                         params={"name": variant})
                for rid in r.json().get("idGroup", {}).get("rxnormId") or []:
                    if rid not in ids:
                        ids.append(rid)
            except requests.RequestException:
                pass
            try:
                r = _get(f"{RXNORM_BASE}/approximateTerm.json",
                         params={"term": variant, "maxEntries": 5})
                for c in r.json().get("approximateGroup",
                                     {}).get("candidate") or []:
                    rid = c.get("rxcui")
                    if rid and rid not in ids:
                        ids.append(rid)
            except requests.RequestException:
                pass

        if not ids:
            ident.note = "не найдено в RxNorm"
            return ident

        # 3) перебираем кандидатов: берём первый, для которого
        # определяется действующее вещество (TTY=IN). Первый ID из
        # точного поиска бывает «неполноценным» (инородные INN-концепты),
        # поэтому вслепую ids[0] доверять нельзя.
        for rid in ids[:5]:
            ingredient = self._ingredient(rid)
            if ingredient:
                ident.rxcui = rid
                ident.ingredient = ingredient
                ident.found = True
                break

        if not ident.found:
            # крайний случай: ID есть, но вещество не определилось
            ident.rxcui = ids[0]
            ident.found = True
            ident.ingredient = ident.query.lower()
            ident.note = ("действующее вещество не определено, "
                          "поиск по исходному вводу")

        ident.name = ident.ingredient or ident.query
        return ident

    @staticmethod
    def _ingredient(rxcui: str) -> str:
        try:
            r = _get(f"{RXNORM_BASE}/rxcui/{rxcui}/related.json",
                     params={"tty": "IN"})
            groups = r.json().get("relatedGroup", {}).get("conceptGroup") or []
            for g in groups:
                props = g.get("conceptProperties") or []
                if props:
                    return props[0].get("name", "")
        except requests.RequestException:
            pass
        return ""


# --------------------------------------------------------------------------
# Агент 2. DailyMed — официальные лейблы препаратов (FDA SPL)
# --------------------------------------------------------------------------

@dataclass
class DrugLabel:
    query: str
    setid: str | None = None
    title: str = ""
    published: str = ""
    url: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    found: bool = False
    note: str = ""


class DailyMedAgent:
    """Ищет SPL-лейбл препарата и извлекает клинически значимые секции."""

    name = "Агент лейблов (DailyMed)"

    def fetch(self, ident: DrugIdentity) -> DrugLabel:
        label = DrugLabel(query=ident.query)
        search_name = ident.ingredient or ident.query
        try:
            r = _get(f"{DAILYMED_BASE}/spls.json",
                     params={"drug_name": search_name, "pagesize": 25})
        except requests.RequestException as e:
            label.note = f"ошибка запроса DailyMed: {e}"
            return label

        items = r.json().get("data") or []
        if not items:
            # повторим по исходному вводу (могли искать по торговому названию)
            try:
                r = _get(f"{DAILYMED_BASE}/spls.json",
                         params={"drug_name": ident.query, "pagesize": 25})
                items = r.json().get("data") or []
            except requests.RequestException as e:
                label.note = f"ошибка запроса DailyMed: {e}"
                return label

        if not items:
            label.note = "лейбл не найден в DailyMed"
            return label

        # Перебираем несколько кандидатов: первый результат — не всегда лучший
        # (может попасться БАД или комбинация «А + Б» без структурированных
        # секций). Предпочитаем лейбл, где название содержит действующее
        # вещество и реально извлекаются клинические секции.
        search_ing = (ident.ingredient or ident.query).lower()
        best: DrugLabel | None = None
        best_score = -1
        for item in items[:25]:
            cand = DrugLabel(query=ident.query)
            cand.setid = item.get("setid")
            cand.title = item.get("title", "")
            cand.published = item.get("published_date", "")
            cand.url = (f"https://dailymed.nlm.nih.gov/dailymed/"
                        f"drugInfo.cfm?setid={cand.setid}")
            try:
                rx = _get(f"{DAILYMED_BASE}/spls/{cand.setid}.xml")
                cand.sections = self._parse_sections(rx.content)
            except requests.RequestException:
                continue
            score = len(cand.sections)
            title_low = cand.title.lower().split("[")[0]
            if search_ing and search_ing in title_low:
                score += 3
            if " and " in title_low:  # комбинированный препарат — хуже
                score -= 2
            if score > best_score:
                best, best_score = cand, score
            if score >= 6 and " and " not in title_low:
                break  # достаточно богатый лейбл моно-препарата

        if best is None:
            label.note = "лейблы найдены, но не удалось загрузить SPL-документ"
            return label

        best.found = True
        return best

    @staticmethod
    def _parse_sections(xml_bytes: bytes) -> dict[str, str]:
        root = ET.fromstring(xml_bytes)
        sections: dict[str, str] = {}

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        def text_of(elem) -> str:
            txt = "".join(elem.itertext())
            return _WS_RE.sub(" ", txt).strip()

        for sec in root.iter():
            if local(sec.tag) != "section":
                continue
            title_el = None
            for child in sec:
                if local(child.tag) == "title":
                    title_el = child
                    break
            if title_el is None:
                continue
            title = (title_el.text or "").strip()
            for key, pattern in SECTION_PATTERNS.items():
                if key in sections:
                    continue
                if re.search(pattern, title, re.IGNORECASE):
                    sections[key] = f"### {title}\n" + text_of(sec)[:8000]
        return sections


# --------------------------------------------------------------------------
# Агент 3. FAERS через openFDA — сигналы фармаконадзора
# --------------------------------------------------------------------------

class FAERSUnavailable(Exception):
    """openFDA недоступен из этой сети (например, блокируется провайдером)."""


@dataclass
class SafetySignal:
    query: str
    total_reports: int | None = None
    top_reactions: list[tuple[str, int]] = field(default_factory=list)
    ok: bool = False
    note: str = ""


class FAERSAgent:
    """Топ нежелательных явлений по препарату из базы FAERS (openFDA API)."""

    name = "Агент фармаконадзора (FAERS / openFDA)"

    def signals(self, ident: DrugIdentity, limit: int = 8) -> SafetySignal:
        sig = SafetySignal(query=ident.query)
        name = (ident.ingredient or ident.query).upper()
        search = (
            f'(patient.drug.openfda.generic_name:"{name}" OR '
            f'patient.drug.medicinalproduct:"{name}")'
        )
        try:
            # общее число сообщений
            r = _get(OPENFDA_BASE, params={"search": search, "limit": 1})
            sig.total_reports = r.json().get("meta", {}).get("results",
                                                             {}).get("total")
            # топ реакций
            r = _get(OPENFDA_BASE, params={
                "search": search,
                "count": "patient.reaction.reactionmeddrapt.exact",
                "limit": limit,
            })
            terms = r.json().get("results") or []
            sig.top_reactions = [(t["term"], t["count"]) for t in terms]
            sig.ok = True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                sig.note = "по препарату нет сообщений в FAERS"
            elif e.response is not None and e.response.status_code in (403, 451):
                raise FAERSUnavailable(
                    "api.fda.gov недоступен из этой сети (HTTP "
                    f"{e.response.status_code}). Данные FAERS будут доступны "
                    "при развёртывании приложения на сервере вне блокирующей "
                    "сети, например на Streamlit Community Cloud."
                )
            else:
                sig.note = f"ошибка openFDA: {e}"
        except requests.RequestException as e:
            sig.note = f"сеть недоступна: {e}"
        return sig


# --------------------------------------------------------------------------
# Агент 4. DDI-движок — перекрёстный анализ взаимодействий
# --------------------------------------------------------------------------

SEVERITY_ORDER = ["ПРОТИВОПОКАЗАНО", "ВЫСОКИЙ РИСК", "СРЕДНИЙ РИСК",
                  "НЕ ПРОВЕРЕНО", "НИЗКИЙ РИСК", "НЕ ОБНАРУЖЕНО"]

_RULES: list[tuple[str, list[str]]] = [
    ("ПРОТИВОПОКАЗАНО", ["contraindicat"]),
    ("ВЫСОКИЙ РИСК", [
        "avoid", "not recommended", "should not", "potentially fatal",
        "serious and", "is not advised", "must not",
    ]),
    ("СРЕДНИЙ РИСК", [
        "caution", "monitor", "adjust the dose", "dose adjustment",
        "reduce the dose", "increased risk", "may increase", "may decrease",
        "may potentiate", "may enhance", "may reduce", "exercise",
        "risk of bleeding", "chance is higher", "increased chance",
        "chance of bleeding",
    ]),
]

_CONTEXT_WORDS = [
    "contraindicat", "avoid", "not recommended", "caution", "monitor",
    "increased risk", "may increase", "may decrease", "may potentiate",
    "may enhance", "interact", "co-administration", "coadministration",
    "concomitant", "serum concentration", "plasma concentration",
]

# Подсказки по фармакологическим классам: если вещество B по имени
# в лейбле A не упомянуто, ищем его класс (стандартные фармакологические
# факты). Повышает полноту: enoxaparin в лейблах часто фигурирует
# как «low molecular weight heparin», aspirin — как «antiplatelet».
CLASS_HINTS: dict[str, list[str]] = {
    "aspirin": ["antiplatelet", "anti-platelet",
                "platelet aggregation inhibitor"],
    "clopidogrel": ["antiplatelet", "platelet aggregation inhibitor"],
    "ticagrelor": ["antiplatelet", "platelet aggregation inhibitor"],
    "warfarin": ["anticoagulant", "vitamin k antagonist"],
    "enoxaparin": ["anticoagulant", "low molecular weight heparin", "lmwh",
                   "heparin"],
    "heparin": ["anticoagulant", "low molecular weight heparin"],
    "dabigatran": ["anticoagulant", "direct thrombin inhibitor"],
    "rivaroxaban": ["anticoagulant", "factor xa inhibitor"],
    "apixaban": ["anticoagulant", "factor xa inhibitor"],
    "amiodarone": ["antiarrhythmic", "cyp3a4 inhibitor", "cyp2c9 inhibitor"],
    "digoxin": ["p-glycoprotein inhibitor", "pgp inhibitor"],
    "metformin": [],
    "simvastatin": ["statin", "cyp3a4 substrate"],
    "atorvastatin": ["statin"],
    "sildenafil": ["pde5 inhibitor"],
}


@dataclass
class DDIResult:
    drug_a: str
    drug_b: str
    severity: str = "НЕ ОБНАРУЖЕНО"
    snippets: list[str] = field(default_factory=list)
    checked_in: list[str] = field(default_factory=list)


class DDIEngine:
    """Проверяет каждую пару препаратов по текстам лейблов DailyMed."""

    name = "DDI-движок (перекрёстный анализ)"

    def __init__(self, labels: dict[str, DrugLabel]):
        # drug_key -> объединённый текст секций для поиска
        self._texts: dict[str, str] = {}
        self._ingredients: dict[str, str] = {}
        for key, lbl in labels.items():
            parts = []
            for sec in ("boxed_warning", "contraindications", "warnings",
                        "precautions", "interactions"):
                if sec in lbl.sections:
                    parts.append(lbl.sections[sec])
            self._texts[key] = "\n".join(parts).lower()
            self._ingredients[key] = key.lower()

    @staticmethod
    def _snippets(text: str, needle: str, window: int = 400) -> list[str]:
        out, start = [], 0
        while True:
            i = text.find(needle, start)
            if i < 0:
                break
            # расширяем до границ предложения, чтобы оценка риска
            # опиралась на полный контекст, а не обрезанный кусок
            lo = text.rfind(". ", 0, i)
            lo = 0 if lo < 0 or i - lo > window else lo + 2
            if lo == 0:
                lo = max(0, i - window // 2)
            hi = text.find(". ", i + len(needle))
            hi = min(len(text), i + window) if hi < 0 else \
                min(hi + 1, i + window + 200)
            out.append(text[lo:hi].strip(" .,;:"))
            start = i + len(needle)
            if len(out) >= 3:
                break
        return out

    def check_pair(self, key_a: str, key_b: str) -> DDIResult:
        res = DDIResult(drug_a=key_a, drug_b=key_b)
        if key_a == key_b:
            return res

        # честный статус: если по одному из препаратов нет данных
        # лейбла — взаимодействие НЕ проверялось, а не «не найдено»
        if not self._texts.get(key_a) or not self._texts.get(key_b):
            res.severity = "НЕ ПРОВЕРЕНО"
            return res

        for from_key, other_key in ((key_a, key_b), (key_b, key_a)):
            text = self._texts.get(from_key, "")
            if not text:
                continue
            # сначала точное вещество, потом фармакологический класс
            needle = self._ingredients[other_key]
            matched_by = needle
            if not needle or needle not in text:
                hints = [h for h in CLASS_HINTS.get(other_key, [])
                         if h in text]
                if not hints:
                    continue
                needle = hints[0]
                matched_by = f"класс «{hints[0]}»"
            res.checked_in.append(from_key)
            found = self._snippets(text, needle)
            res.snippets.extend(f"[{from_key} · {matched_by}] …{s}…"
                                for s in found)

            joined = " ".join(res.snippets).lower()
            for severity, words in _RULES:
                if any(w in joined for w in words):
                    if (SEVERITY_ORDER.index(severity)
                            < SEVERITY_ORDER.index(res.severity)):
                        res.severity = severity
            if res.severity == "НЕ ОБНАРУЖЕНО":
                res.severity = "НИЗКИЙ РИСК"

        # дедупликация фрагментов
        res.snippets = list(dict.fromkeys(res.snippets))[:6]
        return res
