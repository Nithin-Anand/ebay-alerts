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


class _BlockStyleDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as literal blocks (|)."""


def _str_representer(dumper: yaml.Dumper, value: str) -> yaml.ScalarNode:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_BlockStyleDumper.add_representer(str, _str_representer)


_SAVE_HEADER = """\
# eBay Alerts — search definitions
# Each entry is one saved search. See docs/searches.md for the field reference.
# This file is rewritten by the web UI on every change, so hand-written
# comments below this header will be lost. Manual edits are picked up on
# the next restart.

"""


def save_searches(path: str | Path, searches: list[Search]) -> None:
    """
    Write the search list back to searches.yaml.

    Fields still at their default value are omitted to keep the file readable.
    The file is written in place (truncate + write) rather than via an atomic
    rename: with a Docker single-file bind mount, replacing the file would
    change the inode and detach it from the mount on the host.
    """
    payload = [s.model_dump(mode="json", exclude_defaults=True) for s in searches]
    text = _SAVE_HEADER + yaml.dump(
        payload,
        Dumper=_BlockStyleDumper,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    Path(path).write_text(text, encoding="utf-8")
    log.info("searches saved", count=len(searches), path=str(path))
