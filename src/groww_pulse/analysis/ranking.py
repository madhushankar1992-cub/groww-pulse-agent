from collections import Counter

from groww_pulse.schemas import Taxonomy, ThemeRank


def rank(taxonomy: Taxonomy, weekly_assignments: dict[str, str], min_per_theme: int,
         highlights: int = 3) -> tuple[ThemeRank, ...]:
    """Weekly counts in code: count desc, then theme_id. Catch-all themes are never highlighted."""
    counts = Counter(weekly_assignments.values())
    total = len(weekly_assignments)
    ordered = sorted(taxonomy.themes, key=lambda t: (-counts[t.theme_id], t.theme_id))
    chosen: set[str] = set()
    for theme in ordered:
        if len(chosen) < highlights and not theme.is_catch_all and counts[theme.theme_id] >= min_per_theme:
            chosen.add(theme.theme_id)
    return tuple(
        ThemeRank(theme_id=t.theme_id, name=t.name, count=counts[t.theme_id],
                  share=round(counts[t.theme_id] / total, 4) if total else 0.0,
                  highlighted=t.theme_id in chosen)
        for t in ordered
    )
