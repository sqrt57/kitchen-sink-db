"""Lint the local-markdown issue tracker under .scratch/ (see docs/agents/issue-tracker.md).

Every markdown file: relative links must resolve.
Wayfinder efforts (a directory with map.md): every ticket in issues/ must carry
a valid Type, Status and Blocked by line; resolved tickets need an Answer and a
pointer in the map's Decisions so far.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".scratch"
TICKET_NAME = re.compile(r"^\d{2}-[a-z0-9-]+\.md$")
TYPES = {"research", "prototype", "grilling", "task"}
STATUSES = {"open", "claimed", "resolved"}
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def field(text, name):
    m = re.search(rf"^{name}:[ \t]*(.*)$", text, re.MULTILINE)
    return None if m is None else m.group(1).strip()


def section(text, heading):
    m = re.search(rf"^## {heading}\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return None if m is None else m.group(1)


def check_links(path, text, errors):
    text = re.sub(r"<!--.*?-->|`[^`\n]*`", "", text, flags=re.DOTALL)
    for target in LINK.findall(text):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        if not (path.parent / target.split("#")[0]).exists():
            errors.append(f"{path}: broken link {target}")


def check_effort(effort, errors):
    map_text = (effort / "map.md").read_text(encoding="utf-8")
    decisions = section(map_text, "Decisions so far") or ""
    issues = effort / "issues"
    tickets = sorted(issues.glob("*.md")) if issues.is_dir() else []
    ids = {t.name[:2] for t in tickets}
    for t in tickets:
        if not TICKET_NAME.match(t.name):
            errors.append(f"{t}: name must be NN-slug.md")
        text = t.read_text(encoding="utf-8")
        if not text.startswith("# "):
            errors.append(f"{t}: first line must be '# <title>'")
        if field(text, "Type") not in TYPES:
            errors.append(f"{t}: Type must be one of {sorted(TYPES)}")
        status = field(text, "Status")
        if status not in STATUSES:
            errors.append(f"{t}: Status must be one of {sorted(STATUSES)}")
        blocked = field(text, "Blocked by")
        if blocked is None:
            errors.append(f"{t}: missing 'Blocked by:' line")
        else:
            for ref in filter(None, (b.strip() for b in blocked.split(","))):
                if ref not in ids or ref == t.name[:2]:
                    errors.append(f"{t}: Blocked by {ref} is not another ticket")
        if section(text, "Question") is None:
            errors.append(f"{t}: missing '## Question'")
        pointed = f"(issues/{t.name})" in decisions
        if status == "resolved":
            if section(text, "Answer") is None:
                errors.append(f"{t}: resolved without '## Answer'")
            if not pointed:
                errors.append(f"{t}: resolved but missing from map Decisions so far")
        elif pointed:
            errors.append(f"{t}: in map Decisions so far but Status is {status}")


def main():
    errors = []
    if SCRATCH.is_dir():
        for md in sorted(SCRATCH.rglob("*.md")):
            check_links(md, md.read_text(encoding="utf-8"), errors)
        for map_file in sorted(SCRATCH.glob("*/map.md")):
            check_effort(map_file.parent, errors)
    for e in errors:
        print(e.replace(str(ROOT) + "\\", "").replace(str(ROOT) + "/", ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
