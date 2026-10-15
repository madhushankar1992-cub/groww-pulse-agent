import hashlib
from datetime import timedelta

from groww_pulse.privacy.sanitiser import WORD

# MCP server must map this Markdown to these styles; the contract keeps style server-side.
DOC_FORMAT = {"font_size_pt": 11, "margins_inches": 1, "max_pages": 1,
              "headings": "title and section headings", "lists": "numbered themes/actions; quote bullets"}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def word_count(text):
    return len(WORD.findall(text))


def render(analysis, snapshot, composition):
    window = snapshot.window
    end = window.report_end - timedelta(days=1)
    excluded = sum(snapshot.counts.import_excluded.values())
    weeks = (window.report_end - window.history_start).days // 7
    coverage = (f"Coverage: {analysis.eligible_weekly} eligible English Google Play reviews this week; "
                f"{excluded} excluded across the export. History: {weeks} weeks; "
                f"{'complete' if snapshot.history_complete else 'partial'}; "
                f"source: {snapshot.source_status.replace('_', ' ')}.")
    lines = [f"# Groww Weekly Review Pulse | {window.report_start:%d %b} – {end:%d %b %Y}",
             coverage, "", "## Top themes"]
    summaries = {s.theme_id: s.text for s in composition.summaries}
    themes = [r for r in analysis.ranking if r.highlighted]
    for i, theme in enumerate(themes, 1):
        lines.append(f"{i}. {theme.name} — {theme.count}/{analysis.eligible_weekly} reviews. "
                     f"{summaries.get(theme.theme_id, '')}")
    lines.extend(["", "## What users said"])
    for quote in analysis.quotes:
        lines.append(f'- “{quote.excerpt}” — anonymous Google Play review')
    lines.extend(["", "## Action ideas"])
    actions = {a.theme_id: a.text for a in composition.actions}
    for i, theme in enumerate(themes, 1):
        lines.append(f"{i}. {actions.get(theme.theme_id, '')}")
    return "\n".join(lines) + "\n"
