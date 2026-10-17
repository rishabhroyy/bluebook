"""Skill library on disk. Part 1 writes with save(); Parts 2/3 read with load()/search().

ponytail: brute-force cosine over a per-skill .npy. A skill has hundreds of chunks, not
millions; switch to sqlite-vec/LanceDB only if cross-skill search over a big library is needed.
"""

from __future__ import annotations

import json
import re
import shutil
from functools import lru_cache
from pathlib import Path

import numpy as np

from .config import config
from .skill import SCHEMA_VERSION, Chunk, SkillPackage


def skill_dir(skill_id: str, root: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", skill_id):  # also blocks path traversal
        raise ValueError(f"bad skill id: {skill_id!r}")
    return (root or config.skills_dir) / skill_id


def list_skills(root: Path | None = None) -> list[SkillPackage]:
    root = root or config.skills_dir
    return sorted((load(p.parent.name, root) for p in root.glob("*/skill.json")), key=lambda s: s.title)


def load(skill_id: str, root: Path | None = None) -> SkillPackage:
    return _load(skill_dir(skill_id, root))


@lru_cache(maxsize=32)
def _load(d: Path) -> SkillPackage:
    pkg = SkillPackage.model_validate_json((d / "skill.json").read_text())
    if pkg.schema_version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise ValueError(f"{d}: schema {pkg.schema_version}, expected {SCHEMA_VERSION}")
    return pkg


def save(pkg: SkillPackage, embeddings: np.ndarray | None = None, root: Path | None = None) -> Path:
    """Validate, write media-side files first, skill.json last via atomic rename (readers never see a half package)."""
    if errs := pkg.check():
        raise ValueError("invalid skill package:\n  " + "\n  ".join(errs))
    d = skill_dir(pkg.id, root)
    d.mkdir(parents=True, exist_ok=True)
    if embeddings is not None:
        assert pkg.embedding and embeddings.shape == (len(pkg.chunks), pkg.embedding.dim), "embeddings/chunks mismatch"
        np.save(d / pkg.embedding.file, embeddings.astype(np.float32))
    tmp = d / "skill.json.tmp"
    tmp.write_text(pkg.model_dump_json(indent=2))
    tmp.replace(d / "skill.json")
    _load.cache_clear()
    return d


def delete(skill_id: str, root: Path | None = None) -> None:
    shutil.rmtree(skill_dir(skill_id, root))
    _load.cache_clear()


def media_path(skill_id: str, rel: str, root: Path | None = None) -> Path:
    d = skill_dir(skill_id, root).resolve()
    p = (d / rel).resolve()
    if not p.is_relative_to(d):
        raise ValueError("path escapes package")
    return p


async def search(pkg: SkillPackage, query: str, k: int = 5, root: Path | None = None) -> list[tuple[Chunk, float]]:
    """Top-k chunks for a query. Uses embeddings if the package has them, else keyword overlap."""
    if pkg.embedding and (f := skill_dir(pkg.id, root) / pkg.embedding.file).exists():
        from . import models

        if models.model_id("embedder") != pkg.embedding.model:
            raise ValueError(f"package embedded with {pkg.embedding.model}, config uses {models.model_id('embedder')}")
        m = np.load(f)
        q = (await models.embed([query], task="RETRIEVAL_QUERY"))[0]
        scores = m @ q
    else:
        words = set(re.findall(r"\w+", query.lower()))
        scores = np.array(
            [len(words & set(re.findall(r"\w+", c.text.lower()))) / (len(words) or 1) for c in pkg.chunks]
        )
    top = np.argsort(-scores)[:k]
    return [(pkg.chunks[i], float(scores[i])) for i in top if scores[i] > 0]


def export_schema(path: Path) -> None:
    path.write_text(json.dumps(SkillPackage.model_json_schema(), indent=2))
