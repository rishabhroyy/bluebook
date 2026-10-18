"""bluebook-ingest VIDEO [VIDEO...] --id SLUG --title "..." [--force-stage NAME]

Runs the full perception pipeline and writes data/skills/<id>/.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bluebook-ingest",
        description="Turn a task video into a SkillPackage (data/skills/<id>/).",
    )
    parser.add_argument("videos", nargs="+", type=Path, help="Input video file(s)")
    parser.add_argument(
        "--id", required=True, dest="skill_id", metavar="SLUG", help="Skill id (slug, e.g. fix-leaky-tap)"
    )
    parser.add_argument("--title", default=None, help="Skill title (auto-detected if omitted)")
    parser.add_argument(
        "--force-stage", default=None, metavar="NAME", help="Re-run from this stage onwards (invalidates cache)"
    )
    parser.add_argument("--transcriber", choices=["faster-whisper", "gemini"], default="faster-whisper")
    args = parser.parse_args()

    for v in args.videos:
        if not v.exists():
            print(f"error: video not found: {v}", file=sys.stderr)
            sys.exit(1)

    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    from .pipeline import run
    from .transcribe import FasterWhisperBackend, GeminiBackend

    transcriber = GeminiBackend() if args.transcriber == "gemini" else FasterWhisperBackend()

    pkg = await run(
        videos=args.videos,
        skill_id=args.skill_id,
        title=args.title,
        force_stage=args.force_stage,
        transcriber=transcriber,
    )
    print(f"Saved skill '{pkg.id}': {len(pkg.steps)} steps, {len(pkg.chunks)} chunks")
