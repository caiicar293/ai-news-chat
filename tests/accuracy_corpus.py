"""A frozen news corpus and labeled prompts for the accuracy eval.

The headlines are synthetic on purpose: the eval must be reproducible, so the
answers cannot depend on what NewsAPI happens to return today. The article
shape matches what NewsAPI returns plus `full_text`, which is what the tools
would hand the model after extraction.
"""

from __future__ import annotations

HELIOS = [
    {
        "title": "Helios raises $120 million to expand rooftop solar",
        "description": "The Series C round was led by Northwind Capital.",
        "url": "https://example.test/helios/funding",
        "publishedAt": "2026-11-23T09:00:00Z",
        "source": {"name": "Northwind Wire"},
        "content": "Helios raised $120 million in a Series C round.",
        "full_text": (
            "Helios said on Monday it raised $120 million in a Series C round led by "
            "Northwind Capital. The company plans to use the money to expand its rooftop "
            "solar installations across the Midwest and to hire 150 installers by the end "
            "of 2027."
        ),
        "text_source": "full_scrape",
    },
    {
        "title": "Helios cuts panel prices by 15 percent",
        "description": "Residential panels get cheaper from January.",
        "url": "https://example.test/helios/prices",
        "publishedAt": "2026-11-23T11:30:00Z",
        "source": {"name": "Northwind Wire"},
        "content": "Panel prices fall by 15 percent.",
        "full_text": (
            "Helios will cut the price of its residential solar panels by 15 percent from "
            "January, taking the cost per watt from $0.28 to $0.238. That is the equivalent "
            "of 23.8 cents per watt. The company said cheaper cells and a new factory line "
            "drove the reduction."
        ),
        "text_source": "full_scrape",
    },
    {
        "title": "Helios opens panel factory in Ohio, hiring 400",
        "description": "The Toledo plant will make 2 gigawatts of panels a year.",
        "url": "https://example.test/helios/factory",
        "publishedAt": "2026-11-24T07:15:00Z",
        "source": {"name": "Midwest Daily"},
        "content": "A new factory opens in Toledo.",
        "full_text": (
            "Helios opened a panel factory in Toledo, Ohio, on Tuesday. The plant will "
            "employ 400 people and produce 2 gigawatts of panels a year, the company said. "
            "State officials attended the opening."
        ),
        "text_source": "full_scrape",
    },
    {
        "title": "Helios sued over warranty claims on older panels",
        "description": "Homeowners say warranties were refused.",
        "url": "https://example.test/helios/lawsuit",
        "publishedAt": "2026-11-22T16:40:00Z",
        "source": {"name": "Courthouse Ledger"},
        "content": "A lawsuit was filed in Delaware.",
        "full_text": (
            "A group of 1,200 homeowners filed a lawsuit against Helios in Delaware state "
            "court on Friday, saying the company refused to honour warranties on panels "
            "installed before 2021. Helios called the claims 'without merit' and said it "
            "would defend the case."
        ),
        "text_source": "full_scrape",
    },
]

AURORA = [
    {
        "title": "Aurora probe sends first images of Europa's plumes",
        "description": "The Aurora-2 probe imaged water plumes above Europa.",
        "url": "https://example.test/aurora/europa",
        "publishedAt": "2026-11-23T18:05:00Z",
        "source": {"name": "Orbit Report"},
        "content": "The probe returned images of plumes.",
        "full_text": (
            "The Aurora-2 probe returned its first images of water plumes above Europa, "
            "taken from 340 kilometres above the surface. Mission scientists said the "
            "plumes rise about 200 kilometres."
        ),
        "text_source": "full_scrape",
    },
    {
        "title": "Aurora delays third satellite launch to March 14",
        "description": "A faulty valve pushed the launch back.",
        "url": "https://example.test/aurora/launch",
        "publishedAt": "2026-11-21T12:00:00Z",
        "source": {"name": "Orbit Report"},
        "content": "The launch slips to March.",
        "full_text": (
            "Aurora Space delayed the launch of its third weather satellite to March 14, "
            "2027, citing a faulty valve in the second stage. The satellite was originally "
            "due in December."
        ),
        "text_source": "full_scrape",
    },
    {
        "title": "Aurora names Maya Chen chief engineer",
        "description": "Chen previously led the propulsion team.",
        "url": "https://example.test/aurora/engineer",
        "publishedAt": "2026-11-20T09:45:00Z",
        "source": {"name": "Orbit Report"},
        "content": "Aurora appointed a new chief engineer.",
        "full_text": (
            "Aurora Space appointed Maya Chen as chief engineer. Chen previously led the "
            "propulsion team at the company and replaces Daniel Ortiz, who retires in April."
        ),
        "text_source": "full_scrape",
    },
]

ARTICLES: dict[str, list[dict]] = {"Helios": HELIOS, "Aurora": AURORA}

# Each prompt is labeled with the facts a correct answer must contain. `expect`
# is a list of groups: one option from every group must appear in the answer.
PROMPTS: list[dict] = [
    {
        "id": "helios-funding",
        "topic": "Helios",
        "category": "lookup",
        "question": "How much did Helios raise, and who led the round?",
        "expect": [["120"], ["northwind"]],
        "check_numbers": True,
        "min_citations": 1,
    },
    {
        "id": "helios-price",
        "topic": "Helios",
        "category": "lookup",
        "question": "What is Helios's new price per watt for its residential panels?",
        "expect": [["0.238", "23.8", "0,238", "23,8"]],
        "check_numbers": True,
        "min_citations": 1,
    },
    {
        "id": "helios-lawsuit-detail",
        "topic": "Helios",
        "category": "detail",
        "question": "What exactly does the warranty lawsuit against Helios claim? Quote the numbers.",
        "expect": [["1,200", "1200", "1.200"], ["warrant"]],
        "must_call": ["fetch_article_full_text_tool"],
        "check_numbers": True,
        "min_citations": 1,
    },
    {
        "id": "helios-jobs",
        "topic": "Helios",
        "category": "cross",
        "question": "Which Helios story is about new jobs, and how many?",
        "expect": [["ohio", "toledo"], ["400"]],
        "check_numbers": True,
        "min_citations": 1,
    },
    {
        "id": "helios-top",
        "topic": "Helios",
        "category": "ranking",
        "question": "What's the biggest Helios story today?",
        "min_citations": 1,
    },
    {
        "id": "helios-missing-ceo",
        "topic": "Helios",
        "category": "refusal",
        "question": "What did Helios say about its CEO resigning last week?",
        "refusal": True,
        # A refusal talks about what is missing, so corpus overlap says nothing.
        "grade_faithfulness": False,
    },
    {
        "id": "aurora-not-loaded",
        "topic": "Helios",
        "category": "isolation",
        "question": "What's the latest Aurora Space news?",
        "refusal": True,
        "grade_faithfulness": False,
        "forbid": ["Europa", "340", "March 14", "Maya Chen", "Daniel Ortiz"],
    },
    {
        "id": "no-news-state",
        "topic": None,
        "category": "no_news",
        "question": "What are the top stories right now?",
        "expect": [["get news"]],
        "forbid_tools": True,
        "grade_faithfulness": False,
    },
    {
        "id": "plain-chat",
        "topic": None,
        "category": "plain",
        "question": "Hi! What can you help me with?",
        "forbid_tools": True,
        "grade_faithfulness": False,
    },
    {
        "id": "helios-summary",
        "topic": "Helios",
        "category": "summary",
        "question": "Summarise today's Helios headlines in three short bullets.",
        "min_citations": 2,
        "max_unsupported": 0.4,
    },
]
