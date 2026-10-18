"""`bluebook` developer CLI.

    uv run bluebook bootstrap            # interactive: prereqs, .env, sample data, app deps
    uv run bluebook bootstrap --yes --api-key KEY   # non-interactive (agents / CI)
    uv run bluebook server               # same as bluebook-server
    uv run bluebook whisper              # download the Part 1 transcription model (~3 GB, once)
    uv run bluebook pack [--origin URL] [--check]   # build the glasses app .ehpk for Even Hub beta/store

Run from the repo root. Re-running bootstrap is safe: it shows current values as defaults
and keeps any .env keys it doesn't ask about.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import read_env

ENV = Path(".env")
# (key, prompt, default, secret)
FIELDS = [
    ("GEMINI_API_KEY", "Gemini API key (https://aistudio.google.com/apikey)", "", True),
    ("BB_DATA_DIR", "Data directory (skills, calibration, work files)", "data", False),
    ("BB_PORT", "Server port", "8787", False),
    ("BB_INTERACTIVE", "Enable experimental Part 3 AR mode? (0/1)", "0", False),
    ("TYPESAFE_API_KEY", "TypeSafe API key (Part 3 object boxes; needed for AR mode)", "", True),
]


def write_env(values: dict[str, str], path: Path = ENV) -> None:
    """Update keys in place, append new ones; comments and unrelated keys are kept."""
    lines = path.read_text().splitlines() if path.is_file() else []
    todo = dict(values)
    for i, line in enumerate(lines):
        k = line.partition("=")[0].strip()
        if k in todo and not line.lstrip().startswith("#"):
            lines[i] = f"{k}={todo.pop(k)}"
    lines += [f"{k}={v}" for k, v in todo.items()]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)  # holds an API key


def mask(v: str) -> str:
    return f"{v[:4]}…{v[-4:]}" if len(v) > 12 else ("set" if v else "")


def ask(prompt: str, current: str, secret: bool) -> str:
    shown = mask(current) if secret else current
    q = f"{prompt} [{shown}]: " if shown else f"{prompt}: "
    v = (getpass.getpass(q) if secret else input(q)).strip()
    return v or current


def check_key(key: str) -> str | None:
    """None if the key works, else the error. One cheap list-models call."""
    try:
        from google import genai

        c = genai.Client(api_key=key)  # keep a reference: the pager needs the client alive
        next(iter(c.models.list(config={"page_size": 1})))
        return None
    except Exception as e:  # noqa: BLE001 — SDK/network/auth errors vary; report, don't crash bootstrap
        return str(e).splitlines()[0][:200]


def tailscale() -> str | None:
    """This laptop's MagicDNS name if Tailscale is up."""
    exe = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    try:
        out = subprocess.run([exe, "status", "--json"], check=False, capture_output=True, text=True, timeout=5).stdout
        st = json.loads(out)
        return st["Self"]["DNSName"].rstrip(".") or None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None


WHISPER = "large-v3"  # FasterWhisperBackend's default model in bluebook_perception.transcribe


def whisper(local_only: bool = False) -> str | None:
    """Path of the cached faster-whisper model (downloads it unless local_only), None if absent."""
    from faster_whisper.utils import _MODELS, download_model

    if not local_only:  # download_model hides the progress bar; same repo + cache, plus a README
        from huggingface_hub import snapshot_download

        return snapshot_download(_MODELS[WHISPER])
    try:
        return download_model(WHISPER, local_files_only=True)
    except OSError:  # LocalEntryNotFoundError: not cached
        return None


