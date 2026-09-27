# cuesheet

**The music cue sheet and archive-usage report your festival checklist asks for,
straight from the edit.** `cuesheet` reads a timeline export (CMX3600 EDL from
DaVinci Resolve/Premiere/Avid, or FCPXML from Final Cut / Resolve) and produces:

- a **music cue sheet** — cue number, title, composer, publisher, PRO, usage
  (BI/BV/VI/VV…), TC in/out, duration;
- a **third-party footage report** — every archive / stock use with owner,
  licence and screen time;
- a **rights-gap list** — every source whose rights data is still missing.
  With `--strict` it exits `2`, so it can gate a delivery script next to
  [`speccheck`](https://github.com/Obrais-cloud/speccheck) (technical QC →
  rights QC).

Deterministic, **stdlib only** (Python 3.11+), no API keys, no cloud.

## Why

Festivals, broadcasters and sales agents want a cue sheet and proof of what
archive you used and for how long. Doing it by hand means scrubbing the timeline,
adding up seconds, and remembering that the A1/A2 stereo pair is one cue, not
two. The edit already has all of this; `cuesheet` just reads it.

## Use

```bash
# 1. Resolve: File → Export → Timeline → EDL (export the video track and each
#    audio track; pass them all at once — they're merged on record TC)
./cuesheet.py FDV_v12_V.edl FDV_v12_A1.edl FDV_v12_A2.edl --init > rights.toml

# 2. Fill in rights.toml (title, composer, publisher, pro, usage / owner, license)

# 3. Generate the cue sheet
./cuesheet.py FDV_v12_*.edl -r rights.toml                 # Markdown
./cuesheet.py FDV_v12_*.edl -r rights.toml -f csv -o cues.csv
./cuesheet.py FDV_FCP.fcpxml -r rights.toml -f json

# 4. In a delivery script: fail if any cue lacks rights data
./cuesheet.py FDV_v12_*.edl -r rights.toml --strict -o cues.md && speccheck master.mov --spec festival-prores
```

Options: `--fps` (EDLs carry no rate; default 25, drop-frame read from `FCM:`),
`--merge-gap SECONDS` (join uses of the same cue separated by a short gap),
`--only music|archive|stock` (repeatable).

Sample run on the bundled fixtures:

```bash
./cuesheet.py samples/fdv_video.edl samples/fdv_audio.edl -r samples/fdv_rights.toml
```

## How it decides what's what

Built-in rules (override them with `[[category]]` tables in the TOML):

| Category | Matches (clip name or file path) | Required fields |
|---|---|---|
| `music` (audio only) | `*.wav/aif/mp3/flac/m4a`, `MUS_*`, `*/Music/*`, `*/Musica/*` — excluding `*sync*`, `*VO*`, `*dialog*`, `*/SFX/*` | title, composer, publisher, pro, usage |
| `archive` | `ARCH_*`, `ARCHIVO_*`, `*/Archive/*`, `*/Archivo/*` | owner, license |
| `stock` | `STOCK_*`, `*/Stock/*`, pond5, shutterstock, getty, storyblocks, artgrid | owner, license |

A source listed in `[sources."name"]` can force its category
(`category = "archive"`), and `--init` lists unclassified sources commented out
so you can pull in anything the rules missed.

Merging: uses of the same source that overlap or touch on the timeline
(stereo pairs, an edit inside a continuous music cue) become one cue.

Timeline handling:

- **EDL**: `* FROM CLIP NAME` / `* SOURCE FILE` comments, dissolves (the
  incoming `TO CLIP NAME` is used), black/`BL` events ignored, V/A/A2/AA/B
  channels.
- **FCPXML** (1.8–1.11 + `.fcpxmld` bundles): primary storyline, connected
  clips and secondary storylines (which may extend past their parent), gaps,
  compound clips (`ref-clip`, trimmed to the visible part), disabled clips are
  skipped.

## Limits

- Tested against hand-written fixtures in the Resolve/FCP export formats, not yet
  against a real project export — run it on your next export and compare.
- Multicam (`mc-clip`) angles aren't resolved to the active angle.
- Retimed clips report timeline duration (correct for cue sheets), not source
  duration.
- Usage codes (BI/VI…) are yours to fill; the tool doesn't guess them.

## Tests

```bash
python3 -m unittest -v
```
