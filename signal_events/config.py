"""Runtime configuration, driven by environment variables so the tool needs
no network access and no external config service to run on a laptop."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# .resolve() on all three of these: a relative SIGNAL_EVENTS_DATA_DIR (or
# _DB_PATH/_ATTACHMENTS_DIR) would otherwise be stored as-is, relative to
# whatever the process's CWD happened to be when it started -- fine for
# writing files, but attachment_file's send_file(attachment["file_path"])
# resolves relative paths against the Flask app's root_path (the
# signal_events/webapp/ package directory), not the CWD, so a relative
# DATA_DIR silently 404s/500s on every attachment despite the file
# genuinely existing on disk.
DATA_DIR = Path(os.environ.get("SIGNAL_EVENTS_DATA_DIR", PROJECT_ROOT / "data")).resolve()

DB_PATH = Path(os.environ.get("SIGNAL_EVENTS_DB_PATH", DATA_DIR / "events.db")).resolve()
ATTACHMENTS_DIR = Path(
    os.environ.get("SIGNAL_EVENTS_ATTACHMENTS_DIR", DATA_DIR / "attachments")
).resolve()

# Number linked/registered with signal-cli, e.g. "+15551234567". Only needed
# for `sync`, which is the one command that requires network access.
PHONE_NUMBER = os.environ.get("SIGNAL_EVENTS_PHONE_NUMBER")

SIGNAL_CLI_BIN = os.environ.get("SIGNAL_EVENTS_SIGNAL_CLI_BIN", "signal-cli")

# Where signal-cli itself stores downloaded attachments before we copy them
# into ATTACHMENTS_DIR.
SIGNAL_CLI_ATTACHMENTS_DIR = Path(
    os.environ.get(
        "SIGNAL_EVENTS_SIGNAL_CLI_ATTACHMENTS_DIR",
        Path.home() / ".local" / "share" / "signal-cli" / "attachments",
    )
)

WEB_HOST = os.environ.get("SIGNAL_EVENTS_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("SIGNAL_EVENTS_WEB_PORT", "5000"))

# Name of the site/facility these reports are about, used in the
# consolidated summary report's heading. Purely cosmetic.
SITE_NAME = os.environ.get("SIGNAL_EVENTS_SITE_NAME", "skyddsobjektet")

# Optional local LLM narrative for the summary report, via a locally running
# Ollama server (http://localhost:11434 by default) -- no internet involved,
# same as the rest of this tool, since Ollama serves the model from disk.
OLLAMA_URL = os.environ.get("SIGNAL_EVENTS_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("SIGNAL_EVENTS_OLLAMA_MODEL", "llama3.1:latest")

# Ollama's own default context window (num_ctx) is much smaller than what
# this model actually supports -- 2048/4096 tokens on a typical install --
# and silently truncates whatever doesn't fit, with no error raised. The
# AI-analys chat's underlag (saved events + report history, see
# webapp/routes._build_ai_context) regularly runs to 10-20k+ tokens on a
# real event log, so without an explicit override here Ollama would drop
# most of it before the model ever sees it -- the model would answer from
# a truncated fragment and look like it "can't see" older data, without
# any error being raised anywhere. Verified against a real 301-event log:
# 24576 was the smallest size that let the model correctly find and quote
# the actual oldest event rather than a truncated fragment. Llama 3.1
# itself supports up to 131072, but a bigger window costs real time on
# CPU-only/modest hardware -- see OLLAMA_TIMEOUT_SECONDS below.
OLLAMA_NUM_CTX = int(os.environ.get("SIGNAL_EVENTS_OLLAMA_NUM_CTX", "24576"))

# A full, uncached underlag at the context size above can legitimately take
# several minutes to process on CPU-only/modest hardware (measured on a
# real ~300-event log while diagnosing truncation) -- 120s was too short
# and would cut off a response that was still correctly on its way,
# reporting it as a failure. Lower this back down if your hardware is
# fast enough that you'd rather fail fast than wait.
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("SIGNAL_EVENTS_OLLAMA_TIMEOUT", "300"))

# Signal group `watch`/`serve --watch` poll incoming reports *from*.
WATCH_GROUP_NAME = os.environ.get(
    "SIGNAL_EVENTS_WATCH_GROUP", "Stabsassistent test-händelser"
)

# Signal group generated reports/summaries are sent *to* via the "Skicka
# till Signal" buttons in the web UI (and requires network + signal-cli,
# like `sync`/`watch`). Separate from WATCH_GROUP_NAME above -- input and
# output channels are deliberately different groups.
#
# This same group doubles as the exchange channel `watch`/`serve --watch`
# poll for status reports *from* adjacent units -- since everyone posts
# their own generated reports here, it's already the shared channel where
# other units' reports show up too. The sending unit is identified from
# the plain-text unit name embedded in the report's own filename (see
# naming.parse_report_filename), not from a separate sender mapping, and
# only real incoming messages are ever ingested (see ingest_adjacent_report
# in signal_client.py), so this unit's own sent reports are never picked
# back up as if they were an adjacent unit's.
REPORT_GROUP_NAME = os.environ.get(
    "SIGNAL_EVENTS_REPORT_GROUP", "Stabsassistent test-rapport"
)

# Signal group the recurring/suspicious vehicles-and-people list is sent to
# via the "Skicka lista över återkommande" button -- a focused watchlist,
# separate from the full report/summary distribution above.
RECURRING_GROUP_NAME = os.environ.get(
    "SIGNAL_EVENTS_RECURRING_GROUP", "Stabsassistent test-återkommande"
)

# Signal group automated sensor-trigger events are ingested from -- a
# dedicated sensor gateway account posts here, reporting in the same 7S
# format as human-submitted incident reports (see parser.parse_event_fields),
# so they're parsed and stored the exact same way, just from a separate
# group than WATCH_GROUP_NAME's human intake.
SENSOR_GROUP_NAME = os.environ.get(
    "SIGNAL_EVENTS_SENSOR_GROUP", "Stabsassistent test-sensorer"
)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
