"""Report generation: turns reviewed events into a Markdown or PDF summary.
Runs entirely offline -- reportlab and Pillow are pure local libraries."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any

from .. import analysis, naming

FIELD_LABELS = [
    ("event_time", "Tid"),
    ("place", "Plats"),
    ("count", "Antal observerade"),
    ("object", "Observerat objekt"),
    ("activity", "Aktivitet"),
    ("marks", "Kännetecken"),
    ("reported_by", "Rapporterad av"),
    ("next_steps", "Vad händer härnäst"),
]

def _group_heading(group: "analysis.RecurrenceGroup") -> str:
    """Text describing how often a group recurred and its score --
    except for a "notable" single-occurrence observation (a threat of
    violence, an armed person, ... that didn't recur), where "N ggr"
    doesn't mean anything, so it's labeled as a one-off instead."""
    if group.kind == "notable":
        return f"{group.label} (enstaka observation, poäng {group.score})"
    return f"{group.label} ({group.count} ggr, poäng {group.score})"


_SINCE_LABELS = {
    "24h": "senaste 24 timmarna",
    "7d": "senaste 7 dagarna",
    "30d": "senaste 30 dagarna",
    "all": "all tid",
}

_THREAT_LABELS = {
    "green": "GRÖN — Låg hotnivå",
    "yellow": "GUL — Förhöjd uppmärksamhet rekommenderas",
    "red": "RÖD — Återkommande allvarlig indikation, rekommenderad omedelbar genomgång",
}

_SUMMARY_DISCLAIMER = (
    "Detta är ett automatiskt genererat beslutsstöd baserat på enkla, "
    "regelbaserade mönster i lagrade rapporter (inte AI/ML). RÖD utlöses "
    "endast av återkommande (2+) allvarliga indikationer på våldsamma "
    "handlingar — beväpnade personer, misstänkt sprängladdning, eller "
    "tecken på sabotageförsök. En enstaka sådan observation ger GUL, "
    "liksom mönster i sig (återkommande fordon/personer) — RÖD kräver att "
    "indikationen upprepas. Detta ersätter inte mänsklig bedömning — "
    "verifiera alltid underliggande händelser innan åtgärd vidtas."
)


def render_markdown(rows: list[dict[str, Any]], since_label: str) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Händelserapport",
        "",
        f"Skapad: {generated_at}",
        f"Period: {_SINCE_LABELS.get(since_label, since_label)}",
        f"Händelser: {len(rows)}",
        "",
    ]
    for i, row in enumerate(rows, start=1):
        event = row["event"]
        lines.append(f"## Händelse {i}")
        for key, label in FIELD_LABELS:
            value = event[key] if event[key] else "—"
            lines.append(f"- **{label}:** {value}")
        if row["attachments"]:
            lines.append(f"- **Foton:** {len(row['attachments'])} bifogade (se lokala filer)")
        if event["raw_text"]:
            lines.append("")
            lines.append(f"> {event['raw_text']}")
        lines.append("")
    return "\n".join(lines)


def render_text(rows: list[dict[str, Any]], since_label: str) -> str:
    """Plain-text rendering -- same content as render_markdown(), but with
    no Markdown syntax, for reading in a basic text viewer or pasting
    into a plain email."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "HÄNDELSERAPPORT",
        "",
        f"Skapad: {generated_at}",
        f"Period: {_SINCE_LABELS.get(since_label, since_label)}",
        f"Händelser: {len(rows)}",
        "",
    ]
    for i, row in enumerate(rows, start=1):
        event = row["event"]
        heading = f"Händelse {i}"
        lines.append(heading)
        lines.append("-" * len(heading))
        for key, label in FIELD_LABELS:
            value = event[key] if event[key] else "—"
            lines.append(f"{label}: {value}")
        if row["attachments"]:
            lines.append(f"Foton: {len(row['attachments'])} bifogade (se lokala filer)")
        if event["raw_text"]:
            lines.append("")
            lines.append(event["raw_text"])
        lines.append("")
    return "\n".join(lines)


