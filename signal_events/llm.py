"""Optional local-LLM features for the consolidated summary report, via a
locally running Ollama server (Llama 3.1 8B by default). No internet is
involved -- Ollama serves the model from disk over localhost, the same way
the Flask web UI itself is local-only.

The LLM never decides the threat level or the underlying evidence: the
deterministic scoring in `analysis.py` stays authoritative. `generate_narrative`
turns an already-computed, already-auditable summary into a readable prose
narrative (used by the CLI's --narrative flag); `generate_chat_reply` backs
the "AI-analys" web tab's chat-bot, answering questions grounded in a
supplied context of saved events and threat-level reports -- both are
explicitly forbidden from inventing facts or changing the verdict.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
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


def resolve_ollama_url(port: str | None = None) -> str:
    """The base URL to actually call: config.OLLAMA_URL's scheme/host with
    `port` spliced in if given -- `port` is the Inställningar override (see
    db.get_ollama_port), so a caller only needs to pass that straight
    through without knowing anything else about the URL. Falls back to
    config.OLLAMA_URL completely unchanged when `port` is empty/None,
    which is also what every existing caller/test that doesn't pass a
    port at all still gets."""
    if not port:
        return config.OLLAMA_URL
    parts = urllib.parse.urlsplit(config.OLLAMA_URL)
    scheme = parts.scheme or "http"
    host = parts.hostname or "localhost"
    return f"{scheme}://{host}:{port}"


def _post_ollama(path: str, payload: dict, base_url: str | None = None) -> dict:
    """Shared POST-and-parse-JSON plumbing for both the one-shot /api/generate
    call (generate_narrative) and the multi-turn /api/chat call
    (generate_chat_reply). Raises LLMError with a Swedish, user-facing
    message on any failure (server not running, model missing, timeout,
    invalid JSON, ...) -- callers only need to pull their own response
    field out of the returned dict."""
    base_url = base_url or config.OLLAMA_URL
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.OLLAMA_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
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
            f"Kunde inte nå Ollama på {base_url} — kontrollera att "
            f"'ollama serve' körs och att porten stämmer."
        ) from exc
    except TimeoutError as exc:
        raise LLMError(
            f"Ollama svarade inte inom {config.OLLAMA_TIMEOUT_SECONDS} sekunder."
        ) from exc
    except json.JSONDecodeError as exc:
        raise LLMError("Ollama returnerade ett oväntat svar (ogiltig JSON).") from exc


def generate_narrative(summary: analysis.Summary, site_name: str, base_url: str | None = None) -> str:
    """Ask the local Ollama server to turn `summary` into a prose narrative.
    `base_url` overrides config.OLLAMA_URL (see resolve_ollama_url) --
    typically the Inställningar port override, resolved by the caller.
    Raises LLMError with a Swedish, user-facing message on any failure
    (server not running, model missing, timeout, ...)."""
    prompt = _build_prompt(summary, site_name)
    data = _post_ollama("/api/generate", {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_ctx": config.OLLAMA_NUM_CTX},
    }, base_url=base_url)

    text = (data.get("response") or "").strip()
    if not text:
        error = data.get("error")
        raise LLMError(
            f"Ollama returnerade ingen text.{' Fel: ' + error if error else ''}"
        )
    return text


_CHAT_SYSTEM_PROMPT = (
    "Du är en chatbot-assistent åt en vaktstyrka på ett skyddsobjekt. Du har "
    "tillgång till tre typer av underlag, insatta nedan: (1) enhetens sparade "
    "händelserapporter, (2) enhetens egen historik av tidigare hotbedömningar "
    "(nuvarande och äldre), och (3) statusrapporter mottagna från angränsande "
    "enheter (nuvarande och äldre). Orden 'rapporter' och 'händelser' "
    "används synonymt för enhetens egna sparade händelserapporter (del 1) "
    "om inget annat anges -- anta INTE att en fråga om 'rapporter' bara "
    "gäller del 3 (angränsande enheter) bara för att det ordet står i den "
    "rubriken; kolla del 1 lika noga. Svara på frågor om detta underlag på "
    "svenska, sakligt och kortfattat. Hitta inte på fakta som inte finns i "
    "underlaget -- säg tydligt att informationen saknar stöd i underlaget om "
    "den gör det, men leta noggrant igenom ALLA rader i alla tre listorna "
    "innan du drar den slutsatsen -- ge inte upp efter att bara ha tittat på "
    "några få rader. Om frågan gäller vem som rapporterat något, kolla "
    "fältet 'rapporterad av' på varje händelserad; om frågan gäller foton/"
    "bilder, kolla fältet 'bifogat foto' (ja/nej) på varje händelserad. Om "
    "du blir ombedd att ange ett totalt antal händelser/rapporter/"
    "bedömningar/foton, använd de explicit angivna totalsummorna högst upp "
    "i underlaget ordagrant -- räkna aldrig rader själv och gissa aldrig "
    "ett antal, det blir ofta fel. Den fastställda hotnivån (och en "
    "eventuell manuell justering av "
    "den) beslutas av det regelbaserade systemet och en människa, inte av "
    "dig -- du får referera till och resonera kring den, men aldrig påstå "
    "eller föreslå en annan nivå än den som anges i underlaget. Skriv i "
    "normal löptext utan markdown-formattering."
)


def generate_chat_reply(history: list[dict], context: str, base_url: str | None = None) -> str:
    """Ask the local Ollama server for the next reply in the AI-analys
    tab's chat, given the running conversation (`history`, a list of
    {"role": "user"|"assistant", "content": str} dicts, oldest first) and
    a freshly built `context` string (see webapp/routes._build_ai_context)
    describing the currently saved events and threat-level reports. The
    context is re-sent as the system message on every turn -- rather than
    baked into the conversation once -- so an answer always reflects the
    data as it stands right now, including anything added since the chat
    started. `base_url` overrides config.OLLAMA_URL the same way as
    generate_narrative. Raises LLMError the same way generate_narrative
    does."""
    messages = [{"role": "system", "content": f"{_CHAT_SYSTEM_PROMPT}\n\nUNDERLAG:\n{context}"}]
    messages.extend(history)

    data = _post_ollama("/api/chat", {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.3, "num_ctx": config.OLLAMA_NUM_CTX},
    }, base_url=base_url)

    text = (data.get("message") or {}).get("content", "").strip()
    if not text:
        error = data.get("error")
        raise LLMError(
            f"Ollama returnerade ingen text.{' Fel: ' + error if error else ''}"
        )
    return text
