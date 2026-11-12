"""Front-end checks that need no browser: static serving and a small CSS parse."""

import math
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bank.api import WEB_DIR, create_app

SMALL_SCREEN_PX = 430


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def css_rules(css: str) -> list[tuple[str | None, str, str]]:
    """Flatten a stylesheet into (media query or None, selector list, declarations)."""
    rules: list[tuple[str | None, str, str]] = []

    def walk(text: str, media: str | None) -> None:
        pos = 0
        while (start := text.find("{", pos)) != -1:
            prelude = text[pos:start].strip()
            depth, end = 1, start + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(text[end], 0)
                end += 1
            body = text[start + 1 : end - 1]
            if prelude.startswith("@media"):
                walk(body, prelude.removeprefix("@media").strip())
            elif not prelude.startswith("@"):
                rules.append((media, prelude, body))
            pos = end

    walk(strip_comments(css), None)
    return rules


def to_px(value: str, unit: str) -> float:
    return float(value) * (16 if unit in ("em", "rem") else 1)


def applies_at_or_below(query: str | None, width: int) -> bool:
    """True when the media query matches some viewport no wider than ``width``."""
    if query is None:
        return True
    for part in query.split(","):
        if "print" in part and "screen" not in part:
            continue
        lows = [to_px(v, u) for v, u in re.findall(r"min-width\s*:\s*([\d.]+)(px|r?em)", part)]
        highs = [to_px(v, u) for v, u in re.findall(r"max-width\s*:\s*([\d.]+)(px|r?em)", part)]
        low, high = max(lows, default=0.0), min(highs, default=math.inf)
        if low <= width and low <= high:
            return True
    return False


def send_button_tokens() -> set[str]:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    tag = re.search(r'<button[^>]*id="send-button"[^>]*>\s*Send\s*</button>', html)
    assert tag, "index.html has no Send button with id send-button"
    classes = re.search(r'class="([^"]*)"', tag.group(0))
    names = classes.group(1).split() if classes else []
    return {"*", "button", "#send-button"} | {f".{name}" for name in names}


def targets(selector_list: str, tokens: set[str]) -> bool:
    """Does any selector's last compound selector match an element with these tokens?"""
    for selector in selector_list.split(","):
        compound = re.split(r"[\s>+~]+", selector.strip())[-1]
        compound = re.sub(r"\[[^\]]*\]|::?[\w-]+(\([^)]*\))?", "", compound)
        parts = re.findall(r"[#.]?[\w-]+|\*", compound)
        if parts and all(part in tokens for part in parts):
            return True
    return False


def hiding_rules(css: str, tokens: set[str], width: int) -> list[tuple[str | None, str]]:
    return [
        (media, selector)
        for media, selector, body in css_rules(css)
        if re.search(r"display\s*:\s*none", body)
        and applies_at_or_below(media, width)
        and targets(selector, tokens)
    ]


def test_send_button_not_hidden_at_or_below_430px():
    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    assert hiding_rules(css, send_button_tokens(), SMALL_SCREEN_PX) == []


@pytest.mark.parametrize(
    "css, hidden",
    [
        ("@media (max-width: 430px) { .x #send-button { display: none } }", True),
        ("@media (max-width: 600px) { button.btn-primary { display:none; } }", True),
        ("@media (min-width: 431px) { #send-button { display: none } }", False),
        ("@media (max-width: 430px) { .hide-small { display: none } }", False),
        ("@media print { #send-button { display: none } }", False),
        ("/* #send-button { display: none } */ .btn { color: red }", False),
        ("#send-button:disabled { display: none }", True),
    ],
)
def test_css_check_finds_hiding_rules(css, hidden):
    tokens = {"*", "button", "#send-button", ".btn", ".btn-primary"}
    assert bool(hiding_rules(css, tokens, SMALL_SCREEN_PX)) is hidden


def test_pages_are_served_without_external_assets(tmp_path):
    client = TestClient(create_app(tmp_path / "web.db", seed_if_empty=False))
    page = client.get("/")
    assert page.status_code == 200
    assert "YMS Bank" in page.text and 'id="send-button"' in page.text
    for asset in ("/app.css", "/app.js"):
        assert client.get(asset).status_code == 200
    for path in Path(WEB_DIR).iterdir():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(src|href)=\"(https?:)?//", text), path.name
        assert "@import" not in text and "url(http" not in text, path.name
