"""Display names for reference keys, read once from the reference files."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from seawork.ingestion.references import load_yaml


class Names(BaseModel):
    model_config = ConfigDict(frozen=True)

    directions: dict[str, str]
    countries: dict[str, str]

    @classmethod
    def load(cls, reference_dir: Path) -> "Names":
        directions = load_yaml(reference_dir / "directions.yaml")["directions"]
        countries = load_yaml(reference_dir / "countries.yaml")["countries"]
        return cls(
            directions={row["key"]: row["title_ru"] for row in directions},
            countries={row["code"]: row["names"][0] for row in countries},
        )
