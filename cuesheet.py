#!/usr/bin/env python3
"""cuesheet — turn an edit timeline (CMX3600 EDL, FCP7 XML or FCPXML) into a music cue
sheet and a third-party footage usage report, flagging missing rights data.

Deterministic, stdlib only (Python 3.11+ for tomllib).
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import io
import json
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from pathlib import Path

__version__ = "0.3.0"

# --------------------------------------------------------------------------- #
# Timecode
# --------------------------------------------------------------------------- #

TC_RE = re.compile(r"^(\d{1,2})[:;.](\d{2})[:;.](\d{2})([:;.,])(\d{2,3})$")


def nominal(fps: float) -> int:
    return int(round(fps))


def is_df_rate(fps: float) -> bool:
    return abs(fps - 29.97) < 0.01 or abs(fps - 59.94) < 0.01


def tc_to_frames(tc: str, fps: float, drop: bool = False) -> int:
    m = TC_RE.match(tc.strip())
    if not m:
        raise ValueError(f"bad timecode: {tc!r}")
    h, mi, s, sep, f = m.groups()
    h, mi, s, f = int(h), int(mi), int(s), int(f)
    base = nominal(fps)
    frames = ((h * 3600 + mi * 60 + s) * base) + f
    if drop or sep == ";":
        dropn = 2 if base == 30 else 4
        total_min = h * 60 + mi
        frames -= dropn * (total_min - total_min // 10)
    return frames


def frames_to_tc(frames: int, fps: float, drop: bool = False) -> str:
    base = nominal(fps)
    neg = frames < 0
    frames = abs(frames)
    sep = ":"
    if drop:
        sep = ";"
        dropn = 2 if base == 30 else 4
        per10 = base * 600 - dropn * 9
        per1 = base * 60 - dropn
        d, m = divmod(frames, per10)
        if m > dropn:
            frames += dropn * 9 * d + dropn * ((m - dropn) // per1)
        else:
            frames += dropn * 9 * d
    f = frames % base
    s = (frames // base) % 60
    mi = (frames // (base * 60)) % 60
    h = frames // (base * 3600)
    return f"{'-' if neg else ''}{h:02d}:{mi:02d}:{s:02d}{sep}{f:02d}"


def frames_to_dur(frames: int, fps: float) -> str:
    """Human duration mm:ss (rounded to nearest second, min 1s if >0)."""
    secs = frames / fps
    whole = int(round(secs))
    if frames > 0 and whole == 0:
        whole = 1
    return f"{whole // 60}:{whole % 60:02d}"


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class Event:
    """One clip placed on the timeline (record times in frames, 0 = timeline start)."""
    name: str
    path: str
    kind: str          # "video" | "audio"
    track: str
    rec_in: int
    rec_out: int
    src_in: int | None = None
    src_out: int | None = None


@dataclass
class Timeline:
    title: str
    fps: float
    drop: bool
    start: int                    # record TC of first frame, in frames
    events: list[Event] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# EDL (CMX3600)
# --------------------------------------------------------------------------- #

EDL_EVENT_RE = re.compile(
    r"^(\d{3,6})\s+(\S+)\s+(\S+)\s+(C|D|W\d+|K\s*B?|KO)\s*(\d{3})?\s+"
    r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$"
)


def edl_kind_track(channel: str) -> list[tuple[str, str]]:
    ch = channel.upper()
    if ch in ("NONE",):
        return []
    out: list[tuple[str, str]] = []
    if ch.startswith("V") or ch in ("B",):
        out.append(("video", "V"))
        ch = ch[1:].lstrip("/")
    if ch == "B":
        out.append(("audio", "A1"))
        out.append(("audio", "A2"))
    elif ch in ("A",):
        out.append(("audio", "A1"))
    elif ch == "AA":
        out.append(("audio", "A1"))
        out.append(("audio", "A2"))
    elif ch.startswith("A"):
        # A2, A3, A4 ... or "AA/V" handled above
        num = ch[1:]
        if num.isdigit():
            out.append(("audio", f"A{num}"))
        elif num.startswith("A"):  # "AA"
            out.append(("audio", "A1"))
            out.append(("audio", "A2"))
    return out


def parse_edl(text: str, fps: float) -> Timeline:
    title = "Untitled"
    drop = False
    raw: list[dict] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        up = s.upper()
        if up.startswith("TITLE:"):
            title = s.split(":", 1)[1].strip() or title
            continue
        if up.startswith("FCM:"):
            drop = "DROP" in up and "NON" not in up
            continue
        m = EDL_EVENT_RE.match(s)
        if m:
            num, reel, chan, _trans, _dur, si, so, ri, ro = m.groups()
            raw.append({"num": num, "reel": reel, "chan": chan, "si": si,
                        "so": so, "ri": ri, "ro": ro, "name": None, "path": None})
            continue
        if not raw:
            continue
        cur = raw[-1]
        if s.startswith("*"):
            body = s.lstrip("*").strip()
            ub = body.upper()
            if ub.startswith("FROM CLIP NAME:"):
                cur["name"] = body.split(":", 1)[1].strip()
            elif ub.startswith("SOURCE FILE:"):
                cur["path"] = body.split(":", 1)[1].strip()
            elif ub.startswith("TO CLIP NAME:"):
                # dissolve: the incoming clip line is this same event's 2nd line
                cur["to_name"] = body.split(":", 1)[1].strip()
    events: list[Event] = []
    record_start: int | None = None
    for r in raw:
        if r["reel"].upper() in ("BL", "BLK", "BLACK"):
            continue
        ri = tc_to_frames(r["ri"], fps, drop)
        ro = tc_to_frames(r["ro"], fps, drop)
        if ro <= ri:
            continue
        record_start = ri if record_start is None else min(record_start, ri)
        name = r.get("to_name") or r["name"] or r["reel"]
        for kind, track in edl_kind_track(r["chan"]):
            events.append(Event(
                name=name, path=r["path"] or "", kind=kind, track=track,
                rec_in=ri, rec_out=ro,
                src_in=tc_to_frames(r["si"], fps, drop),
                src_out=tc_to_frames(r["so"], fps, drop)))
    start = record_start or 0
    # Conventional program start is the hour boundary at or below first event.
    hour = nominal(fps) * 3600
    start = (start // hour) * hour
    for e in events:
        e.rec_in -= start
        e.rec_out -= start
    return Timeline(title=title, fps=fps, drop=drop, start=start, events=events)


# --------------------------------------------------------------------------- #
# FCPXML
# --------------------------------------------------------------------------- #


def rt(v: str | None) -> Fraction:
    """FCPXML rational time: '3600/25s', '10s', '0s'."""
    if not v:
        return Fraction(0)
    v = v.strip().rstrip("s")
    if "/" in v:
        a, b = v.split("/")
        return Fraction(int(a), int(b))
    return Fraction(v)


CLIP_TAGS = {"asset-clip", "clip", "ref-clip", "sync-clip", "mc-clip", "audio",
             "video", "title", "gap", "spine", "transition"}


def parse_fcpxml(text: str, fps_override: float | None = None) -> Timeline:
    root = ET.fromstring(text)
    res = root.find("resources")
    formats: dict[str, Fraction] = {}
    assets: dict[str, dict] = {}
    media: dict[str, ET.Element] = {}
    if res is not None:
        for f in res.findall("format"):
            if f.get("frameDuration"):
                formats[f.get("id")] = rt(f.get("frameDuration"))
        for a in res.findall("asset"):
            src = a.get("src") or ""
            rep = a.find("media-rep")
            if rep is not None and rep.get("src"):
                src = rep.get("src")
            assets[a.get("id")] = {
                "name": a.get("name") or Path(src).name or a.get("id"),
                "src": src,
                "hasVideo": a.get("hasVideo") == "1",
                "hasAudio": a.get("hasAudio") == "1",
            }
        for m in res.findall("media"):
            media[m.get("id")] = m
    seq = root.find(".//project/sequence")
    title = "Untitled"
    proj = root.find(".//project")
    if proj is not None:
        title = proj.get("name") or title
    if seq is None:
        seq = root.find(".//sequence")
    if seq is None:
        raise ValueError("this FCPXML holds no timeline (<sequence>) — probably a clip or "
                         "event export; export the timeline itself")
    fd = formats.get(seq.get("format") or "", Fraction(1, 25))
    fps = fps_override or float(1 / fd)
    if abs(fps - round(fps)) > 0.001:
        fps = round(fps, 3)
    drop = seq.get("tcFormat") == "DF"
    tc_start = rt(seq.get("tcStart"))

    events: list[Event] = []

    def to_frames(t: Fraction) -> int:
        return int(round(t * Fraction(fps).limit_denominator(1001)))

    def lane_track(lane: int, kind: str) -> str:
        if kind == "audio":
            return f"A{abs(lane)}" if lane < 0 else ("A0" if lane == 0 else f"A+{lane}")
        return f"V{lane + 1}" if lane >= 0 else f"V{lane}"

    def emit(name, path, kind, lane, abs_in, dur, src_start):
        if dur <= 0:
            return
        ri = to_frames(abs_in - tc_start)
        ro = to_frames(abs_in + dur - tc_start)
        events.append(Event(name=name, path=path, kind=kind,
                            track=lane_track(lane, kind), rec_in=ri, rec_out=ro,
                            src_in=to_frames(src_start),
                            src_out=to_frames(src_start + dur)))

    def walk(el: ET.Element, origin: Fraction, parent_start: Fraction,
             inherited_lane: int, win: tuple[Fraction, Fraction],
             outer: tuple[Fraction, Fraction], audio_only: bool = False):
        """origin/parent_start map the container's local time to absolute time:
        abs = origin + (local - parent_start). Primary (lane 0) children are
        clipped to the container's window `win`; connected clips (lane != 0)
        may hang past their parent, so they only respect `outer`."""
        for ch in el:
            win_in, win_out = outer if int(ch.get("lane", "0")) != 0 else win
            tag = ch.tag
            if tag not in CLIP_TAGS or tag == "transition":
                continue
            if ch.get("enabled") == "0":
                continue
            offset = rt(ch.get("offset"))
            start = rt(ch.get("start"))
            dur = rt(ch.get("duration"))
            lane = int(ch.get("lane", "0")) or inherited_lane
            abs_in = origin + (offset - parent_start)
            if tag == "spine":
                # A secondary storyline sits at `offset` in its parent's time and
                # its children count their offsets from the storyline's own 0
                # (verified against Resolve 21 FCPXML 1.10 exports).
                if ch.get("offset") is not None:
                    walk(ch, abs_in, Fraction(0), lane, (win_in, win_out), outer, audio_only)
                else:
                    walk(ch, origin, parent_start, lane, (win_in, win_out), outer, audio_only)
                continue
            if tag == "gap":
                walk(ch, abs_in, start, lane, (max(win_in, abs_in),
                     min(win_out, abs_in + dur)), (win_in, win_out), audio_only)
                continue
            # clip to the parent's visible window
            c_in = max(abs_in, win_in)
            c_out = min(abs_in + dur, win_out)
            if c_out <= c_in:
                continue
            trimmed_head = c_in - abs_in
            ref = ch.get("ref")
            name = ch.get("name")
            if tag in ("asset-clip", "audio", "video") and ref in assets:
                a = assets[ref]
                kinds = []
                if tag == "video" or (tag == "asset-clip" and a["hasVideo"]):
                    kinds.append("video")
                if tag == "audio" or (tag == "asset-clip" and a["hasAudio"]
                                      and ch.get("audioRole") != "none"):
                    kinds.append("audio")
                if audio_only:
                    kinds = [k for k in kinds if k == "audio"]
                if tag == "asset-clip" and ch.get("srcEnable") == "audio":
                    kinds = [k for k in kinds if k == "audio"]
                if tag == "asset-clip" and ch.get("srcEnable") == "video":
                    kinds = [k for k in kinds if k == "video"]
                for k in kinds:
                    klane = lane
                    if k == "audio" and tag == "asset-clip" and not a["hasVideo"]:
                        klane = lane if lane < 0 else -1 if lane == 0 else lane
                    emit(name or a["name"], a["src"], k, klane, c_in,
                         c_out - c_in, start + trimmed_head)
                # connected clips nested inside
                walk(ch, abs_in, start, 0, (c_in, c_out), (win_in, win_out), audio_only)
            elif tag == "ref-clip" and ch.get("ref") in media:
                inner = media[ch.get("ref")].find("sequence/spine")
                if inner is not None:
                    walk(inner, abs_in, start, lane, (c_in, c_out), (c_in, c_out),
                         ch.get("srcEnable") == "audio")
                walk(ch, abs_in, start, 0, (c_in, c_out), (win_in, win_out), audio_only)
            elif tag in ("clip", "sync-clip", "mc-clip", "title"):
                walk(ch, abs_in, start, lane, (c_in, c_out), (win_in, win_out), audio_only)
    spine = seq.find("spine")
    if spine is not None:
        inf = (Fraction(-10**9), Fraction(10**9))
        walk(spine, Fraction(0), Fraction(0), 0, inf, inf)
    return Timeline(title=title, fps=fps, drop=drop, start=to_frames(tc_start),
                    events=events)


# --------------------------------------------------------------------------- #
# FCP7 XML (xmeml) — what Resolve/Premiere write for "FCP 7 XML"
# --------------------------------------------------------------------------- #


def _xm_rate(el: ET.Element | None) -> float | None:
    if el is None or el.find("timebase") is None:
        return None
    tb = int(el.findtext("timebase"))
    ntsc = (el.findtext("ntsc") or "").upper() == "TRUE"
    return round(tb * 1000 / 1001, 3) if ntsc else float(tb)


def _xm_path(url: str) -> str:
    from urllib.parse import unquote, urlparse
    if not url:
        return ""
    u = urlparse(url)
    return unquote(u.path) if u.scheme in ("file", "") else url


def _xm_cut(tr: ET.Element) -> int:
    """Edit point of a transitionitem: its start/end for start-/end-aligned
    transitions (incl. *-black), the midpoint for centred ones."""
    s, e = int(tr.findtext("start", "-1")), int(tr.findtext("end", "-1"))
    align = (tr.findtext("alignment") or "center").lower()
    if align.startswith("start"):
        return s
    if align.startswith("end"):
        return e
    return (s + e) // 2


def parse_xmeml(root: ET.Element, fps_override: float | None = None) -> Timeline:
    seq = root.find("sequence")
    if seq is None:
        seq = root.find(".//sequence")
    if seq is None:
        raise ValueError("no <sequence> found in FCP7 XML")
    fps = fps_override or _xm_rate(seq.find("rate")) or 25.0
    tc = seq.find("timecode")
    drop = tc is not None and (tc.findtext("displayformat") or "").upper() == "DF"
    start = 0
    if tc is not None:
        if tc.findtext("frame") is not None:
            start = int(tc.findtext("frame"))
        elif tc.findtext("string"):
            start = tc_to_frames(tc.findtext("string"), fps, drop)

    # <file> is fully described on first use, then referenced by id only.
    files: dict[str, dict] = {}
    for f in seq.iter("file"):
        fid = f.get("id")
        if fid and (f.find("name") is not None or f.find("pathurl") is not None):
            files[fid] = {"name": f.findtext("name") or "",
                          "path": _xm_path(f.findtext("pathurl") or "")}

    events: list[Event] = []
    for kind in ("video", "audio"):
        tracks = seq.findall(f"media/{kind}/track")
        for n, track in enumerate(tracks, 1):
            if (track.findtext("enabled") or "TRUE").upper() == "FALSE":
                continue
            items = list(track)
            for i, it in enumerate(items):
                if it.tag != "clipitem":
                    continue  # generatoritem = titles/solids, transitionitem handled below
                if (it.findtext("enabled") or "TRUE").upper() == "FALSE":
                    continue
                s, e = int(it.findtext("start", "-1")), int(it.findtext("end", "-1"))
                src_len = int(it.findtext("out", "0")) - int(it.findtext("in", "0"))
                # -1 means "this edge sits inside a transition": use the transition's
                # edit point, which is what Resolve itself reports for the clip.
                if s < 0 and i > 0 and items[i - 1].tag == "transitionitem":
                    s = _xm_cut(items[i - 1])
                if e < 0 and i + 1 < len(items) and items[i + 1].tag == "transitionitem":
                    e = _xm_cut(items[i + 1])
                if s < 0 <= e and src_len > 0:
                    s = e - src_len
                if e < 0 <= s and src_len > 0:
                    e = s + src_len
                if s < 0 or e <= s:
                    continue
                f = it.find("file")
                meta = files.get(f.get("id"), {}) if f is not None else {}
                name = it.findtext("name") or meta.get("name") or ""
                src_in = int(it.findtext("in", "0"))
                events.append(Event(
                    name=name, path=meta.get("path", ""), kind=kind,
                    track=f"{'V' if kind == 'video' else 'A'}{n}",
                    rec_in=s, rec_out=e, src_in=src_in, src_out=src_in + (e - s)))
    return Timeline(title=seq.findtext("name") or "Untitled", fps=fps, drop=drop,
                    start=start, events=events)


def load_timeline(path: Path, fps: float | None) -> Timeline:
    if path.is_dir() and (path / "Info.fcpxml").exists():  # .fcpxmld bundle
        path = path / "Info.fcpxml"
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    head = text.lstrip()[:200].lower()
    if path.suffix.lower() in (".fcpxml", ".xml") or head.startswith("<?xml") or head.startswith("<"):
        root = ET.fromstring(text)
        if root.tag == "xmeml":
            tl = parse_xmeml(root, fps)
        elif root.tag == "fcpxml":
            tl = parse_fcpxml(text, fps)
        else:
            raise ValueError(f"{path.name}: unsupported XML <{root.tag}> "
                             "(expected FCPXML or FCP7 XML/xmeml)")
    else:
        tl = parse_edl(text, fps or 25.0)
    if not tl.events and text.strip():
        # An empty report looks like "no rights issues" — never let that happen silently.
        raise ValueError(f"{path.name}: no clip events could be read from this timeline")
    return tl


# --------------------------------------------------------------------------- #
# Rules / rights
# --------------------------------------------------------------------------- #

# Patterns are matched case-insensitively against clip name, file path and file name.
# Production sound (camera rolls, recorder takes, sync, VO, dialogue) is never a cue.
PRODUCTION_AUDIO = [
    "*.rdc/*", "a[0-9][0-9][0-9]_c[0-9][0-9][0-9]_*", "c[0-9][0-9][0-9][0-9].*",
    "*-t[0-9][0-9][0-9]*.wav", "zoom[0-9][0-9][0-9][0-9]*", "*_tr[0-9]*.wav",
    "[0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9]*.wav", "*sync*", "*/vo/*", "vo_*", "*_vo_*",
    "*dialog*", "*dialogo*", "*diálogo*", "*/dx/*",
]
AUDIO_EXT = ["*.wav", "*.aif", "*.aiff", "*.mp3", "*.flac", "*.m4a", "*.ogg", "*.bwf"]

DEFAULT_RULES = {
    "category": [
        {"name": "sfx", "kind": "audio",
         "match": ["*sfx*", "*/fx/*", "*foley*", "*sound effect*", "*efecto de sonido*",
                   "*efectos de sonido*", "*/efectos/*"],
         "exclude": PRODUCTION_AUDIO,
         "required": ["owner", "license"]},
        {"name": "music", "kind": "audio",
         "match": ["mus_*", "*music*", "*musica*", "*música*", "*score*",
                   "*soundtrack*", "*banda sonora*",
                   "*[ _/(-]bso[ _/)-]*", "bso[ _-]*", "*[ _/(-]ost[ _/)-]*", "ost[ _-]*"],
         "exclude": PRODUCTION_AUDIO,
         "required": ["title", "composer", "publisher", "pro", "usage"]},
        {"name": "archive",
         "match": ["arch_*", "archivo_*", "*/archive/*", "*/archivo/*"],
         "required": ["owner", "license"]},
        {"name": "stock",
         "match": ["stock_*", "*/stock/*", "*shutterstock*", "*gettyimages*", "*pond5*",
                   "*storyblocks*", "*artgrid*", "*artlist*", "*envato*"],
         "required": ["owner", "license"]},
        # Any other audio file: don't guess. It stays "to review" (and fails
        # --strict) until the rights file says what it is.
        {"name": "audio-review", "kind": "audio",
         "match": AUDIO_EXT,
         "exclude": PRODUCTION_AUDIO,
         "required": ["category"]},
    ],
    "merge_gap_seconds": 0.0,
}


def load_rules(path: Path | None) -> dict:
    rules = json.loads(json.dumps(DEFAULT_RULES))
    rules["sources"] = {}
    if path is None:
        return rules
    with open(path, "rb") as fh:
        user = tomllib.load(fh)
    if "category" in user:
        rules["category"] = user["category"]
    for k in ("merge_gap_seconds", "fps", "title"):
        if k in user:
            rules[k] = user[k]
    rules["sources"] = user.get("sources", {})
    return rules


def _match(patterns, e: Event) -> bool:
    cands = [c.casefold() for c in (e.name, e.path, Path(e.path).name if e.path else "") if c]
    return any(fnmatch.fnmatchcase(c, p.casefold()) for p in patterns for c in cands)


def classify(e: Event, rules: dict) -> str | None:
    # explicit per-source category wins
    src = source_meta(e, rules)
    if src and "category" in src:
        return src["category"] or None  # category = "" means: not a cue, ignore
    for cat in rules["category"]:
        if cat.get("kind") and cat["kind"] != e.kind:
            continue
        if _match(cat.get("match", []), e) and not _match(cat.get("exclude", []), e):
            return cat["name"]
    return None


def source_meta(e: Event, rules: dict) -> dict | None:
    srcs = rules.get("sources", {})
    for key in (e.name, Path(e.path).name if e.path else None, e.path or None):
        if key and key in srcs:
            return srcs[key]
    for key, meta in srcs.items():
        if any(c and fnmatch.fnmatch(c, key) for c in (e.name, e.path)):
            return meta
    return None


def source_key(e: Event) -> str:
    return e.name or Path(e.path).name


# --------------------------------------------------------------------------- #
# Cues
# --------------------------------------------------------------------------- #


@dataclass
class Cue:
    number: int
    category: str
    source: str
    path: str
    kind: str
    tracks: list[str]
    rec_in: int
    rec_out: int
    meta: dict
    missing: list[str]

    @property
    def frames(self) -> int:
        return self.rec_out - self.rec_in


def build_cues(tl: Timeline, rules: dict, merge_gap_frames: int = 0,
               include: set[str] | None = None) -> list[Cue]:
    groups: dict[tuple[str, str], list[Event]] = {}
    for e in tl.events:
        cat = classify(e, rules)
        if not cat or (include and cat not in include):
            continue
        groups.setdefault((cat, source_key(e)), []).append(e)
    req = {c["name"]: c.get("required", []) for c in rules["category"]}
    cues: list[Cue] = []
    for (cat, src), evs in groups.items():
        evs.sort(key=lambda e: (e.rec_in, e.rec_out))
        runs: list[list[Event]] = []
        for e in evs:
            if runs and e.rec_in <= max(x.rec_out for x in runs[-1]) + merge_gap_frames:
                runs[-1].append(e)
            else:
                runs.append([e])
        meta = source_meta(evs[0], rules) or {}
        missing = [f for f in req.get(cat, []) if not str(meta.get(f, "")).strip()]
        if cat == "audio-review":
            missing = ["category"]  # still undecided, even if the rights file lists it
        for run in runs:
            cues.append(Cue(
                number=0, category=cat, source=src, path=run[0].path,
                kind=run[0].kind,
                tracks=sorted({x.track for x in run}),
                rec_in=min(x.rec_in for x in run),
                rec_out=max(x.rec_out for x in run),
                meta={k: v for k, v in meta.items() if k != "category"},
                missing=missing))
    cues.sort(key=lambda c: (c.category != "music", c.category, c.rec_in, c.source))
    counters: dict[str, int] = {}
    for c in cues:
        counters[c.category] = counters.get(c.category, 0) + 1
        c.number = counters[c.category]
    return cues


def cue_label(c: Cue) -> str:
    prefix = {"music": "M", "sfx": "FX", "archive": "AR", "stock": "ST",
              "audio-review": "RV"}.get(c.category,
                                                              c.category[:2].upper())
    return f"{prefix}{c.number:02d}"


def summarize(cues: list[Cue], tl: Timeline) -> list[dict]:
    per: dict[tuple[str, str], dict] = {}
    for c in cues:
        d = per.setdefault((c.category, c.source), {
            "category": c.category, "source": c.source, "uses": 0, "frames": 0,
            "missing": c.missing, "meta": c.meta})
        d["uses"] += 1
        d["frames"] += c.frames
    out = sorted(per.values(), key=lambda d: (d["category"], -d["frames"]))
    for d in out:
        d["duration"] = frames_to_dur(d["frames"], tl.fps)
        d["seconds"] = round(d["frames"] / tl.fps, 2)
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

MUSIC_COLS = ["title", "composer", "publisher", "pro", "usage"]
FOOTAGE_COLS = ["owner", "license"]


def _tc(frames: int, tl: Timeline) -> str:
    return frames_to_tc(frames + tl.start, tl.fps, tl.drop)


def _cell(v) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ")


def render_md(tl: Timeline, cues: list[Cue]) -> str:
    total = max((e.rec_out for e in tl.events), default=0)
    lines = [f"# Cue sheet — {tl.title}", "",
             f"- Timeline: **{tl.title}** · {tl.fps:g} fps"
             f"{' DF' if tl.drop else ''} · duración {frames_to_dur(total, tl.fps)}"
             f" · TC inicio {frames_to_tc(tl.start, tl.fps, tl.drop)}",
             f"- Generado por cuesheet {__version__}", ""]
    music = [c for c in cues if c.category == "music"]
    review = [c for c in cues if c.category == "audio-review"]
    other = [c for c in cues if c.category not in ("music", "audio-review")]
    if music:
        lines += ["## Música", "",
                  "| Cue | Título | Compositor | Editorial | Sociedad | Uso | TC in | TC out | Duración | Fuente |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for c in music:
            m = c.meta
            title = m.get("title") or f"⚠ {c.source}"
            lines.append("| " + " | ".join(_cell(x) for x in [
                cue_label(c), title,
                m.get("composer") or "⚠", m.get("publisher") or "⚠",
                m.get("pro") or "⚠", m.get("usage") or "⚠",
                _tc(c.rec_in, tl), _tc(c.rec_out, tl),
                frames_to_dur(c.frames, tl.fps), c.source]) + " |")
        mtotal = sum(c.frames for c in music)
        lines += ["", f"Total música: **{frames_to_dur(mtotal, tl.fps)}** en "
                  f"{len(music)} cues.", ""]
    if other:
        lines += ["## Material de terceros (efectos / archivo / stock)", "",
                  "| Cue | Categoría | Fuente | Titular | Licencia | TC in | TC out | Duración |",
                  "|---|---|---|---|---|---|---|---|"]
        for c in other:
            m = c.meta
            lines.append("| " + " | ".join(_cell(x) for x in [
                cue_label(c), c.category, c.source, m.get("owner") or "⚠",
                m.get("license") or "⚠", _tc(c.rec_in, tl), _tc(c.rec_out, tl),
                frames_to_dur(c.frames, tl.fps)]) + " |")
        lines.append("")
    if review:
        srcs = sorted({c.source for c in review})
        lines += [f"## ⚠ Audio sin clasificar ({len(srcs)} fuentes)", "",
                  "Ni música, ni efectos, ni sonido directo según las reglas. Decide en el "
                  'fichero de derechos: `category = "music"`, `"sfx"` o `""` (no es un cue).', ""]
        for src in srcs:
            fr = sum(c.frames for c in review if c.source == src)
            lines.append(f"- `{src}` — {frames_to_dur(fr, tl.fps)}")
        lines.append("")
    summ = summarize(cues, tl)
    if summ:
        lines += ["## Uso total por fuente", "",
                  "| Categoría | Fuente | Usos | Tiempo en pantalla | Faltan datos |",
                  "|---|---|---|---|---|"]
        for d in summ:
            lines.append("| " + " | ".join(_cell(x) for x in [
                d["category"], d["source"], d["uses"], d["duration"],
                ", ".join(d["missing"]) or "—"]) + " |")
        lines.append("")
    gaps = [d for d in summ if d["missing"]]
    if gaps:
        lines += [f"## ⚠ Derechos incompletos ({len(gaps)} fuentes)", "",
                  "Completa estos campos en el fichero de reglas (`cuesheet --init`) "
                  "antes de entregar:", ""]
        for d in gaps:
            lines.append(f"- `{d['source']}` ({d['category']}): {', '.join(d['missing'])}")
        lines.append("")
    if not cues:
        lines += ["_No se detectó música ni material de terceros con las reglas actuales._", ""]
    return "\n".join(lines)


def render_csv(tl: Timeline, cues: list[Cue]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["cue", "category", "source", "title", "composer", "publisher",
                "pro", "usage", "owner", "license", "tc_in", "tc_out",
                "duration", "seconds", "tracks", "path", "missing"])
    for c in cues:
        m = c.meta
        w.writerow([cue_label(c), c.category, c.source, m.get("title", ""),
                    m.get("composer", ""), m.get("publisher", ""), m.get("pro", ""),
                    m.get("usage", ""), m.get("owner", ""), m.get("license", ""),
                    _tc(c.rec_in, tl), _tc(c.rec_out, tl),
                    frames_to_dur(c.frames, tl.fps), round(c.frames / tl.fps, 2),
                    " ".join(c.tracks), c.path, ";".join(c.missing)])
    return buf.getvalue()


def render_json(tl: Timeline, cues: list[Cue]) -> str:
    return json.dumps({
        "timeline": {"title": tl.title, "fps": tl.fps, "drop_frame": tl.drop,
                     "start_tc": frames_to_tc(tl.start, tl.fps, tl.drop),
                     "events": len(tl.events)},
        "cues": [{**{k: v for k, v in asdict(c).items()},
                  "label": cue_label(c), "tc_in": _tc(c.rec_in, tl),
                  "tc_out": _tc(c.rec_out, tl),
                  "seconds": round(c.frames / tl.fps, 2)} for c in cues],
        "summary": summarize(cues, tl),
        "incomplete_sources": sorted({c.source for c in cues if c.missing}),
    }, indent=2, ensure_ascii=False)


def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render_init(tl: Timeline, rules: dict) -> str:
    seen: dict[str, tuple[str, Event]] = {}
    unclassified: dict[str, Event] = {}
    for e in tl.events:
        cat = classify(e, rules)
        k = source_key(e)
        if cat:
            seen.setdefault(k, (cat, e))
        else:
            unclassified.setdefault(k, e)
    req = {c["name"]: c.get("required", []) for c in rules["category"]}
    out = [f"# cuesheet rights file — generado de «{tl.title}»",
           "# Rellena los campos vacíos. Una fuente puede forzar su categoría con",
           '# category = "music" | "archive" | "stock" | "" (vacío = ignorar).',
           "# usage (música): BI background instrumental, BV background vocal,",
           "#   VI visual instrumental, VV visual vocal, MT main title, ET end title.",
           "", "merge_gap_seconds = 0.0", ""]
    for k, (cat, e) in sorted(seen.items(), key=lambda kv: (kv[1][0], kv[0])):
        existing = rules.get("sources", {}).get(k, {})
        out.append(f"[sources.{toml_str(k)}]")
        out.append(f"category = {toml_str(cat)}")
        if cat == "audio-review":
            out[-1] = 'category = "audio-review"  # ⚠ cámbialo: "music", "sfx" o "" (no es un cue)'
        for f in req.get(cat, []):
            if f == "category":
                continue
            out.append(f"{f} = {toml_str(str(existing.get(f, '')))}")
        if e.path:
            out.append(f"# path: {e.path}")
        out.append("")
    if unclassified:
        out.append("# --- Fuentes no clasificadas (material propio?). Descomenta para incluir:")
        for k, e in sorted(unclassified.items()):
            out.append(f"# [sources.{toml_str(k)}]  # {e.kind} {e.track}")
            out.append('# category = "archive"')
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cuesheet",
        description="Timeline (EDL/FCPXML) → music cue sheet + third-party footage report.")
    p.add_argument("timeline", nargs="+", type=Path,
                   help="EDL (.edl) or FCPXML (.fcpxml/.xml). Several EDLs (e.g. one per "
                        "audio track exported from Resolve) are merged into one timeline.")
    p.add_argument("-r", "--rules", type=Path,
                   help="TOML rights/rules file (see --init). Default: built-in rules.")
    p.add_argument("-f", "--format", choices=["md", "csv", "json"], default="md")
    p.add_argument("-o", "--output", type=Path, help="write to file instead of stdout")
    p.add_argument("--fps", type=float, help="frame rate (EDL has none; default 25)")
    p.add_argument("--merge-gap", type=float,
                   help="merge uses of the same source separated by ≤ N seconds")
    p.add_argument("--only", action="append",
                   help="only these categories (repeatable): music, archive, stock…")
    p.add_argument("--init", action="store_true",
                   help="print a rights TOML skeleton for every detected source")
    p.add_argument("--strict", action="store_true",
                   help="exit 2 if any cue lacks required rights fields")
    p.add_argument("--version", action="version", version=f"cuesheet {__version__}")
    a = p.parse_args(argv)

    rules = load_rules(a.rules)
    fps = a.fps or rules.get("fps")
    try:
        timelines = [load_timeline(t, fps) for t in a.timeline]
    except (ValueError, ET.ParseError, OSError) as exc:
        print(f"cuesheet: {exc}", file=sys.stderr)
        return 1
    tl = timelines[0]
    for other in timelines[1:]:
        # align on absolute record TC
        shift = other.start - tl.start
        for e in other.events:
            e.rec_in += shift
            e.rec_out += shift
        tl.events.extend(other.events)
    if rules.get("title"):
        tl.title = rules["title"]

    if a.init:
        out = render_init(tl, rules)
    else:
        gap = a.merge_gap if a.merge_gap is not None else rules.get("merge_gap_seconds", 0.0)
        cues = build_cues(tl, rules, int(round(gap * tl.fps)),
                          set(a.only) if a.only else None)
        out = {"md": render_md, "csv": render_csv, "json": render_json}[a.format](tl, cues)
    if a.output:
        a.output.write_text(out, encoding="utf-8")
    else:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if not a.init and a.strict:
        bad = sorted({c.source for c in cues if c.missing})
        if bad:
            print(f"cuesheet: {len(bad)} source(s) missing rights data: "
                  + ", ".join(bad), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