def render_pdf(rows: list[dict[str, Any]], since_label: str) -> io.BytesIO:
    # Imported lazily so `signal-events report --format markdown` doesn't
    # require reportlab/Pillow to be installed.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )
    from PIL import Image as PILImage

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Händelserapport")
    story: list[Any] = []

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph("Händelserapport", styles["Title"]))
    story.append(Paragraph(f"Skapad: {generated_at}", styles["Normal"]))
    story.append(
        Paragraph(f"Period: {_SINCE_LABELS.get(since_label, since_label)}", styles["Normal"])
    )
    story.append(Paragraph(f"Händelser: {len(rows)}", styles["Normal"]))
    story.append(Spacer(1, 0.5 * cm))

    for i, row in enumerate(rows, start=1):
        event = row["event"]
        story.append(Paragraph(f"Händelse {i}", styles["Heading2"]))

        table_data = [
            [label, event[key] or "—"] for key, label in FIELD_LABELS
        ]
        table = Table(table_data, colWidths=[4.5 * cm, 11 * cm])
        table.setStyle(
            TableStyle(
                [
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(table)

        if event["raw_text"]:
            story.append(Spacer(1, 0.15 * cm))
            story.append(Paragraph(f"<i>{event['raw_text']}</i>", styles["Normal"]))

        for attachment in row["attachments"]:
            try:
                with PILImage.open(attachment["file_path"]) as im:
                    im.thumbnail((400, 400))
                    w, h = im.size
                story.append(Spacer(1, 0.2 * cm))
                story.append(Image(attachment["file_path"], width=w * (5 * cm / max(w, 1)),
                                    height=h * (5 * cm / max(w, 1))))
            except Exception:
                continue  # unreadable/corrupt image; skip rather than fail the report

        story.append(Spacer(1, 0.6 * cm))

    doc.build(story)
    buf.seek(0)
    return buf


def _format_group_events(group: "analysis.RecurrenceGroup") -> list[str]:
    lines = []
    for ref in group.events:
        tnr = naming.event_tnr(ref.created_at)
        # event_time (when the observation was made) is shown alongside
        # TNR (when the app received the report) since the two now always
        # come from different timestamps -- skip it only in the rare case
        # it happens to read identically to the TNR already shown.
        extras = [ref.event_time] if ref.event_time and ref.event_time.strip() != tnr else []
        extras.append(ref.place)
        parts = [p for p in extras if p]
        lines.append(f"Händelse {tnr}" + (f" — {', '.join(parts)}" if parts else ""))
    return lines


def render_summary_markdown(
    summary: "analysis.Summary", site_name: str, narrative: str | None = None
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    threat = summary.threat
    lines = [
        f"# Sammanställd hotbedömning — {site_name}",
        "",
        f"Skapad: {generated_at}",
        f"Period: {_SINCE_LABELS.get(summary.period_label, summary.period_label)}",
        f"Rapporter i underlaget: {summary.total_events}",
        "",
        f"## Hotnivå: {_THREAT_LABELS.get(threat.level, threat.level)}",
        f"Poäng: {threat.score}",
        "",
        "### Motivering",
    ]
    for reason in threat.reasons:
        lines.append(f"- {reason}")
    lines += ["", f"_{_SUMMARY_DISCLAIMER}_", ""]

    if narrative:
        lines += [
            "## AI-sammanfattning (Llama 3.1 8B, lokal modell)",
            narrative,
            "",
            "_AI-genererad text baserad på det regelbaserade underlaget ovan "
            "— den ändrar inte hotnivån. Kontrollera alltid mot "
            "originalhändelserna._",
            "",
        ]

    for title, groups in [
        ("Återkommande fordon", summary.vehicle_groups),
        ("Återkommande personer", summary.person_groups),
        ("Övriga anmärkningsvärda observationer", summary.other_groups),
    ]:
        lines.append(f"## {title}")
        if not groups:
            lines.append("Inga identifierade.")
            lines.append("")
            continue
        for group in groups:
            lines.append(f"### {_group_heading(group)}")
            for reason in group.reasons:
                lines.append(f"- {reason}")
            for event_line in _format_group_events(group):
                lines.append(f"  - {event_line}")
            lines.append("")

    return "\n".join(lines)


def render_summary_text(
    summary: "analysis.Summary", site_name: str, narrative: str | None = None
) -> str:
    """Plain-text rendering -- same content as render_summary_markdown(),
    but with no Markdown syntax."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    threat = summary.threat
    title = f"SAMMANSTÄLLD HOTBEDÖMNING — {site_name}"
    lines = [
        title,
        "",
        f"Skapad: {generated_at}",
        f"Period: {_SINCE_LABELS.get(summary.period_label, summary.period_label)}",
        f"Rapporter i underlaget: {summary.total_events}",
        "",
        f"HOTNIVÅ: {_THREAT_LABELS.get(threat.level, threat.level)}",
        f"Poäng: {threat.score}",
        "",
        "Motivering",
    ]
    for reason in threat.reasons:
        lines.append(f"- {reason}")
    lines += ["", _SUMMARY_DISCLAIMER, ""]

    if narrative:
        lines += [
            "AI-SAMMANFATTNING (Llama 3.1 8B, lokal modell)",
            narrative,
            "",
            "AI-genererad text baserad på det regelbaserade underlaget ovan "
            "— den ändrar inte hotnivån. Kontrollera alltid mot "
            "originalhändelserna.",
            "",
        ]

    for section_title, groups in [
        ("Återkommande fordon", summary.vehicle_groups),
        ("Återkommande personer", summary.person_groups),
        ("Övriga anmärkningsvärda observationer", summary.other_groups),
    ]:
        lines.append(section_title)
        if not groups:
            lines.append("Inga identifierade.")
            lines.append("")
            continue
        for group in groups:
            lines.append(_group_heading(group))
            for reason in group.reasons:
                lines.append(f"- {reason}")
            for event_line in _format_group_events(group):
                lines.append(f"  {event_line}")
            lines.append("")

    return "\n".join(lines)


_WATCHLIST_DISCLAIMER = (
    "Automatiskt genererad lista baserad direkt på registrerade personer, "
    "fordon och objekt (inte AI/ML): de som är länkade till flera "
    "händelser, eller manuellt markerade för bevakning. Kontrollera alltid "
    "mot originalhändelserna innan åtgärd vidtas."
)

_WATCHLIST_TYPE_TITLES = [
    ("person", "Personer"),
    ("vehicle", "Fordon"),
    ("object", "Objekt"),
]


def _watchlist_entry_heading(entry: dict[str, Any]) -> str:
    entity = entry["entity"]
    count = entity["event_count"]
    heading = f"{entity['label']} ({count} {'händelse' if count == 1 else 'händelser'})"
    if entity["watchlist"]:
        heading += " — bevakas manuellt"
    return heading


def _watchlist_entry_details(entry: dict[str, Any]) -> str | None:
    """A compact one-line summary of an entity's stored attributes (Reg.nr
    for a vehicle, Namn/Alias for a person, ...) -- whatever entities.py's
    parser or a human filled in, same key/value pairs shown in full on the
    entity's own page."""
    entity = entry["entity"]
    parts = []
    if entity["registration"]:
        parts.append(f"Reg.nr: {entity['registration']}")
    attributes = json.loads(entity["attributes"]) if entity["attributes"] else {}
    for key, value in attributes.items():
        if key == "Registration":
            continue  # already shown above, normalized, as Reg.nr
        parts.append(f"{key}: {value}")
    if entity["notes"]:
        parts.append(entity["notes"])
    return ", ".join(parts) if parts else None


def _format_entity_events(events: list[Any]) -> list[str]:
    lines = []
    for event in events:
        tnr = naming.event_tnr(event["created_at"])
        extras = [event["event_time"]] if event["event_time"] and event["event_time"].strip() != tnr else []
        extras.append(event["place"])
        parts = [p for p in extras if p]
        lines.append(f"Händelse {tnr}" + (f" — {', '.join(parts)}" if parts else ""))
    return lines


def render_watchlist_markdown(entries: list[dict[str, Any]], site_name: str) -> str:
    """A focused list of recurring and manually bevakade (watchlisted)
    persons/vehicles/objects, straight from the entities database -- no
    threat-level badge or score. For the "Skicka bevakningslista" button
    on Personer, fordon och objekt (see db.list_watchlist_entities)."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Bevakningslista — {site_name}",
        "",
        f"Skapad: {generated_at}",
        f"Poster på listan: {len(entries)}",
        "",
    ]

    for entity_type, title in _WATCHLIST_TYPE_TITLES:
        type_entries = [e for e in entries if e["entity"]["entity_type"] == entity_type]
        lines.append(f"## {title}")
        if not type_entries:
            lines.append("Inga identifierade.")
            lines.append("")
            continue
        for entry in type_entries:
            lines.append(f"### {_watchlist_entry_heading(entry)}")
            details = _watchlist_entry_details(entry)
            if details:
                lines.append(f"- {details}")
            for event_line in _format_entity_events(entry["events"]):
                lines.append(f"  - {event_line}")
            lines.append("")

    lines += [f"_{_WATCHLIST_DISCLAIMER}_", ""]
    return "\n".join(lines)


def render_watchlist_pdf(entries: list[dict[str, Any]], site_name: str) -> io.BytesIO:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Bevakningslista")
    story: list[Any] = []

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Bevakningslista — {site_name}", styles["Title"]))
    story.append(Paragraph(f"Skapad: {generated_at}", styles["Normal"]))
    story.append(Paragraph(f"Poster på listan: {len(entries)}", styles["Normal"]))
    story.append(Spacer(1, 0.5 * cm))

    for entity_type, title in _WATCHLIST_TYPE_TITLES:
        type_entries = [e for e in entries if e["entity"]["entity_type"] == entity_type]
        story.append(Paragraph(title, styles["Heading2"]))
        if not type_entries:
            story.append(Paragraph("Inga identifierade.", styles["Normal"]))
            story.append(Spacer(1, 0.3 * cm))
            continue
        for entry in type_entries:
            story.append(Paragraph(_watchlist_entry_heading(entry), styles["Heading3"]))
            details = _watchlist_entry_details(entry)
            if details:
                story.append(Paragraph(details, styles["Normal"]))
            event_lines = _format_entity_events(entry["events"])
            if event_lines:
                story.append(Paragraph(", ".join(event_lines), styles["Normal"]))
            story.append(Spacer(1, 0.3 * cm))

    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(f"<i>{_WATCHLIST_DISCLAIMER}</i>", styles["Normal"]))

    doc.build(story)
    buf.seek(0)
    return buf


def render_summary_pdf(
    summary: "analysis.Summary", site_name: str, narrative: str | None = None
) -> io.BytesIO:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    threat_colors = {
        "green": colors.HexColor("#d7f0d7"),
        "yellow": colors.HexColor("#fde2b6"),
        "red": colors.HexColor("#f8d7d7"),
    }

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Sammanställd hotbedömning")
    story: list[Any] = []
    threat = summary.threat

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Sammanställd hotbedömning — {site_name}", styles["Title"]))
    story.append(Paragraph(f"Skapad: {generated_at}", styles["Normal"]))
    story.append(
        Paragraph(
            f"Period: {_SINCE_LABELS.get(summary.period_label, summary.period_label)}",
            styles["Normal"],
        )
    )
    story.append(Paragraph(f"Rapporter i underlaget: {summary.total_events}", styles["Normal"]))
    story.append(Spacer(1, 0.4 * cm))

    level_table = Table(
        [[f"{_THREAT_LABELS.get(threat.level, threat.level)}  (poäng: {threat.score})"]],
        colWidths=[15.5 * cm],
    )
    level_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), threat_colors.get(threat.level, colors.white)),
            ("FONTSIZE", (0, 0), (-1, -1), 12),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ])
    )
    story.append(level_table)
    story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph("Motivering", styles["Heading3"]))
    for reason in threat.reasons:
        story.append(Paragraph(f"• {reason}", styles["Normal"]))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(f"<i>{_SUMMARY_DISCLAIMER}</i>", styles["Normal"]))
    story.append(Spacer(1, 0.6 * cm))

    if narrative:
        story.append(Paragraph("AI-sammanfattning (Llama 3.1 8B, lokal modell)", styles["Heading3"]))
        for paragraph in narrative.strip().split("\n\n"):
            clean = escape(paragraph.replace("\n", " ").strip())
            if clean:
                story.append(Paragraph(clean, styles["Normal"]))
                story.append(Spacer(1, 0.15 * cm))
        story.append(
            Paragraph(
                "<i>AI-genererad text baserad på det regelbaserade underlaget "
                "ovan — den ändrar inte hotnivån. Kontrollera alltid mot "
                "originalhändelserna.</i>",
                styles["Normal"],
            )
        )
        story.append(Spacer(1, 0.6 * cm))

    for title, groups in [
        ("Återkommande fordon", summary.vehicle_groups),
        ("Återkommande personer", summary.person_groups),
        ("Övriga anmärkningsvärda observationer", summary.other_groups),
    ]:
        story.append(Paragraph(title, styles["Heading2"]))
        if not groups:
            story.append(Paragraph("Inga identifierade.", styles["Normal"]))
            story.append(Spacer(1, 0.3 * cm))
            continue
        for group in groups:
            story.append(
                Paragraph(_group_heading(group), styles["Heading3"])
            )
            for reason in group.reasons:
                story.append(Paragraph(f"• {reason}", styles["Normal"]))
            event_lines = _format_group_events(group)
            if event_lines:
                story.append(Paragraph(", ".join(event_lines), styles["Normal"]))
            story.append(Spacer(1, 0.3 * cm))

    doc.build(story)
    buf.seek(0)
    return buf
