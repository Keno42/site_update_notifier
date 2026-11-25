"""language-learning-audio #428 (the owner, 2026-11-25: a /lesson-configure option): the learner's own name and age in the lines they say about
themselves. The values are personal: they live in the user's lesson settings and are passed to the generation (``--own-name``, ``--own-age``);
before a record leaves the machine (``feedback_sync``), the persona's values are put back, so the feedback repository names no one."""

from __future__ import annotations

import re

PERSONA_NAME = "Yuki"  # language-learning-audio's ``own.PERSONA_NAME``
PERSONA_AGE = 20  # the age the persona says in small talk 4 («Ég er tuttugu ára.»)
# a name in Latin letters (Icelandic's and other accented ones included): the target voice says it in «Ég heiti …», and its edges are found
# beside Japanese text, whose letters are not Latin (review of #245: «Kenjiの名前で»)
LATIN = "A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f"
NAME_RE = re.compile(rf"^(?=.{{1,24}}$)[{LATIN}]+(?:['\-][{LATIN}]+)*$")
# the ages the course can teach as an item (language-learning-audio's age fillers; a test reads them from the submodule): another age is not said
AGE_WORDS = {
    13: "þrettán", 14: "fjórtán", 15: "fimmtán", 16: "sextán", 17: "sautján", 18: "átján", 19: "nítján", 20: "tuttugu", 25: "tuttugu og fimm",
    30: "þrjátíu", 40: "fjörutíu", 50: "fimmtíu", 60: "sextíu", 70: "sjötíu", 80: "áttatíu", 90: "níutíu",
}


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def generate_args(name: str | None, age: int | None) -> list[str]:
    """The generation's arguments for the settings: none when neither is set, so a learner without them is generated as before."""
    return [*(["--own-name", name] if name else []), *(["--own-age", str(age)] if age is not None else [])]


def values_of_args(args) -> tuple[str | None, int | None]:
    """The own name and age a record was generated with, from its ``generate_args`` (review of #245: a record is scrubbed with its own values,
    never the learner's current settings, which may have changed or been cleared since)."""
    args = [str(a) for a in args] if isinstance(args, list) else []
    def value(flag: str) -> str | None:
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None
    name = value("--own-name")
    age = value("--own-age")
    return (name if name and valid_name(name) else None), (int(age) if age and age.isdigit() else None)


def scrub(text: str, name: str | None, age: int | None) -> str:
    """``text`` with the learner's own values replaced by the persona's: the name as a word (and as the value of ``--own-name``), and the age where
    the lesson says it (the line, its cue, its argument). A text without them is returned as it is."""
    if name:
        text = re.sub(rf"(?<![{LATIN}]){re.escape(name)}(?![{LATIN}])", PERSONA_NAME, text)
    if age is not None:
        text = re.sub(rf'("--own-age",\s*"){age}(")', rf"\g<1>{PERSONA_AGE}\g<2>", text)
        text = re.sub(rf"(--own-age\s+){age}\b", rf"\g<1>{PERSONA_AGE}", text)
        text = re.sub(rf"Say you're {age}\.", "Say you're twenty.", text)
        text = re.sub(rf"(?<!\d){age}歳だと言ってください", f"{PERSONA_AGE}歳だと言ってください", text)
        if age in AGE_WORDS:
            text = text.replace(f"Ég er {AGE_WORDS[age]} ára.", f"Ég er {AGE_WORDS[PERSONA_AGE]} ára.")
    return text
