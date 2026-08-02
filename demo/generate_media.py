#!/usr/bin/env python3
"""Generates the demo's synthetic media library — no copyrighted content,
everything produced locally by ffmpeg.

Six "content sets" (1..6) are invented, each with its own duration and its
own ~24 lines of made-up dialogue. `stub_transcriber.py` fingerprints a
transcription request purely by the (offset-adjusted) audio slice duration
it receives, so each content set's duration must be distinct: `duration =
60 + 6*c` seconds. `impostarr.assets.extract.extract_audio` slices from
offset 60s (for any file longer than that) up to 900s, so the slice length
the stub transcriber actually sees is `duration - 60 = 6*c` seconds — the
manifest below is keyed on that slice length, not the file's own duration.

Only four content sets (1, 2, 3, 5) are ever written into an actual episode
file — see ASSIGNMENTS. Content 4 and 6 exist only as reference subtitles
(content 4 is what a *correctly* labelled S01E04 would contain — never
generated as a file, so its slot is free for content 5's real home;
content 6 has no file at all, included purely for realism per the demo
spec). File slot 4 is deliberately mislabeled: it holds content 5's audio/
subs, which is how the demo's one "impostor" file is manufactured.

Outputs (relative to this file's directory):
  volumes/media/tv/<title>/Season 01/<Dotted.Title>.S01E0N.720p.mkv (4 files)
  volumes/manifest/manifest.json          (stub_transcriber's lookup table)
  volumes/staging/refsubs/S01E0{1..6}.srt (seed.sh places these under
                                            Impostarr's refsubs manual_dir
                                            once the TVDB id is known)
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
MEDIA_ROOT = DEMO_DIR / "volumes" / "media" / "tv"
STAGING_REFSUBS = DEMO_DIR / "volumes" / "staging" / "refsubs"
MANIFEST_PATH = DEMO_DIR / "volumes" / "manifest" / "manifest.json"

LINES_PER_EPISODE = 24  # comfortably above whisper-subs' default min_lines=20
VIDEO_SIZE = "640x360"
VIDEO_RATE = 24
AUDIO_HZ = 440

# content index -> (callsign, location), substituted into that content's
# own template pool below.
CONTENT_INFO = {
    1: ("Harmony Station", "Deck Three"),
    2: ("Signal Relay", "Comms Bay"),
    3: ("Greenhouse Dome", "Dome Seven"),
    4: ("Reactor Core", "Engineering"),
    5: ("Quarantine Wing", "Med Bay"),
    6: ("Archive Vault", "Vault Nine"),
}

# Each content set gets its own vocabulary-distinct template pool (rather
# than one shared pool with only name/location substituted) — the
# whisper-subs plugin's fuzzy match (rapidfuzz token_set_ratio) compares
# token *sets*, so sharing sentence structure/connective words across
# content sets was inflating cross-content similarity to ~0.8+ even for
# genuinely different episodes, defeating the demo's mismatch scenario.
# Distinct subject-matter vocabulary per set keeps genuine mismatches low
# and the true match (near-identical text) at ~1.0.
CONTENT_TEMPLATES: dict[int, list[str]] = {
    1: [
        "Hull plating cracked along frame nine near {loc}.",
        "Pressure alarm triggered on {loc}, sealing bulkhead {i}.",
        "Structural crew welding the breach on {loc}, panel {i}.",
        "{name} reports a widening fracture near {loc}.",
        "Emergency shoring holding at {loc} for now, cycle {i}.",
        "Vacuum warning cleared after patching {loc}, sector {i}.",
        "Rivets failing along the outer hull, near {loc}.",
        "Damage control team logging repairs at {loc}, entry {i}.",
    ],
    2: [
        "Cipher key rotated for the {i}th time today at {loc}.",
        "Static bursts jamming the uplink from {loc}.",
        "{name} decoded a fragment of the burst transmission, packet {i}.",
        "Antenna array realigned toward the relay satellite, pass {i}.",
        "Encrypted chatter intercepted near {loc}, frequency shift {i}.",
        "Handshake protocol failed twice before syncing at {loc}.",
        "Message queue backlog cleared at {loc}, batch {i}.",
        "Jamming source triangulated southeast of {loc}, bearing {i}.",
    ],
    3: [
        "Wilting leaves spotted on row {i} inside {loc}.",
        "Soil samples from {loc} show fungal spores, batch {i}.",
        "{name} quarantined the affected seedlings near {loc}.",
        "Irrigation lines flushed to slow the blight at {loc}.",
        "Pollen count dropping across {loc}, reading {i}.",
        "New sprouts emerging despite the blight, tray {i}.",
        "Nutrient mix adjusted for the crops at {loc}, formula {i}.",
        "Blight spreading slower than expected near {loc}, day {i}.",
    ],
    4: [
        "Coolant pressure dropping steadily in {loc}, gauge {i}.",
        "{name} sealed a hairline crack near the core manifold.",
        "Temperature spike logged at {loc}, reading {i} degrees.",
        "Backup pumps engaged to stabilize {loc}, cycle {i}.",
        "Radiation shielding holding steady around {loc}, check {i}.",
        "Valve replaced on the primary loop at {loc}.",
        "Reactor output throttled down while {loc} stabilizes, step {i}.",
        "Leak rate slowing after the patch at {loc}, hour {i}.",
    ],
    5: [
        "Fever spiking among patients in {loc}, chart {i}.",
        "{name} administered the antiviral batch at {loc}.",
        "New admissions logged overnight at {loc}, tally {i}.",
        "Isolation protocol holding steady across {loc}, shift {i}.",
        "Symptoms easing for several patients near {loc}.",
        "Blood samples sent from {loc} for testing, vial {i}.",
        "Ventilators running steady in {loc}, unit {i}.",
        "Recovery rate improving slightly at {loc}, day {i}.",
    ],
    6: [
        "Dust-covered ledgers uncovered deep in {loc}, shelf {i}.",
        "{name} catalogued another crate of old records at {loc}.",
        "Water damage found on scrolls near {loc}, box {i}.",
        "Microfilm reels recovered from {loc}, reel {i}.",
        "Missing census pages located inside {loc}, folder {i}.",
        "Sealed archive drawer pried open at {loc}, drawer {i}.",
        "Handwritten log fragments pieced together from {loc}.",
        "Restoration team scanning documents at {loc}, page {i}.",
    ],
}

# file slot (season-1 episode number as Sonarr will see it) -> content index.
# Slot 4 is the mislabel: it's really content 5's dialogue/duration.
ASSIGNMENTS = {1: 1, 2: 2, 3: 3, 4: 5}


def content_duration(content_idx: int) -> int:
    return 60 + 6 * content_idx


def dialogue_lines(content_idx: int) -> list[str]:
    name, loc = CONTENT_INFO[content_idx]
    templates = CONTENT_TEMPLATES[content_idx]
    lines = []
    for i in range(1, LINES_PER_EPISODE + 1):
        template = templates[(i - 1) % len(templates)]
        lines.append(template.format(name=name, loc=loc, i=i))
    return lines


def _srt_timestamp(seconds: float) -> str:
    millis_total = round(seconds * 1000)
    hours, rem = divmod(millis_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(lines: list[str], duration_s: float, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cue_len = duration_s / len(lines)
    blocks = []
    for i, text in enumerate(lines):
        start = _srt_timestamp(i * cue_len)
        end = _srt_timestamp((i + 1) * cue_len)
        blocks.append(f"{i + 1}\n{start} --> {end}\n{text}\n")
    out_path.write_text("\n".join(blocks), encoding="utf-8")


def build_episode_file(content_idx: int, out_path: Path) -> None:
    duration = content_duration(content_idx)
    srt_path = out_path.with_suffix(".embedded.srt")
    write_srt(dialogue_lines(content_idx), duration, srt_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={VIDEO_SIZE}:rate={VIDEO_RATE}:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={AUDIO_HZ}:duration={duration}",
            "-i",
            str(srt_path),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-c:s",
            "srt",
            "-t",
            str(duration),
            "-shortest",
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )
    srt_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Pioneer One", help="series title (default: Pioneer One)")
    args = parser.parse_args()

    dotted_title = args.title.replace(" ", ".")
    series_dir = MEDIA_ROOT / args.title / "Season 01"

    manifest: dict[str, list[str]] = {}
    for content_idx in range(1, 7):
        lines = dialogue_lines(content_idx)
        slice_duration = content_duration(content_idx) - 60
        manifest[str(slice_duration)] = lines
        write_srt(
            lines,
            content_duration(content_idx),
            STAGING_REFSUBS / f"S01E{content_idx:02d}.srt",
        )

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"manifest written: {MANIFEST_PATH}")

    for file_ep, content_idx in ASSIGNMENTS.items():
        out_path = series_dir / f"{dotted_title}.S01E{file_ep:02d}.720p.mkv"
        build_episode_file(content_idx, out_path)
        mislabel = f" (MISLABELED: really content {content_idx})" if content_idx != file_ep else ""
        print(f"built S01E{file_ep:02d} <- content {content_idx}{mislabel}: {out_path}")


if __name__ == "__main__":
    main()
