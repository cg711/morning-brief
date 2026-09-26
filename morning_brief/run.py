"""python -m morning_brief.run            generate today's episode now
python -m morning_brief.run voices A B   render a sample sentence per Kokoro voice into data/voice-samples/"""
from __future__ import annotations

import argparse
import logging
import sys

from . import db, pipeline, speech
from .config import Settings

VOICE_SAMPLE = (
    "Good morning, it's Friday, September twenty-fifth. MPR News reports that the city council "
    "passed next year's budget last night, seven votes to six, after a four-hour debate."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m morning_brief.run")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("generate", help="generate today's episode (default)")
    voices = sub.add_parser("voices", help="render voice samples")
    voices.add_argument("names", nargs="+")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # phonemizer logs a harmless "words count mismatch" WARNING for nearly every passage.
    logging.getLogger("phonemizer").setLevel(logging.ERROR)

    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    db.migrate(conn)

    if args.cmd == "voices":
        out = settings.data_dir / "voice-samples"
        out.mkdir(exist_ok=True)
        with pipeline.run_lock(settings):
            for name in args.names:
                mp3, _ = speech.synthesize([VOICE_SAMPLE], name, settings.models_dir)
                (out / f"{name}.mp3").write_bytes(mp3)
                print(out / f"{name}.mp3")
        return 0

    if not settings.daily_brief:
        print("daily brief is off (DAILY_BRIEF=0); not calling the API")
        return 2

    deps = pipeline.default_deps(settings)
    try:
        run_id = pipeline.run_episode(deps, trigger="cli")
    finally:
        deps.http.close()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    print({k: run[k] for k in run.keys()})
    return 0 if run["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
