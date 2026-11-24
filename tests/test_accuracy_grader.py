"""Unit tests for the accuracy graders (offline, no Gemini calls)."""

from __future__ import annotations

from accuracy_grader import (
    cites,
    expectations_met,
    grade,
    invented_numbers,
    refused,
    support_ratio,
)

CORPUS = (
    "Helios raised $120 million in a round led by Northwind Capital. "
    "Helios will cut panel prices by 15 percent, to $0.238 per watt."
)
PRICE_TITLE = "Helios cuts panel prices by 15 percent"
PRICE_ARTICLE = {
    "title": PRICE_TITLE,
    "description": "Cheaper panels.",
    "full_text": CORPUS,
    "publishedAt": "2026-11-23T11:30:00Z",
    "source": {"name": "Northwind Wire"},
}


def test_supported_sentences_score_one():
    assert support_ratio("Helios raised $120 million led by Northwind Capital.", CORPUS) == 1.0


def test_invented_sentence_is_flagged():
    answer = (
        "Helios raised $120 million led by Northwind Capital. "
        "Helios bought a French vineyard for 900 million euros."
    )
    assert support_ratio(answer, CORPUS) < 1.0


def test_invented_numbers_are_detected():
    assert invented_numbers("Helios raised $120 million.", CORPUS) == []
    assert invented_numbers("Helios raised $900 million.", CORPUS) == ["900"]


def test_numbers_compare_across_locales():
    corpus = "A group of 1,200 homeowners filed suit, panels cost $0.238 per watt."
    assert invented_numbers("Es waren 1.200 Hausbesitzer.", corpus) == []
    assert invented_numbers("Es kostet 0,238 pro Watt.", corpus) == []
    assert invented_numbers("Es waren 1.900 Hausbesitzer.", corpus) == ["1900"]


def test_trailing_punctuation_is_not_a_decimal():
    corpus = "panels installed before 2021 were covered. $120 million in total."
    assert invented_numbers("installed before 2021, Helios said", corpus) == []


def test_refusal_with_negation_and_news_context():
    assert refused("The provided articles do not discuss a CEO resignation.")
    assert refused("There is nothing in the loaded coverage about that.")
    assert not refused("Helios raised $120 million, and the article says prices will fall.")


def test_expectation_groups_allow_alternatives():
    assert expectations_met("That works out to 23.8 cents per watt", [["0.238", "23.8"]])
    assert not expectations_met("That works out to 30 cents", [["0.238", "23.8"]])
    assert expectations_met("Helios raised $120 million", [["120"], ["million"]])
    assert not expectations_met("Helios raised $120 million", [["120"], ["northwind"]])


def test_refusal_detection():
    assert refused("That story is not in the loaded headlines.")
    assert refused("I only have Helios headlines loaded, so no Aurora news.")
    assert not refused("Helios raised $120 million.")


def test_refusal_detection_covers_observed_phrasings():
    # These are real answers from gemini-3.5-flash-lite to the missing-CEO prompt.
    assert refused(
        "Based on the available news articles, there is no mention of Helios's CEO resigning."
    )
    assert refused("None of the loaded articles mention a CEO resigning.")
    assert refused("The loaded headlines do not mention a CEO resigning.")
    assert refused("I couldn't find any news about Aurora Space in the loaded articles.")


def test_citation_uses_title_overlap():
    assert cites("Helios will cut panel prices by 15 percent from January.", PRICE_ARTICLE, "Helios")
    assert not cites("Helios raised $120 million from Northwind.", PRICE_ARTICLE, "Helios")


def test_grade_passes_a_correct_answer():
    prompt = {
        "id": "price",
        "category": "lookup",
        "topic": "Helios",
        "expect": [["0.238"]],
        "check_numbers": True,
        "min_citations": 1,
    }

    result = grade(
        prompt,
        "Helios will cut panel prices by 15 percent, to 0.238 per watt.",
        [],
        [PRICE_ARTICLE],
    )

    assert result["passed"], result
    assert result["checks"]["expect"] is True
    assert result["invented"] == []
    assert result["unsupported"] == 0.0


def test_grade_fails_an_invented_answer():
    prompt = {
        "id": "price",
        "category": "lookup",
        "topic": "Helios",
        "expect": [["0.238"]],
        "check_numbers": True,
        "min_citations": 1,
    }

    result = grade(
        prompt,
        "Helios bought a French vineyard for 900 million euros.",
        [],
        [PRICE_ARTICLE],
    )

    assert not result["passed"]
    assert result["checks"]["expect"] is False
    assert "900" in result["invented"]


def test_grade_checks_tool_requirements():
    prompt = {
        "id": "detail",
        "category": "detail",
        "topic": "Helios",
        "must_call": ["fetch_article_full_text_tool"],
        "grade_faithfulness": False,
    }

    without_tool = grade(prompt, "The lawsuit is about warranties.", [], [PRICE_ARTICLE])
    with_tool = grade(
        prompt,
        "The lawsuit is about warranties.",
        [{"tool": "fetch_article_full_text_tool", "detail": "index=3"}],
        [PRICE_ARTICLE],
    )

    assert without_tool["passed"] is False
    assert with_tool["passed"] is True
