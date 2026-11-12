"""
ИИ-клинический фармаколог — мультиагентная проверка лекарственной схемы.

Источники: RxNorm + DailyMed (NLM), FAERS через openFDA (FDA).
Учебный проект. Не является медицинской рекомендацией.
"""

import itertools

import pandas as pd
import streamlit as st

from agents import (DDIEngine, DailyMedAgent, FAERSAgent, FAERSUnavailable,
                    RxNormAgent, SEVERITY_ORDER)
from translator import RU_SECTION_TITLES, to_ru

st.set_page_config(page_title="ИИ-клинический фармаколог",
                   page_icon="💊", layout="wide")

SEVERITY_STYLE = {
    "ПРОТИВОПОКАЗАНО": "🔴 ПРОТИВОПОКАЗАНО",
    "ВЫСОКИЙ РИСК": "🟠 ВЫСОКИЙ РИСК",
    "СРЕДНИЙ РИСК": "🟡 СРЕДНИЙ РИСК",
    "НИЗКИЙ РИСК": "🟢 НИЗКИЙ РИСК",
    "НЕ ОБНАРУЖЕНО": "⚪ не обнаружено в лейблах",
}

st.title("💊 ИИ-клинический фармаколог")
st.markdown(
    "Мультиагентная проверка лекарственной схемы: нормализация препаратов "
    "(**RxNorm**), официальные лейблы FDA с анализом взаимодействий "
    "(**DailyMed**), сигналы безопасности из базы фармаконадзора "
    "(**FAERS / openFDA**)."
)

with st.sidebar:
    st.header("Схема лечения")
    raw = st.text_area(
        "По одному препарату на строку (латиницей, например "
        "warfarin, aspirin, metformin):",
        value="warfarin\naspirin\nmetformin",
        height=140,
    )
    run = st.button("Проверить схему", type="primary", use_container_width=True)
    st.divider()
    st.caption("Учебный проект. Результат — не медицинская рекомендация. "
               "Данные: NLM RxNorm/DailyMed, FDA FAERS.")

if not run:
    st.info("Введите препараты слева и нажмите «Проверить схему».")
    st.stop()

drug_names = [ln.strip() for ln in raw.splitlines() if ln.strip()]
if len(drug_names) < 2:
    st.warning("Нужно минимум два препарата для анализа взаимодействий.")
    st.stop()

# ------------------------------------------------------------------
# Агент 1 — RxNorm
# ------------------------------------------------------------------
with st.status("Агент 1/4: нормализация названий (RxNorm)…",
               expanded=True) as status:
    rx_agent = RxNormAgent()
    identities = {}
    for name in drug_names:
        ident = rx_agent.resolve(name)
        identities[name] = ident
        if ident.found:
            st.write(f"✅ **{name}** → RxCUI {ident.rxcui}, "
                     f"действующее вещество: *{ident.ingredient or '—'}*")
        else:
            st.write(f"⚠️ **{name}** — {ident.note or 'не найден'}")
    status.update(label="Агент 1/4: нормализация завершена", state="complete")

norm_df = pd.DataFrame(
    [{"Ввод": i.query,
      "Найдено в RxNorm": "да" if i.found else "нет",
      "RxCUI": i.rxcui or "—",
      "Действующее вещество": i.ingredient or "—",
      "Примечание": i.note}
     for i in identities.values()]
)

# ------------------------------------------------------------------
# Агент 2 — DailyMed
# ------------------------------------------------------------------
with st.status("Агент 2/4: загрузка лейблов FDA (DailyMed)…",
               expanded=True) as status:
    dm_agent = DailyMedAgent()
    labels = {}
    for name, ident in identities.items():
        lbl = dm_agent.fetch(ident)
        labels[name] = lbl
        if lbl.found:
            st.write(f"✅ **{name}** — лейбл: {lbl.title[:80]} "
                     f"(обновлён {lbl.published})")
        else:
            st.write(f"⚠️ **{name}** — {lbl.note or 'лейбл не найден'}")
    status.update(label="Агент 2/4: лейблы загружены", state="complete")

# ------------------------------------------------------------------
# Агент 3 — FAERS (openFDA)
# ------------------------------------------------------------------
faers_ok = False
signals = {}
with st.status("Агент 3/4: сигналы фармаконадзора (FAERS)…",
               expanded=True) as status:
    faers_agent = FAERSAgent()
    try:
        for name, ident in identities.items():
            sig = faers_agent.signals(ident)
            signals[name] = sig
            if sig.ok:
                faers_ok = True
                st.write(f"✅ **{name}** — сообщений в FAERS: "
                         f"**{sig.total_reports:,}**".replace(",", " "))
            else:
                st.write(f"⚠️ **{name}** — {sig.note or 'нет данных'}")
        status.update(label="Агент 3/4: FAERS обработан", state="complete")
    except FAERSUnavailable as e:
        status.update(label="Агент 3/4: FAERS недоступен из этой сети",
                      state="error")
        st.warning(f"{e}")

