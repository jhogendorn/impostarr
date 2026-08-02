#!/usr/bin/env python3
"""Generates the demo's synthetic media library — no copyrighted content,
everything produced locally by ffmpeg.

Six "content sets" (1..6) are invented, each with its own duration and its
own ~24 lines of made-up dialogue (one of which is a deterministic "Log
entry episode <N>" marker — see `dialogue_lines()` — parsed by
`demo/stub_services.py`'s stub LLM to answer "which episode is this
really", the same way `_match_ratio` answers it for whisper-subs).
`stub_services.py`'s transcription endpoint fingerprints a transcription
request purely by the (offset-adjusted) audio slice duration it receives,
so each content set's duration must be distinct: `duration = 60 + 6*c`
seconds. `impostarr.assets.extract.extract_audio` slices from offset 60s
(for any file longer than that) up to 900s, so the slice length the stub
transcriber actually sees is `duration - 60 = 6*c` seconds — the manifest
below is keyed on that slice length, not the file's own duration.

Five files are written into the library (ASSIGNMENTS), exercising a full
scenario matrix across both identifier plugins — whisper-subs (transcribed
audio vs. reference subs) and subs-llm (embedded subs vs. an LLM):

  S01E01 <- content 1, honest             -> matched (both plugins agree)
  S01E02 <- content 2, no embedded subs   -> inconclusive (subs-llm: no
            (audio kept — Sonarr's own       embedded subs; whisper-subs:
             HasAudioTrackSpecification       its audio is real but
             permanently rejects import       deliberately excluded from
             of an audio-less file — see      the manifest below, so the
             NO_SUBS_SLOT)                    stub transcriber returns zero
                                              segments — neither plugin has
                                              any evidence to work with)
  S01E03 <- content 6, mislabeled         -> remediated (dry-run remap to
            (S01E06 left empty on disk)      S01E06 — no competing file)
  S01E04 <- content 5, mislabeled         -> quarantine (auto-remap proposes
            (S01E05 also honestly           S01E05 but refuses: target
             holds content 5)                already occupied); also
                                              triggers dupe_info (near-
                                              identical to S01E05)
  S01E05 <- content 5, honest             -> matched (the competitor E04's
                                              remap can't take)

Content 3 and 4 exist only as reference subtitles, never embedded into a
file: content 4 is what a *correctly* labelled S01E04 would have contained
(its slot is occupied by S01E05 instead); content 3's honest home (S01E03)
is where the S01E03 mislabel lives instead.

Outputs (relative to this file's directory):
  volumes/media/tv/<title>/Season 01/<Dotted.Title>.S01E0N.720p.mkv (5 files)
  volumes/manifest/manifest.json          (stub_services' transcription
                                            lookup table)
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
# Slot 3 and slot 4 are the mislabels: slot 3 really holds content 6's
# dialogue/duration (its honest home, S01E06, is left empty on disk so the
# remap has a free slot to land in); slot 4 really holds content 5's — but
# so does slot 5 (content 5's honest home), creating an occupied-target
# remap competition. Slot 2 has no embedded subs (see NO_SUBS_SLOT) — the
# inconclusive case.
ASSIGNMENTS = {1: 1, 2: 2, 3: 6, 4: 5, 5: 5}

# File slot with no embedded subtitle stream muxed in (see
# `build_episode_file`'s `include_subs`) — the inconclusive case. Its
# audio track is kept real (Sonarr's own `HasAudioTrackSpecification`
# permanently rejects import of any file with no audio track at all — a
# genuinely video-only mkv can never reach Impostarr in the first place),
# but its content index is deliberately excluded from the manifest below,
# so the stub transcriber returns zero segments for it: whisper-subs
# abstains ("transcript too short") the same as if there were no audio,
# and subs-llm abstains ("no embedded subtitles") for real.
NO_SUBS_SLOT = 2


def content_duration(content_idx: int) -> int:
    return 60 + 6 * content_idx


def dialogue_lines(content_idx: int) -> list[str]:
    name, loc = CONTENT_INFO[content_idx]
    templates = CONTENT_TEMPLATES[content_idx]
    # Deterministically parseable episode marker for the subs-llm stub
    # (demo/stub_services.py: `_EPISODE_CUE_RE`) — every content set states
    # its own canonical episode number this way, so the stub can answer
    # "which episode is this really" straight from the transcribed/embedded
    # dialogue text, the same way whisper-subs' fuzzy match does via
    # content-distinct vocabulary. Counts toward LINES_PER_EPISODE (total
    # line count unchanged), so whisper-subs' min_lines threshold is
    # unaffected.
    lines = [f"Log entry episode {content_idx}: {name} status update from {loc}."]
    for i in range(1, LINES_PER_EPISODE):
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


def build_episode_file(
    content_idx: int, out_path: Path, *, include_audio: bool = True, include_subs: bool = True
) -> None:
    """`include_audio=False` produces a video-only mkv — unused by any
    slot in practice (Sonarr's `HasAudioTrackSpecification` permanently
    rejects importing one), kept only because it's the natural symmetric
    counterpart to `include_subs=False`, which `NO_SUBS_SLOT` does use to
    produce a file with no muxed subtitle stream."""
    duration = content_duration(content_idx)
    srt_path = out_path.with_suffix(".embedded.srt") if include_subs else None
    if srt_path is not None:
        write_srt(dialogue_lines(content_idx), duration, srt_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={VIDEO_SIZE}:rate={VIDEO_RATE}:duration={duration}",
    ]
    if include_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={AUDIO_HZ}:duration={duration}"]
    if srt_path is not None:
        cmd += ["-i", str(srt_path)]

    cmd += ["-map", "0:v"]
    if include_audio:
        cmd += ["-map", "1:a"]
    if srt_path is not None:
        sub_input = 2 if include_audio else 1
        cmd += ["-map", f"{sub_input}:s"]

    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if include_audio:
        cmd += ["-c:a", "aac"]
    if srt_path is not None:
        cmd += ["-c:s", "srt"]
    cmd += ["-t", str(duration), "-shortest", str(out_path)]

    subprocess.run(cmd, check=True, capture_output=True)
    if srt_path is not None:
        srt_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Pioneer One", help="series title (default: Pioneer One)")
    args = parser.parse_args()

    dotted_title = args.title.replace(" ", ".")
    series_dir = MEDIA_ROOT / args.title / "Season 01"

    # NO_SUBS_SLOT's content is deliberately excluded from the manifest —
    # its audio is real (so Sonarr will import the file) but the stub
    # transcriber must never recognize its duration, so whisper-subs sees
    # zero transcript segments (see NO_SUBS_SLOT's comment above).
    no_subs_content = ASSIGNMENTS[NO_SUBS_SLOT]

    manifest: dict[str, list[str]] = {}
    for content_idx in range(1, 7):
        lines = dialogue_lines(content_idx)
        slice_duration = content_duration(content_idx) - 60
        if content_idx != no_subs_content:
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
        has_subs = file_ep != NO_SUBS_SLOT
        build_episode_file(content_idx, out_path, include_audio=True, include_subs=has_subs)
        mislabel = f" (MISLABELED: really content {content_idx})" if content_idx != file_ep else ""
        stripped = " (no embedded subs, unmatched audio)" if not has_subs else ""
        print(f"built S01E{file_ep:02d} <- content {content_idx}{mislabel}{stripped}: {out_path}")


if __name__ == "__main__":
    main()
