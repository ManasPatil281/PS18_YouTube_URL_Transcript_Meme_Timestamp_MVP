"""LangGraph based, transcript-only comedy discovery pipeline."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

_TOKEN_REQUESTS: deque[tuple[float, int]] = deque()
_LOG = logging.getLogger("ps18.agents")


def configure_logging():
    """Write concise rotating logs without API keys, transcript text, or prompts."""
    if _LOG.handlers:
        return
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(log_dir, "ps18.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    _LOG.addHandler(handler)
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False
    app_log = logging.getLogger("ps18.app")
    app_log.addHandler(handler)
    app_log.setLevel(logging.INFO)
    app_log.propagate = False


def _progress(agent: str, message: str, **details):
    writer = get_stream_writer()
    writer({"agent": agent, "message": message, **details})
    _LOG.info("agent=%s message=%s details=%s", agent, message, details)


class ClipState(TypedDict, total=False):
    rows: list[dict[str, Any]]
    language: str
    target_len: int
    max_candidates: int
    chunks: list[list[dict[str, Any]]]
    findings: list[dict[str, Any]]
    reaction_signals: list[dict[str, Any]]
    candidates: list[dict[str, Any]]


def _model():
    from langchain_groq import ChatGroq

    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    return ChatGroq(
        api_key=key,
        model=os.getenv("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile"),
        temperature=0.2,
        max_tokens=700,
        max_retries=2,
    )


def _paced_invoke(model, prompt: str):
    """Reserve estimated input + max output tokens under a rolling per-minute budget."""
    budget = max(1000, int(os.getenv("GROQ_TPM_BUDGET", "6500")))
    reservation = max(1, len(prompt) // 4) + 700
    if reservation > budget:
        raise ValueError("A single prompt exceeds GROQ_TPM_BUDGET; lower the transcript chunk size.")
    while True:
        now = time.monotonic()
        while _TOKEN_REQUESTS and now - _TOKEN_REQUESTS[0][0] >= 60:
            _TOKEN_REQUESTS.popleft()
        used = sum(count for _, count in _TOKEN_REQUESTS)
        if used + reservation <= budget:
            _TOKEN_REQUESTS.append((now, reservation))
            break
        time.sleep(max(0.5, 60 - (now - _TOKEN_REQUESTS[0][0]) + 0.1))
    return model.invoke(prompt)


def _chunk_rows(rows: list[dict[str, Any]], max_chars: int = 4200):
    """Pack rows by text size, retaining timestamps and a small context overlap."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        text = str(row.get("text", ""))
        item = {"start": float(row.get("start", 0)), "end": float(row.get("end", 0)), "text": text[:500]}
        item_size = len(text) + 40
        if current and size + item_size > max_chars:
            chunks.append(current)
            # Keep the previous final caption as context for callbacks and setups.
            current = current[-1:]
            size = len(current[0]["text"]) + 40 if current else 0
        current.append(item)
        size += item_size
    if current:
        chunks.append(current)
    return chunks


