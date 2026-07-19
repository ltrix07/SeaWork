from enum import IntEnum, StrEnum


class OpportunityType(StrEnum):
    JOB = "job"
    INTERNSHIP = "internship"
    VOLUNTEER = "volunteer"
    COURSE = "course"
    CERTIFICATION = "certification"
    ARTICLE = "article"
    NEWS = "news"
    RESEARCH = "research"
    EVENT = "event"


class Provenance(StrEnum):
    SOURCE = "source"
    RULE = "rule"
    LLM = "llm"


class ExperienceLevel(StrEnum):
    ENTRY = "entry"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"


class SourceTier(IntEnum):
    OPEN = 1
    INTERNAL = 2
    PAID = 3
    HTML = 4


class SourceTrust(StrEnum):
    PRIMARY = "primary"
    AGGREGATED = "aggregated"
    UNTRUSTED = "untrusted"


class OpportunityStatus(StrEnum):
    ACTIVE = "active"
    REJECTED = "rejected"
