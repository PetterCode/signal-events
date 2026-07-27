"""Optional local-LLM narrative for the consolidated summary report, via a
locally running Ollama server (Llama 3.1 8B by default). No internet is
involved -- Ollama serves the model from disk over localhost, the same way
the Flask web UI itself is local-only.

The LLM never decides the threat level or the underlying evidence: the
deterministic scoring in `analysis.py` stays authoritative. This module
only asks the model to turn that already-computed, already-auditable
summary into a readable prose narrative -- the prompt explicitly forbids
inventing facts or changing the verdict.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import analysis, config

_THREAT_LABELS = {
    "green": "GRÖN (låg hotnivå)",
    "yellow": "GUL (förhöjd uppmärksamhet)",
    "red": "RÖD (hög hotnivå)",
}

_SYSTEM_PROMPT = (
    "Du är en assistent som skriver korta, sakliga lägesrapporter åt en "
    "vaktstyrka baserat på ett redan färdigt, regelbaserat analysunderlag. "
    "Hitta inte på nya fakta och ändra inte den fastställda hotnivån eller "
    "poängen -- de är redan beslutade och ska tas som givna. Din enda "
    "uppgift är att sammanfatta underlaget till löpande text på svenska "
    "som är lätt att läsa för en beslutsfattare, och lyfta fram det mest "
    "relevanta mönstret först. Skriv 3-5 korta stycken utan rubrik, egna "
    "punktlistor, eller markdown-formattering."
)


class LLMError(RuntimeError):
    pass


def _format_group(group: analysis.RecurrenceGroup) -> str:
    places = ", ".join(sorted(group.distinct_places)) or "okänd plats"
    frequency = (
        "enstaka observation" if group.kind == "notable" else f"{group.count} ggr"
    )
    return (
        f"- {group.label}: {frequency}, poäng {group.score}, "
        f"platser: {places}. {'; '.join(group.reasons)}"
    )


def _build_prompt(summary: analysis.Summary, site_name: str) -> str:
    threat = summary.threat
    sections = [
        f"Skyddsobjekt: {site_name}",
        f"Period: {summary.period_label}",
        f"Antal rapporter i underlaget: {summary.total_events}",
        f"Fastställd hotnivå: {_THREAT_LABELS.get(threat.level, threat.level)} "
        f"(poäng: {threat.score})",
        "Motivering till hotnivån:",
        *[f"- {reason}" for reason in threat.reasons],
    ]

    for title, groups in [
        ("Återkommande fordon", summary.vehicle_groups),
        ("Återkommande personer", summary.person_groups),
        ("Övriga anmärkningsvärda observationer", summary.other_groups),
    ]:
        sections.append(f"\n{title}:")
        if not groups:
            sections.append("- Inga identifierade.")
        else:
            sections.extend(_format_group(g) for g in groups)

    body = "\n".join(sections)
    return f"{_SYSTEM_PROMPT}\n\nUNDERLAG:\n{body}\n\nSkriv nu lägesrapporten:"


def generate_narrative(summary: analysis.Summary, site_name: str) -> str:
    """Ask the local Ollama server to turn `summary` into a prose narrative.
    Raises LLMError with a Swedish, user-facing message on any failure
    (server not running, model missing, timeout, ...)."""
    prompt = _build_prompt(summary, site_name)
    payload = json.dumps({
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3},
    }).encode("utf-8")

    request = urllib.request.Request(
        f"{config.OLLAMA_URL.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.OLLAMA_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        if exc.code == 404 or "not found" in detail.lower():
            raise LLMError(
                f"Ollama är igång, men hittar ingen modell med taggen "
                f"'{config.OLLAMA_MODEL}'. Kör 'ollama list' för att se dina "
                f"nedladdade modeller, och antingen hämta den exakta taggen "
                f"(ollama pull {config.OLLAMA_MODEL}) eller sätt "
                f"SIGNAL_EVENTS_OLLAMA_MODEL till en tagg du redan har "
                f"(t.ex. llama3.1:latest)."
            ) from exc
        raise LLMError(f"Ollama svarade med fel {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(
            f"Kunde inte nå Ollama på {config.OLLAMA_URL} — kontrollera att "
            f"'ollama serve' körs."
        ) from exc
    except TimeoutError as exc:
        raise LLMError(
            f"Ollama svarade inte inom {config.OLLAMA_TIMEOUT_SECONDS} sekunder."
        ) from exc
    except json.JSONDecodeError as exc:
        raise LLMError("Ollama returnerade ett oväntat svar (ogiltig JSON).") from exc

    text = (data.get("response") or "").strip()
    if not text:
        error = data.get("error")
        raise LLMError(
            f"Ollama returnerade ingen text.{' Fel: ' + error if error else ''}"
        )
    return text