# ------------------------------------------------------------------
# Агент 4 — DDI-движок
# ------------------------------------------------------------------
with st.status("Агент 4/4: перекрёстный анализ взаимодействий (DDI)…",
               expanded=True) as status:
    engine = DDIEngine(labels)
    results = []
    for a, b in itertools.combinations(drug_names, 2):
        r = engine.check_pair(a, b)
        results.append(r)
        if r.severity in ("ПРОТИВОПОКАЗАНО", "ВЫСОКИЙ РИСК"):
            st.write(f"🟠 **{a} + {b}** → {r.severity}")
        elif r.severity != "НЕ ОБНАРУЖЕНО":
            st.write(f"🟡 **{a} + {b}** → {r.severity}")
        else:
            st.write(f"⚪ {a} + {b} → явных упоминаний в лейблах нет")
    status.update(label="Агент 4/4: анализ завершён", state="complete")

# ------------------------------------------------------------------
# Отчёт
# ------------------------------------------------------------------
st.divider()
st.header("📋 Отчёт по схеме")

tab_pairs, tab_labels, tab_faers, tab_norm = st.tabs(
    ["Взаимодействия (DDI)", "Лейблы DailyMed", "Сигналы FAERS",
     "Нормализация RxNorm"])

with tab_pairs:
    if not results:
        st.write("Нет пар для анализа.")
    df_pairs = pd.DataFrame(
        [{"Пара": f"{r.drug_a} + {r.drug_b}",
          "Оценка": SEVERITY_STYLE[r.severity],
          "Фрагментов лейбла": len(r.snippets)}
         for r in results])
    st.dataframe(df_pairs, use_container_width=True, hide_index=True)

    worst = sorted(results,
                   key=lambda r: SEVERITY_ORDER.index(r.severity))
    for r in worst:
        if r.snippets:
            with st.expander(
                    f"{SEVERITY_STYLE[r.severity]} — {r.drug_a} + {r.drug_b}",
                    expanded=r.severity in ("ПРОТИВОПОКАЗАНО",
                                            "ВЫСОКИЙ РИСК")):
                for s in r.snippets:
                    st.caption(s)
                    if s.startswith("[") and "] " in s:
                        prefix, body = s.split("] ", 1)
                        body = body.strip("… .")
                    else:
                        prefix, body = "", s
                    ru = to_ru(body, max_chars=600)
                    if ru:
                        st.caption(f"Перевод{prefix}: {ru}")
                sources = [n for n in (r.drug_a, r.drug_b)
                           if n in r.checked_in]
                for n in sources:
                    if labels[n].url:
                        st.markdown(f"- лейбл [{n}]({labels[n].url})")

with tab_labels:
    for name, lbl in labels.items():
        if not lbl.found:
            st.subheader(name)
            st.warning(lbl.note or "лейбл не найден")
            continue
        st.subheader(f"{name} — {lbl.title[:70]}")
        st.caption(f"Обновлён: {lbl.published} · [открыть на DailyMed]"
                   f"({lbl.url})")
        secs = [(k, v) for k, v in lbl.sections.items()
                if k != "boxed_warning"]
        if "boxed_warning" in lbl.sections:
            bw = lbl.sections["boxed_warning"]
            bw_ru = to_ru(bw[:600], max_chars=600)
            st.error("**Предупреждение FDA (boxed warning):** "
                     + (bw_ru or bw[:600]))
        cols = st.columns(2)
        for i, (k, v) in enumerate(secs):
            with cols[i % 2]:
                with st.expander(RU_SECTION_TITLES.get(k, k)):
                    v_ru = to_ru(v[:2500], max_chars=1800)
                    if v_ru:
                        st.caption("**Перевод:** " + v_ru)
                        with st.expander("Оригинал (англ.)"):
                            st.caption(v[:2500])
                    else:
                        st.caption(v[:2500])
                        st.caption("_Перевод недоступен (лимит "
                                   "бесплатного API или сеть)_")

with tab_faers:
    if not faers_ok:
        st.info("Данные FAERS недоступны из этой сети (api.fda.gov "
                "блокируется провайдером). При развёртывании приложения на "
                "Streamlit Community Cloud этот раздел работает полностью.")
    for name, sig in signals.items():
        st.subheader(name)
        if sig.ok:
            st.metric("Сообщений о НЯ (все годы, FAERS)",
                      f"{sig.total_reports:,}".replace(",", " "))
            if sig.top_reactions:
                terms = []
                for t, _ in sig.top_reactions:
                    ru = to_ru(t, max_chars=120)
                    terms.append(f"{ru} ({t})" if ru else t)
                chart = pd.DataFrame(
                    {"Нежелательное явление": terms,
                     "Число сообщений": [c for _, c in sig.top_reactions]}
                ).set_index("Нежелательное явление")
                st.bar_chart(chart)
        else:
            st.caption(sig.note or "нет данных")

with tab_norm:
    st.dataframe(norm_df, use_container_width=True, hide_index=True)
    st.caption("RxNorm, NLM: rxnav.nlm.nih.gov")

st.divider()
st.caption("⚠️ Сервис учебный: вердикт «не обнаружено» не означает "
           "отсутствие риска — лейблы описывают не все возможные "
           "взаимодействия. Решения о назначении принимает врач.")
