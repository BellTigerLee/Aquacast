"""Dependency-free local RAG helper for Aquacast LLM prompts."""

from __future__ import annotations

from pathlib import Path
import re


DEFAULT_GUIDELINES = [
    "If unionized ammonia/NH3 rises toward 0.05 mg/L, reduce feeding and check biofilter performance.",
    "If dissolved oxygen drops below the safe band, increase oxygen injection, aeration, and circulation immediately.",
    "If nitrate keeps rising, plan partial water exchange and check denitrification capacity.",
    "High pH increases ammonia toxicity, so ammonia events should be evaluated together with pH.",
    "Rising CO2 usually indicates insufficient degassing or circulation; inspect flow and aeration paths.",
]


def build_rag_context(
    query: str,
    *,
    manuals_path: str | None,
    top_k: int = 3,
    max_chars: int = 3500,
) -> str:
    path = _resolve_path(manuals_path)
    text = _read_text(path)
    chunks = _split_chunks(text)
    selected = _rank_chunks(query, chunks, max(1, int(top_k)))

    guideline_context = "[built-in RAS control guidelines]\n" + "\n".join(f"- {line}" for line in DEFAULT_GUIDELINES)
    if selected:
        selected.append(guideline_context)
    else:
        selected = [guideline_context]

    context = "\n\n".join(selected).strip()
    if len(context) > max_chars:
        context = context[:max_chars].rsplit(" ", 1)[0].strip()
    source = str(path) if path else "built-in guidelines"
    return f"[Aquacast local RAG source: {source}]\n{context}"


def _resolve_path(path_value: str | None) -> Path | None:
    value = str(path_value or "").strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _read_text(path: Path | None) -> str:
    if path is None or not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def _split_chunks(text: str) -> list[str]:
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        if current and current_len + len(paragraph) > 1200:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _rank_chunks(query: str, chunks: list[str], top_k: int) -> list[str]:
    query_terms = _terms(query)
    if not query_terms:
        return chunks[:top_k]
    scored = []
    for index, chunk in enumerate(chunks):
        terms = _terms(chunk)
        score = len(query_terms & terms)
        if score:
            scored.append((score, -index, chunk))
    scored.sort(reverse=True)
    return [chunk for _score, _index, chunk in scored[:top_k]]


def _terms(text: str) -> set[str]:
    terms = set()
    for term in re.findall(r"[0-9A-Za-z가-힣_]+", str(text).lower()):
        if len(term) >= 2:
            terms.add(term)
    return terms
