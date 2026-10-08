"""Small line icons for DebugAssistAgent's pages, drawn to sit beside system text like SF Symbols (1.8 px strokes,
round caps). Decorative: every icon is aria-hidden and always next to words that say the same thing."""


def _svg(body: str, size: int = 20, cls: str = "") -> str:
    return (f'<svg class="i {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>')


def check(size: int = 20) -> str:
    return _svg('<path d="M5.5 12.5l4.2 4.2L18.5 7.8"/>', size)


def cross(size: int = 20) -> str:
    return _svg('<path d="M7.5 7.5l9 9M16.5 7.5l-9 9"/>', size)


def pause(size: int = 20) -> str:
    return _svg('<path d="M9.5 7.5v9M14.5 7.5v9"/>', size)


def spinner(size: int = 20) -> str:
    return _svg('<circle cx="12" cy="12" r="8" opacity=".25"/><path d="M12 4a8 8 0 0 1 8 8"/>', size, "spin")


def chevron(size: int = 16) -> str:
    return _svg('<path d="M9.5 6.5l5.5 5.5-5.5 5.5"/>', size)


def copy(size: int = 18) -> str:
    return _svg('<rect x="8.5" y="8.5" width="10" height="10" rx="2.5"/><path d="M15.5 8.5V7a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v6.5a2 2 0 0 0 2 2h1.5"/>', size)


def plus(size: int = 18) -> str:
    return _svg('<path d="M12 6v12M6 12h12"/>', size)


def play(size: int = 18) -> str:
    return _svg('<path d="M8.5 6.8v10.4a.8.8 0 0 0 1.2.7l8.3-5.2a.8.8 0 0 0 0-1.4L9.7 6.1a.8.8 0 0 0-1.2.7z"/>', size)


def list_(size: int = 18) -> str:
    return _svg('<path d="M9 7h10M9 12h10M9 17h10"/><circle cx="5" cy="7" r=".6"/><circle cx="5" cy="12" r=".6"/><circle cx="5" cy="17" r=".6"/>', size)


def link(size: int = 18) -> str:
    """Two chain links: connect a repo."""
    return _svg('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>', size)


def mark(size: int = 22) -> str:
    """DebugAssistAgent's mark: a target ring around a point, the thing it finds."""
    return _svg('<circle cx="12" cy="12" r="8.2"/><circle cx="12" cy="12" r="3.6"/><path d="M12 1.8v3M12 19.2v3M1.8 12h3M19.2 12h3"/>', size)


STATE = {"done": check, "running": spinner, "waiting": pause, "stopped": cross}