def _json_from_text(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        value = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in value)
    text = str(value or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {"candidates": []}


def _fallback_findings(chunk: list[dict[str, Any]], target_len: int):
    """Cheap keyword/punctuation fallback when Groq is unavailable or fails."""
    hints = re.compile(r"\b(laugh|lmao|haha|hilarious|kidding|joke|funny|seriously|imagine|literally|apparently|plot twist|no way|what\?|why would|I thought)\b", re.I)
    out = []
    for row in chunk:
        text = row["text"].strip()
        if not text or not hints.search(text):
            continue
        out.append({"start": row["start"], "end": max(row["end"], row["start"] + target_len),
                    "joke_type": "possible_banter", "evidence": text[:240],
                    "reason": "Transcript wording or punctuation may signal a joke; review the surrounding video.",
                    "structure_score": 45, "overlay": "", "captions": {}})
    return out


def _ingest(state: ClipState):
    started = time.monotonic()
    rows = state.get("rows", [])
    chunks = _chunk_rows(rows)
    _progress("ingestion", f"Prepared {len(rows)} transcript segments in {len(chunks)} bounded chunks.", chunks=len(chunks), segments=len(rows))
    _LOG.info("agent=ingestion duration_seconds=%.2f", time.monotonic() - started)
    return {"chunks": chunks, "findings": []}


def _structure_agent(state: ClipState):
    """Comedic structure specialist; each call receives only one small transcript chunk."""
    started = time.monotonic()
    rows = state.get("rows", [])
    chunks = state.get("chunks") or _chunk_rows(rows)
    findings: list[dict[str, Any]] = []
    model = _model()
    language = state.get("language", "unknown")
    target = int(state.get("target_len", 6))
    for index, chunk in enumerate(chunks):
        _progress("comedic_structure", f"Reviewing transcript chunk {index + 1}/{len(chunks)}.", chunk=index + 1, total_chunks=len(chunks))
        if model is None:
            fallback = _fallback_findings(chunk, target)
            findings.extend(fallback)
            _LOG.info("agent=comedic_structure chunk=%d/%d mode=local_fallback candidates=%d", index + 1, len(chunks), len(fallback))
            continue
        prompt = (
        "Find at most 5 likely comedy moments in this timestamped transcript chunk. "
            "Return JSON only: {\"candidates\":[{\"start\":number,\"end\":number,"
            "\"joke_type\":string,\"evidence\":string,\"reason\":string,"
            "\"structure_score\":0-100,\"overlay\":string,\"captions\":{\"deadpan\":string,"
            "\"hype\":string,\"meme\":string}}]}. Use only supplied timestamps; each window should cover a coherent setup/punchline and target 3-8 seconds. "
            "Do not infer laughter or visual reactions. Be selective; return [] when there is no clear moment. "
            f"Transcript language: {language}. Target length: {target}s. Chunk {index + 1}/{len(chunks)}.\n"
            + json.dumps(chunk, ensure_ascii=False)
        )
        try:
            response = _paced_invoke(model, prompt)
            payload = _json_from_text(getattr(response, "content", response))
            raw_found = payload.get("candidates", [])
            found = raw_found[:5] if isinstance(raw_found, list) else []
            findings.extend(found)
            _LOG.info("agent=comedic_structure chunk=%d/%d mode=groq candidates=%d", index + 1, len(chunks), len(found))
            if not found:
                fallback = _fallback_findings(chunk, target)
                findings.extend(fallback)
                _LOG.info("agent=comedic_structure chunk=%d/%d mode=empty_response_fallback candidates=%d", index + 1, len(chunks), len(fallback))
        except Exception as exc:
            # A long transcript can trip provider TPM limits even with chunking; keep the app useful.
            if "413" in str(exc) or "tokens per minute" in str(exc).lower() or "rate_limit" in str(exc).lower():
                time.sleep(1)
            fallback = _fallback_findings(chunk, target)
            findings.extend(fallback)
            _LOG.warning("agent=comedic_structure chunk=%d/%d mode=fallback error_type=%s candidates=%d", index + 1, len(chunks), type(exc).__name__, len(fallback))
    _progress("comedic_structure", f"Structure review finished with {len(findings)} possible moments.", findings=len(findings))
    _LOG.info("agent=comedic_structure duration_seconds=%.2f findings=%d", time.monotonic() - started, len(findings))
    return {"findings": findings}


def _reaction_signal_agent(state: ClipState):
    """Transcript reaction cues only; this does not analyze audio or video."""
    started = time.monotonic()
    rows = state.get("rows", [])
    words = re.compile(r"\b(laugh(?:s|ing|ter)?|haha+|lmao|lol|audience|crowd|clap(?:ping)?|no way|oh my god|what\?|screaming)\b", re.I)
    signals = []
    for row in rows:
        text = row.get("text", "")
        if words.search(text):
            signals.append({"start": float(row.get("start", 0)), "end": float(row.get("end", 0)), "text": text})
    _progress("reaction_signal", f"Found {len(signals)} transcript reaction clues; audio/video were not analyzed.", signals=len(signals))
    _LOG.info("agent=reaction_signal duration_seconds=%.2f signals=%d", time.monotonic() - started, len(signals))
    return {"reaction_signals": signals}


def _candidate_agent(state: ClipState):
    started = time.monotonic()
    rows = state.get("rows", [])
    duration = max((float(r.get("end", 0)) for r in rows), default=0)
    target = int(state.get("target_len", 6))
    signals = state.get("reaction_signals", [])
    candidates = []
    for item in state.get("findings", []):
        try:
            start = max(0.0, float(item.get("start", 0)))
            end = min(duration, float(item.get("end", start + target)))
            if end - start < 3:
                end = min(duration, start + 3)
            if end - start > 8:
                end = start + 8
            if end <= start or end - start < 2.5:
                continue
            evidence = str(item.get("evidence", ""))[:300]
            cue = any(float(s["start"]) <= end and float(s["end"]) >= start for s in signals)
            structure_score = max(0, min(100, float(item.get("structure_score", 50))))
            score = min(100, round(structure_score * 0.85 + (12 if cue else 0)))
            candidate = dict(item)
            candidate.update(start=start, end=end, score=score, evidence=evidence,
                             reaction_signal="transcript cue present" if cue else "none found in transcript")
            candidate.setdefault("safety_flags", ["Review source context and rights before publishing."])
            candidate.setdefault("captions", {})
            candidates.append(candidate)
        except (TypeError, ValueError):
            continue
    # Merge overlapping windows so two chunk boundaries don't produce duplicate moments.
    candidates.sort(key=lambda c: c["score"], reverse=True)
    unique = []
    for c in candidates:
        if not any(c["start"] < prior["end"] and c["end"] > prior["start"] for prior in unique):
            unique.append(c)
    result = unique[:max(1, int(state.get("max_candidates", 6)))]
    _progress("candidate_generation", f"Kept {len(result)} candidates from {len(candidates)} findings after timing and duplicate checks.", findings=len(candidates), candidates=len(result))
    _LOG.info("agent=candidate_generation duration_seconds=%.2f findings=%d candidates=%d", time.monotonic() - started, len(candidates), len(result))
    return {"candidates": result}


def _ranking_agent(state: ClipState):
    started = time.monotonic()
    candidates = sorted(state.get("candidates", []), key=lambda c: c.get("score", 0), reverse=True)
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
    _progress("shareability_ranking", f"Ranked {len(candidates)} candidates by transcript structure and reaction heuristics.", candidates=len(candidates))
    _LOG.info("agent=shareability_ranking duration_seconds=%.2f candidates=%d", time.monotonic() - started, len(candidates))
    return {"candidates": candidates}


def _caption_agent(state: ClipState):
    """Create caption variants for already-ranked candidates, with local fallbacks."""
    started = time.monotonic()
    candidates = state.get("candidates", [])
    _progress("meme_caption", f"Preparing caption variants for {len(candidates)} candidates.", candidates=len(candidates))
    model = _model()
    if model and candidates:
        compact = [{"rank": c["rank"], "evidence": c.get("evidence", ""), "joke_type": c.get("joke_type", "moment")} for c in candidates]
        try:
            caption_prompt = (
                "For each ranked comedy moment, create concise meme captions. Return JSON only as "
                "{\"captions\":[{\"rank\":1,\"deadpan\":\"...\",\"hype\":\"...\",\"meme\":\"...\",\"overlay\":\"...\"}]}. "
                "Do not invent details beyond the evidence.\n" + json.dumps(compact, ensure_ascii=False)
            )
            response = _paced_invoke(model, caption_prompt)
            caption_rows = _json_from_text(getattr(response, "content", response)).get("captions", [])
            by_rank = {int(c.get("rank", 0)): c for c in caption_rows if isinstance(c, dict)}
            for candidate in candidates:
                generated = by_rank.get(candidate["rank"], {})
                existing = candidate.get("captions", {})
                candidate["captions"] = {k: generated.get(k) or existing.get(k) or "" for k in ("deadpan", "hype", "meme")}
                candidate["overlay"] = generated.get("overlay") or candidate.get("overlay", "")
        except Exception as exc:
            _LOG.warning("agent=meme_caption mode=fallback error_type=%s", type(exc).__name__)
    for c in candidates:
        c.setdefault("captions", {})
        c["captions"].setdefault("deadpan", "When the plan makes sense on paper")
        c["captions"].setdefault("hype", "Wait for it…")
        c["captions"].setdefault("meme", "me explaining this with confidence 💀")
        c.setdefault("overlay", c["captions"].get("meme", ""))
    _progress("meme_caption", f"Finished caption generation for {len(candidates)} candidates.", candidates=len(candidates))
    _LOG.info("agent=meme_caption duration_seconds=%.2f candidates=%d", time.monotonic() - started, len(candidates))
    return {"candidates": candidates}


def build_comedy_graph():
    graph = StateGraph(ClipState)
    graph.add_node("ingestion_agent", _ingest)
    graph.add_node("comedic_structure_agent", _structure_agent)
    graph.add_node("reaction_signal_agent", _reaction_signal_agent)
    graph.add_node("micro_clip_candidate_agent", _candidate_agent)
    graph.add_node("shareability_ranking_agent", _ranking_agent)
    graph.add_node("meme_caption_agent", _caption_agent)
    graph.add_edge(START, "ingestion_agent")
    graph.add_edge("ingestion_agent", "comedic_structure_agent")
    graph.add_edge("comedic_structure_agent", "reaction_signal_agent")
    graph.add_edge("reaction_signal_agent", "micro_clip_candidate_agent")
    graph.add_edge("micro_clip_candidate_agent", "shareability_ranking_agent")
    graph.add_edge("shareability_ranking_agent", "meme_caption_agent")
    graph.add_edge("meme_caption_agent", END)
    return graph.compile()


_COMEDY_GRAPH = None


def analyze_comedy(rows, language="en", max_candidates=6, target_len=6, progress_callback=None):
    global _COMEDY_GRAPH
    configure_logging()
    if _COMEDY_GRAPH is None:
        _COMEDY_GRAPH = build_comedy_graph()
    result = []
    _LOG.info("workflow=started segments=%d language=%s", len(rows), language)
    for mode, event in _COMEDY_GRAPH.stream(
        {"rows": rows, "language": language, "max_candidates": max_candidates, "target_len": target_len},
        stream_mode=["updates", "custom"],
    ):
        if mode == "custom":
            if callable(progress_callback):
                progress_callback(event)
        elif mode == "updates":
            for node, update in event.items():
                _LOG.info("node=%s completed", node)
                if "candidates" in update:
                    result = update["candidates"]
    _LOG.info("workflow=finished candidates=%d", len(result))
    return result
