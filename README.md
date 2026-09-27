# cuesheet

**The music cue sheet and archive-usage report your festival checklist asks for,
straight from the edit.** `cuesheet` reads a timeline export (CMX3600 EDL,
FCP 7 XML from Resolve/Premiere, or FCPXML from Final Cut / Resolve) and produces:

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
./cuesheet.py FDV_v12.xml -r rights.toml             # FCP 7 XML: all tracks
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

Built-in rules, matched case-insensitively on clip name, file name and full
path (override them with `[[category]]` tables in the TOML):

| Category | Matches | Required fields |
|---|---|---|
| `sfx` (audio) | `*sfx*`, `*foley*`, `*/fx/*`, "sound effect", "efecto(s) de sonido" | owner, license |
| `music` (audio) | `MUS_*`, music / música / score / soundtrack / banda sonora, BSO, OST — **only with that evidence** in the name or folder | title, composer, publisher, pro, usage |
| `archive` | `ARCH_*`, `ARCHIVO_*`, `*/Archive/*`, `*/Archivo/*` | owner, license |
| `stock` | `STOCK_*`, `*/Stock/*`, pond5, shutterstock, getty, storyblocks, artgrid, artlist, envato | owner, license |
| `audio-review` (audio) | any other audio file (`.wav/.aif/.mp3/.flac/.m4a/.bwf`) | a decision — always flagged, fails `--strict` |

Production sound is never a cue: camera rolls (`A001_C002_*`, `*.RDC/*`,
`C0001.MP4`), recorder takes (`ZOOM0040_Tr1.WAV`, `200310_001.WAV`, `…-T017.WAV`),
sync, VO, dialogue.

An unknown audio file is never silently called music: it lands in
**audio-review** until the rights file decides — `category = "music"`, `"sfx"`,
or `""` (not a cue, ignore it). A source listed in `[sources."name"]` can force
any category, and `--init` lists unclassified sources commented out so you can
pull in anything the rules missed.

Merging: uses of the same source that overlap or touch on the timeline
(stereo pairs, an edit inside a continuous music cue) become one cue.

Timeline handling:

- **EDL**: `* FROM CLIP NAME` / `* SOURCE FILE` comments, dissolves (the
  incoming `TO CLIP NAME` is used), black/`BL` events ignored, V/A/A2/AA/B
  channels.
- **FCP 7 XML** (`xmeml`, Resolve *File → Export → Timeline → FCP 7 XML*):
  every video and audio track, rate/NTSC and start TC from the sequence,
  `<file>` references by id, clip edges inside transitions (`-1`), disabled
  clips and generators (titles, solids) skipped. **Prefer this over EDL from
  Resolve**: a Resolve EDL holds a single video track, the XML holds all of them.
- **FCPXML** (1.8–1.11 + `.fcpxmld` bundles): primary storyline, connected
  clips and secondary storylines (which may extend past their parent), gaps,
  compound clips (`ref-clip`, trimmed to the visible part), disabled clips are
  skipped.

An XML it doesn't recognise, or a timeline it reads zero clips from, is an
**error (exit 1)**, never an empty report — an empty cue sheet would read as
"no rights issues".

## Validated on real exports

**Feature timeline (music path).** A 31.7-min cut of the Fillos do Vento
feature (Resolve Studio 21.1, 23.976, 7 video + 17 audio tracks, 1 005 audio
clips) exported by Resolve itself to FCP 7 XML, FCPXML 1.10 (`.fcpxmld`) and
EDL, and compared with the clip list read back through Resolve's scripting API:

| | FCP 7 XML | FCPXML 1.10 | EDL |
|---|---|---|---|
| Cues identical to Resolve (source + TC in/out) | **31/31** | **31/31** | — (video only) |
| Clips read vs Resolve's 802 media clips | 802 (757 frame-exact, rest ±a few frames at transitions) | 771 | 139 (V1) |

With the default rules: 13 music sources (all the score and licensed tracks),
8 SFX sources (all library effects), 3 flagged for review (conformed production
audio), and 0 music missed among the 94 ignored production-sound sources.

**Installation timelines.** 6 FCP 7 XML + 2 EDL (23.976) from the same project
all parse; EDL and XML of the same timeline agree on 85/88 V1 events.

**Use FCP 7 XML from Resolve**: it holds every track and matched Resolve best;
a Resolve EDL holds one video track and no audio.

## Limits
- Multicam (`mc-clip`) angles aren't resolved to the active angle.
- Retimed clips report timeline duration (correct for cue sheets), not source
  duration.
- Usage codes (BI/VI…) are yours to fill; the tool doesn't guess them.

## Tests

```bash
python3 -m unittest -v
```
