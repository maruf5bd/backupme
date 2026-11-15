"""Shared progress display helper for long-running CLI operations.

Uses tqdm when available (lazy import), with a plain ``click.echo`` fallback
that renders ``desc: done/total (pct%)`` in-place lines. Stdlib + tqdm only;
no backupme.core imports so this stays lazy-import friendly.
"""
import click

try:  # optional dependency: resolved once at import, tests may monkeypatch this
    from tqdm import tqdm

    _HAS_TQDM = True
except ImportError:  # pragma: no cover - exercised implicitly when tqdm absent
    tqdm = None
    _HAS_TQDM = False


class Progress:
    """Optional progress display; a no-op when ``enabled`` is false."""

    def __init__(self, enabled: bool, total: int | None = None, desc: str = ""):
        self._enabled = enabled
        self._desc = desc
        self._done = 0
        self._total = total
        self._bar = None
        if not enabled:
            return
        if _HAS_TQDM:
            self._bar = tqdm(total=total, desc=desc or None, unit="item")
        elif total is not None:
            self._render_fallback()

    def update(self, n: int = 1):
        if not self._enabled:
            return
        self._done += n
        if self._bar is not None:
            self._bar.update(n)
        else:
            self._render_fallback()

    def set_total(self, total: int):
        """Totals may be discovered mid-stream (e.g. estimate_archive)."""
        if not self._enabled:
            return
        self._total = total
        if self._bar is not None:
            self._bar.total = total
            self._bar.refresh()
        else:
            self._render_fallback()

    def finish(self):
        if not self._enabled:
            return
        if self._bar is not None:
            self._bar.close()
            self._bar = None
        else:
            click.echo()
        summary = f"{self._desc}: done ({self._done})" if self._desc else f"done ({self._done})"
        click.echo(summary)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.finish()
        return False

    def _render_fallback(self):
        total = self._total
        if total:
            pct = round(self._done * 100 / total)
            click.echo(f"\r{self._desc}: {self._done}/{total} ({pct}%)", nl=False)
        else:
            click.echo(f"\r{self._desc}: {self._done}/? (?%)", nl=False)
