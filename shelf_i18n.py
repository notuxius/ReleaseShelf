"""Translate desktop interface text while keeping recipe and preference keys stable."""

from functools import cache
import json
from pathlib import Path

from shelf_message import Message


LANGUAGES = {
    "ar": ("العربية", "Arabic"),
    "bn": ("বাংলা", "Bengali"),
    "zh": ("简体中文", "Chinese (Simplified)"),
    "arz": ("العربية المصرية", "Egyptian Arabic"),
    "en": ("English", "American"),
    "fr": ("Français", "French"),
    "de": ("Deutsch", "German"),
    "hi": ("हिन्दी", "Hindi"),
    "id": ("Bahasa Indonesia", "Indonesian"),
    "ja": ("日本語", "Japanese"),
    "pcm": ("Naijíriá Píjin", "Nigerian Pidgin"),
    "pt": ("Português (Brasil)", "Portuguese (Brazil)"),
    "ru": ("Русский", "Russian"),
    "es": ("Español", "Spanish"),
    "uk": ("Українська", "Ukrainian"),
    "ur": ("اردو", "Urdu"),
}
_messages = {}


@cache
def catalog(code):
    if code == "en" or code not in LANGUAGES:
        return {}
    path = Path(__file__).with_name("locales") / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def set_language(code):
    global _messages
    _messages = catalog(code)


def tr(template, **params):
    if isinstance(template, Message):
        params = template.params
        template = template.template
    template = _messages.get(template, template)
    if not params:
        return template
    values = {key: tr(value) if isinstance(value, Message) else value for key, value in params.items()}
    return template.format(**values)
