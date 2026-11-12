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

# ------------------------------------------------------------------
# Фирменный стиль: сливово-фиолетовый + кремовый (как в презентации)
# ------------------------------------------------------------------
SEV_COLOR = {
    "ПРОТИВОПОКАЗАНО": "#A94438",
    "ВЫСОКИЙ РИСК": "#C08A3E",
    "СРЕДНИЙ РИСК": "#A98F2F",
    "НЕ ПРОВЕРЕНО": "#684765",
    "НИЗКИЙ РИСК": "#71855B",
    "НЕ ОБНАРУЖЕНО": "#9A938C",
}
SEV_ICON = {
    "ПРОТИВОПОКАЗАНО": "⛔",
    "ВЫСОКИЙ РИСК": "🔶",
    "СРЕДНИЙ РИСК": "⚠️",
    "НЕ ПРОВЕРЕНО": "🔮",
    "НИЗКИЙ РИСК": "✅",
    "НЕ ОБНАРУЖЕНО": "⚪",
}

st.markdown("""
<style>
  :root {
    --brand: #684765; --accent: #C9828B; --cream: #F7F3E8;
    --ink: #3A3340; --muted: #8C7F8A;
  }
  .stApp { background: var(--cream); }
  h1, h2, h3 { color: var(--brand) !important; }
  section[data-testid="stSidebar"] { background: #EFE9DC; }
  .brand-banner {
    background: linear-gradient(135deg, #684765 0%, #8A6B87 100%);
    border-radius: 14px; padding: 22px 28px; margin-bottom: 18px;
    color: #F7F3E8;
  }
  .brand-banner h1 { color: #F7F3E8 !important; font-size: 1.7rem; margin: 0; }
  .brand-banner p { color: #E5D9E4; margin: 6px 0 0 0; font-size: 0.95rem; }
  .sev-chip {
    display: inline-block; padding: 3px 12px; border-radius: 999px;
    color: #fff; font-weight: 700; font-size: 0.8rem; letter-spacing: 0.4px;
  }
  .ddi-card {
    background: #FFFFFF; border-radius: 12px; padding: 14px 18px;
    margin: 8px 0; border-left: 6px solid #ccc;
    box-shadow: 0 1px 3px rgba(60,45,60,0.12);
  }
  .ddi-card .pair { font-size: 1.05rem; font-weight: 700; color: var(--ink); }
  .ddi-card .meta { color: var(--muted); font-size: 0.82rem; margin-top: 2px; }
  .summary-strip { display: flex; gap: 10px; flex-wrap: wrap; margin: 6px 0 14px 0; }
  .sum-pill {
    background: #fff; border-radius: 10px; padding: 8px 14px;
    box-shadow: 0 1px 3px rgba(60,45,60,0.10); text-align: center; min-width: 92px;
  }
  .sum-pill .n { font-size: 1.5rem; font-weight: 800; }
  .sum-pill .t { font-size: 0.72rem; color: var(--muted); letter-spacing: 0.5px; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="brand-banner">
  <h1>💊 ИИ-клинический фармаколог</h1>
  <p>Мультиагентная проверка лекарственной схемы · официальные источники FDA и NLM ·
  RxNorm → DailyMed → FAERS → DDI</p>
</div>
""", unsafe_allow_html=True)

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
with st.status("Агент 1/4 · нормализация названий (RxNorm)…",
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
    status.update(label="Агент 1/4 · нормализация завершена", state="complete")

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
with st.status("Агент 2/4 · загрузка лейблов FDA (DailyMed)…",
               expanded=True) as status:
    dm_agent = DailyMedAgent()
    labels = {}
    for name, ident in identities.items():
        lbl = dm_agent.fetch(ident)
        # ключ — действующее вещество: поиск взаимодействий идёт
        # по каноническому имени, а не по исходному вводу
        key = (ident.ingredient or name).lower()
        if key in labels and labels[key].found and not lbl.found:
            st.write(f"⏭️ **{name}** — уже покрыт лейблом «{key}»")
            continue
        labels[key] = lbl
        if lbl.found:
            st.write(f"✅ **{name}** → *{key}* — лейбл: {lbl.title[:70]} "
                     f"(обновлён {lbl.published})")
        else:
            st.write(f"⚠️ **{name}** → *{key}* — {lbl.note or 'лейбл не найден'}")
    status.update(label="Агент 2/4 · лейблы загружены", state="complete")

# ------------------------------------------------------------------
# Агент 3 — FAERS (openFDA)
# ------------------------------------------------------------------
faers_ok = False
signals = {}
with st.status("Агент 3/4 · сигналы фармаконадзора (FAERS)…",
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
        status.update(label="Агент 3/4 · FAERS обработан", state="complete")
    except FAERSUnavailable as e:
        status.update(label="Агент 3/4 · FAERS недоступен из этой сети",
                      state="error")
        st.warning(f"{e}")

# ------------------------------------------------------------------
# Агент 4 — DDI-движок
# ------------------------------------------------------------------
with st.status("Агент 4/4 · перекрёстный анализ взаимодействий (DDI)…",
               expanded=True) as status:
    engine = DDIEngine(labels)
    results = []
    for a, b in itertools.combinations(list(labels.keys()), 2):
        r = engine.check_pair(a, b)
        results.append(r)
        ic = SEV_ICON[r.severity]
        if r.severity == "НЕ ОБНАРУЖЕНО":
            st.write(f"{ic} {a} + {b} — явных упоминаний в лейблах нет")
        else:
            st.write(f"{ic} **{a} + {b}** → {r.severity}")
    status.update(label="Агент 4/4 · анализ завершён", state="complete")

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
    else:
        # сводная лента: сколько пар каждого уровня
        counts = {s: sum(1 for r in results if r.severity == s)
                  for s in SEVERITY_ORDER if any(r.severity == s for r in results)}
        pills = "".join(
            f'<div class="sum-pill"><div class="n" style="color:{SEV_COLOR[s]}">'
            f"{SEV_ICON[s]} {n}</div>"
            f'<div class="t">{s}</div></div>'
            for s, n in counts.items())
        st.markdown(f'<div class="summary-strip">{pills}</div>',
                    unsafe_allow_html=True)

        if any(r.severity == "НЕ ПРОВЕРЕНО" for r in results):
            st.warning("🔮 Для части препаратов DailyMed не нашёл лейбл — чаще "
                       "всего это неамериканские бренды, БАДы и препараты вне "
                       "US-реестра. По таким парам взаимодействие НЕ проверялось: "
                       "«не проверено» ≠ «безопасно».")

        worst = sorted(results, key=lambda r: SEVERITY_ORDER.index(r.severity))
        for r in worst:
            col = SEV_COLOR[r.severity]
            n_snip = len(r.snippets)
            if n_snip:
                meta = (f"фрагментов лейбла: {n_snip} · источники: "
                        + ", ".join(r.checked_in))
            else:
                meta = "фрагментов нет"
            card = (
                f'<div class="ddi-card" style="border-left-color:{col}">'
                f'<span class="sev-chip" style="background:{col}">'
                f"{SEV_ICON[r.severity]} {r.severity}</span> "
                f'<span class="pair">{r.drug_a} + {r.drug_b}</span>'
                f'<div class="meta">{meta}</div></div>')
            st.markdown(card, unsafe_allow_html=True)

            if r.snippets:
                with st.expander("Фрагменты лейбла и перевод",
                                 expanded=r.severity in ("ПРОТИВОПОКАЗАНО",
                                                         "ВЫСОКИЙ РИСК")):
                    for s in r.snippets:
                        st.caption(s)
                        if s.startswith("[") and "] " in s:
                            prefix, body = s.split("] ", 1)
                            prefix = prefix.lstrip("[").strip()
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
            c1, c2 = st.columns([1, 3])
            with c1:
                st.metric("Сообщений о НЯ (все годы, FAERS)",
                          f"{sig.total_reports:,}".replace(",", " "))
            with c2:
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