def bootstrap(a: argparse.Namespace) -> int:
    if not Path("pyproject.toml").is_file() or not Path("apps/g2").is_dir():
        print("run this from the bluebook repo root", file=sys.stderr)
        return 1
    ok = True

    print("== prerequisites")
    for tool, why, required in [
        ("uv", "Python workspace", True),
        ("pnpm", "glasses app (npm i -g pnpm)", True),
        ("ffmpeg", "Part 1 video ingest (brew install ffmpeg)", False),
    ]:
        found = shutil.which(tool)
        print(f"  {'✓' if found else ('✗' if required else '-')} {tool:7} {why}")
        ok &= bool(found) or not required
    w = whisper(local_only=True)
    print(f"  {'✓' if w else '-'} whisper   {WHISPER if w else 'not cached: uv run bluebook whisper (3 GB)'}")
    host = tailscale()
    print(f"  {'✓' if host else '-'} tailscale {host or 'not running: phone camera page + packaged app need it'}")

    print("== .env")
    env = read_env()
    values = {k: env.get(k, default) for k, _, default, _ in FIELDS}
    if a.api_key:
        values["GEMINI_API_KEY"] = a.api_key
    if not a.yes:
        for k, prompt, _, secret in FIELDS:
            values[k] = ask(prompt, values[k], secret)
    if values["BB_INTERACTIVE"] not in ("0", "1") or not values["BB_PORT"].isdigit():
        print("  BB_INTERACTIVE must be 0/1 and BB_PORT a number", file=sys.stderr)
        return 1
    write_env(values)
    print(f"  wrote {ENV.resolve()} (mode 600)")
    if not values["GEMINI_API_KEY"]:
        print("  ! no GEMINI_API_KEY: the server runs, model calls will fail")
        ok = False
    elif not a.no_check:
        err = check_key(values["GEMINI_API_KEY"])
        print("  ✓ Gemini key works" if err is None else f"  ✗ Gemini key check failed: {err}")
        ok &= err is None

    print("== data")
    skills = Path(values["BB_DATA_DIR"]) / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    if not any(skills.iterdir()):
        for src in Path("fixtures/skills").iterdir():
            shutil.copytree(src, skills / src.name)
        print(f"  copied sample skill(s) into {skills}")
    else:
        print(f"  {skills} already has skills, left alone")

    print("== glasses app deps")
    if shutil.which("pnpm"):
        r = subprocess.run(["pnpm", "install", "--silent"], check=False, cwd="apps/g2")
        print("  ✓ pnpm install" if r.returncode == 0 else "  ✗ pnpm install failed")
        ok &= r.returncode == 0

    print("\n" + ("ready: ./scripts/dev.sh   (or --sim without glasses)" if ok else "finished with problems above"))
    return 0 if ok else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="bluebook")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap", help="set up this laptop: prereqs, .env, sample data, app deps")
    b.add_argument("--yes", "-y", action="store_true", help="don't prompt; keep current/default values")
    b.add_argument("--api-key", help="Gemini API key (else prompted, hidden)")
    b.add_argument("--no-check", action="store_true", help="skip the network check of the API key")
    sub.add_parser("server", help="run the dev server")
    sub.add_parser("whisper", help=f"download the faster-whisper {WHISPER} model for Part 1 ingest (~3 GB)")
    k = sub.add_parser("pack", help="build the glasses app as an .ehpk to upload to Even Hub (beta/store)")
    k.add_argument("--origin", help="https origin of the server the app talks to (default: this laptop's tailnet name)")
    k.add_argument("--check", action="store_true", help="validate manifest + package id with Even Hub (needs login)")
    a = p.parse_args()
    if a.cmd == "pack":
        if a.origin and not a.origin.startswith("https://"):
            p.error("--origin must start with https://")
        env = os.environ | ({"BB_ORIGIN": a.origin.rstrip("/")} if a.origin else {})
        sys.exit(subprocess.call(["scripts/pack-g2.sh", *(["-c"] if a.check else [])], env=env))
    elif a.cmd == "server":
        from .server import main as serve

        serve()
    elif a.cmd == "whisper":
        print(f"faster-whisper {WHISPER}: {whisper()}")
    else:
        sys.exit(bootstrap(a))
