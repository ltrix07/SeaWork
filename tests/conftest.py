from pathlib import Path

import pytest


@pytest.fixture
def reference_dir() -> Path:
    return Path("data/reference")


@pytest.fixture
def pinpoint_posting() -> dict[str, object]:
    return {
        "id": "43999",
        "url": "https://hollandamericagroup.pinpointhq.com/postings/43999",
        "title": "SBN - Chef De Partie - CSSI",
        "description": "<div>Directs and supervises preparation of food for guests and crew.</div>",
        "key_responsibilities_header": "Responsibilities",
        "key_responsibilities": "<ul><li>Supervises the assigned galley station.</li></ul>",
        "skills_knowledge_expertise_header": "Requirements",
        "skills_knowledge_expertise": (
            "<p>Minimum 3 years experience. STCW Basic Safety Training required.</p>"
        ),
        "benefits_header": "Travel Requirements",
        "benefits": "<p>Passport and C1/D visa required.</p>",
        "employment_type": "fixed_term_contract",
        "compensation": None,
        "location": {"name": "India - CSSI", "city": "Kurla West, Mumbai"},
        "job": {
            "division": {"name": "Hotel"},
            "department": {"name": "Galley"},
            "structure_custom_group_one": {"name": "Seabourn"},
        },
    }
