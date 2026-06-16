import structlog
import yaml
from pathlib import Path
from pydantic import ValidationError

from .models import Search

log = structlog.get_logger()


def load_searches(path: str | Path) -> list[Search]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Searches file not found: {path}\n"
            "Create it from the example: cp searches.yaml.example searches.yaml"
        )

    with path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, list):
        raise ValueError(
            f"searches.yaml must be a YAML list of search configs (got {type(raw).__name__})"
        )

    searches: list[Search] = []
    for i, item in enumerate(raw):
        try:
            searches.append(Search.model_validate(item))
        except ValidationError as exc:
            search_id = (
                item.get("id", f"item[{i}]") if isinstance(item, dict) else f"item[{i}]"
            )
            raise ValueError(
                f"Invalid search config for '{search_id}':\n{exc}"
            ) from exc

    ids = [s.id for s in searches]
    seen: set[str] = set()
    for sid in ids:
        if sid in seen:
            raise ValueError(
                f"Duplicate search id '{sid}'. Each search must have a unique id."
            )
        seen.add(sid)

    log.info("searches loaded", count=len(searches), ids=ids)
    return searches
