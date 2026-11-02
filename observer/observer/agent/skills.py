"""Skills: recipes for recurring analyses, shareable between houses.

A skill is a folder ``<name>/`` with ``SKILL.md`` — YAML front matter
(title, description, tools, defaults, max_steps) and the recipe in Markdown —
plus optional helper files (Python modules, tests). Helpers are copied into
the run's workspace under ``skill/`` so code in the sandbox can import them.

Recipes speak the house model's vocabulary (circuits, roles, locations),
never one house's signal ids; the agent maps them through the registry and
the house description. Skills come from the package (``observer/skills``, in
git) and from the data directory (``<OBSERVER_HOME>/skills``, house-local),
the latter overriding the former.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

import yaml

PACKAGE_SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


@dataclass
class Skill:
    name: str
    title: str
    description: str
    recipe: str
    folder: str
    tools: list[str] | None = None
    defaults: dict = field(default_factory=dict)
    max_steps: int = 25
    origin: str = "package"

    def helpers(self) -> list[str]:
        return sorted(f for f in os.listdir(self.folder)
                      if f != "SKILL.md" and not f.startswith((".", "__")) and os.path.isfile(os.path.join(self.folder, f)))

    def install(self, workspace: str) -> list[str]:
        """Copy the helper files to <workspace>/skill/."""
        files = self.helpers()
        if files:
            target = os.path.join(workspace, "skill")
            os.makedirs(target, exist_ok=True)
            for f in files:
                shutil.copyfile(os.path.join(self.folder, f), os.path.join(target, f))
        return [f"skill/{f}" for f in files]

    def view(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description, "tools": self.tools,
                "defaults": self.defaults, "max_steps": self.max_steps, "origin": self.origin,
                "helpers": self.helpers(), "recipe": self.recipe}


def parse(folder: str, origin: str) -> Skill:
    with open(os.path.join(folder, "SKILL.md"), encoding="utf-8") as fp:
        text = fp.read()
    meta, body = {}, text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
    name = os.path.basename(os.path.normpath(folder))
    return Skill(name=name, title=meta.get("title") or name, description=meta.get("description") or "",
                 recipe=body.strip(), folder=folder, tools=meta.get("tools"), defaults=meta.get("defaults") or {},
                 max_steps=int(meta.get("max_steps") or 25), origin=origin)


def load(extra_dirs: list[str] | None = None) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for root, origin in [(PACKAGE_SKILLS, "package")] + [(d, "house") for d in (extra_dirs or [])]:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            if os.path.isfile(os.path.join(folder, "SKILL.md")):
                skills[name] = parse(folder, origin)
    return skills
