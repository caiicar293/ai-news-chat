"""Deterministic graders for the accuracy eval.

No model judges another model here: every check is a rule you can read and
reproduce. That keeps the eval cheap and stable; the trade-off is that it
rewards answers that reuse the source wording.
"""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[a-z0-9$%.]+")
NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")

STOPWORDS = frozenset(
    """
    a an and are as at be been but by for from had has have he her his i if in into is it
    its of on or our she so than that the their them then there these they this to was
    were what when where which who will with would you your about after all also any
    because before between both can could did do does down during each few first get got
    how just like more most much must no not now off once only other out over own said
    same some such still take too under until up very via we well while why within without
    """.split()
)

# Phrases that count as "this is not in the loaded headlines". Deliberately
# broad: the check is about refusing to invent, not about exact wording.
REFUSAL_CUES = (
    "not in the loaded",
    "not among the loaded",
    "no loaded headline",
    "not in this chat",
    "not included in the loaded",
    "no headlines",
    "only have",
    "only headlines",
    "not about",
    "get news",
    "don't have",
    "do not have",
    "cannot see",
    "can't see",
    "no article about",
    "no aurora",
    "not covered",
    "doesn't mention",
    "does not mention",
    "do not mention",
    "don't mention",
    "not mentioned",
    "couldn't find",
    "could not find",
    "no news about",
    "no information about",
    "no mention",
    "none of the loaded",
    "there is no",
    "there's no",
    "no reporting",
    "not in the headlines",
    # German, because the model sometimes answers in the wrong language; a
    # correct refusal must still count as one.
    "keine informationen",
    "keine artikel",
    "keine meldungen",
    "nicht in den geladenen",
    "nicht enthalten",
    "liegen keine",
)


def normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _stem(word: str) -> str:
    """Crude stem: drop punctuation and cut to five characters (raise/raised)."""
    return word.strip(".,;:!?()[]\"'").rstrip(".")[:5]


def _words(text: str) -> list[str]:
    return WORD_RE.findall((text or "").lower())


def stems(text: str, ignore: frozenset[str] = frozenset()) -> set[str]:
    return {
        _stem(word)
        for word in _words(text)
        if len(word) > 2 and word not in STOPWORDS and word not in ignore
    } - {""}


def expectations_met(answer: str, groups: list[list[str]]) -> bool:
    """True when every group has at least one option present in the answer."""
    text = normalize(answer)
    return all(any(normalize(option) in text for option in group) for group in groups)


def support_ratio(answer: str, corpus: str) -> float:
    """Share of answer sentences whose content words mostly exist in the corpus.

    A sentence is "supported" when at least half of its distinctive stems appear
    in the provided headlines/text. That catches invented specifics; it does not
    understand paraphrase, so treat it as a screening signal.
    """
    corpus_stems = stems(corpus)
    sentences = [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer or "")
        if len(_words(sentence)) >= 4
    ]
    if not sentences or not corpus_stems:
        return 1.0

    supported = 0
    for sentence in sentences:
        sentence_stems = stems(sentence)
        if not sentence_stems:
            supported += 1
            continue
        if len(sentence_stems & corpus_stems) / len(sentence_stems) >= 0.5:
            supported += 1
    return supported / len(sentences)


def _normalize_number(raw: str) -> str:
    """Normalises $1,200 / 1.200 / 0,238 so locales compare equal."""
    # A trailing comma/period is punctuation, not a decimal separator
    # ("installed before 2021," must not become "2021.").
    value = raw.replace("$", "").replace("\u00a0", "").replace(" ", "").rstrip(".,")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", value):
        return value.replace(".", "").replace(",", "")
    return value.replace(",", ".")


def _numbers(text: str) -> list[str]:
    return [_normalize_number(match) for match in NUMBER_RE.findall(text or "")]


def invented_numbers(answer: str, corpus: str) -> list[str]:
    """Numbers in the answer that do not appear in the provided articles."""
    allowed = set(_numbers(corpus))
    return [number for number in _numbers(answer) if number not in allowed]


def cites(answer: str, article: dict, topic: str = "") -> bool:
    """Whether the answer names this headline (or its source)."""
    source = normalize((article.get("source") or {}).get("name") or "")
    if source and source in normalize(answer):
        return True
    ignore = frozenset({_stem(topic.lower())}) if topic else frozenset()
    title_stems = stems(article.get("title") or "", ignore=ignore)
    return len(title_stems & stems(answer, ignore=ignore)) >= 2


NEGATION_WORDS = frozenset({"no", "not", "none", "nothing", "never", "keine", "nicht", "ohne"})
NEWS_WORDS = frozenset(
    {
        "headline", "headlines", "article", "articles", "loaded", "news", "information",
        "mention", "mentioned", "mentions", "report", "reported", "coverage", "story",
        "stories", "artikel", "meldung", "meldungen", "nachrichten",
    }
)


def refused(answer: str) -> bool:
    """True when the answer declines the loaded-headlines question.

    Either an explicit cue, or a negation next to a news word: a refusal in
    this app always talks about the headlines/articles it does not have, and
    the model rewords that sentence on almost every run.
    """
    text = normalize(answer)
    if any(cue in text for cue in REFUSAL_CUES):
        return True
    words = set(_words(text))
    negated = bool(words & NEGATION_WORDS) or "n't" in text
    return negated and bool(words & NEWS_WORDS)


def grade(prompt: dict, answer: str, tools: list | None, articles: list[dict]) -> dict:
    """Applies every check the prompt declares and returns a result record."""
    topic = prompt.get("topic") or ""
    corpus = " ".join(
        [topic]
        + [
            " ".join(
                str(field)
                for field in (
                    article.get("title"),
                    article.get("description"),
                    article.get("full_text"),
                    article.get("publishedAt"),
                )
                if field
            )
            for article in articles
        ]
    )
    tool_names = [
        item.get("tool") for item in (tools or []) if isinstance(item, dict) and item.get("tool")
    ]

    checks: dict[str, bool] = {}
    if prompt.get("expect"):
        checks["expect"] = expectations_met(answer, prompt["expect"])
    if prompt.get("must_call"):
        checks["must_call"] = all(name in tool_names for name in prompt["must_call"])
    if prompt.get("forbid_tools"):
        checks["no_tools"] = not tool_names
    if prompt.get("refusal"):
        checks["refusal"] = refused(answer)
    if prompt.get("forbid"):
        text = normalize(answer)
        checks["no_invented_topic"] = not any(
            normalize(word) in text for word in prompt["forbid"]
        )

    invented: list[str] = []
    if prompt.get("check_numbers"):
        invented = invented_numbers(answer, corpus)
        checks["no_invented_numbers"] = not invented

    unsupported = None
    if prompt.get("grade_faithfulness", True) and articles:
        supported = support_ratio(answer, corpus)
        unsupported = 1.0 - supported
        checks["faithfulness"] = unsupported <= float(prompt.get("max_unsupported", 0.34))

    cited = sum(1 for article in articles if cites(answer, article, topic))
    if prompt.get("min_citations"):
        checks["citations"] = cited >= int(prompt["min_citations"])

    return {
        "id": prompt["id"],
        "category": prompt["category"],
        "passed": bool(checks) and all(checks.values()),
        "checks": checks,
        "cited": cited,
        "unsupported": unsupported,
        "invented": invented,
        "answer": answer,
        "tools": tool_names,
    }
