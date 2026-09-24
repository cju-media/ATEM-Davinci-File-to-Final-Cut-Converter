#!/usr/bin/env python3
"""Convert a Blackmagic ATEM ISO recording project (.drp) into FCPXML 1.10.

The output contains a multicam clip built from the ISO recordings (plus the
program recording and selected WAVs as extra angles) and a project whose
timeline reproduces the live switching as mc-clips, with ATEM mixes turned
into Cross Dissolves.  Imports into Final Cut Pro and DaVinci Resolve.

Standard library only.  If ffprobe is on the PATH it is used to read real
durations, audio channel counts and start timecodes.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from fractions import Fraction

SETTINGS_PATH = os.path.expanduser("~/Library/Application Support/atem2fcp/settings.json")
# options the front end can save for the Finder Quick Action (--use-settings)
SETTING_DEFAULTS = {"no_transitions": False, "skip_wavs": False, "camera_wavs": False, "audio_angle": ""}

CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"

# rate token -> (frameDuration numerator, denominator, timecode fps, name code)
RATES = {
    "23.98": (1001, 24000, 24, "2398"),
    "23.976": (1001, 24000, 24, "2398"),
    "24": (100, 2400, 24, "24"),
    "25": (100, 2500, 25, "25"),
    "29.97": (1001, 30000, 30, "2997"),
    "30": (100, 3000, 30, "30"),
    "50": (100, 5000, 50, "50"),
    "59.94": (1001, 60000, 60, "5994"),
    "60": (100, 6000, 60, "60"),
}
# interlaced modes name the field rate; the frame rate is half of it
FIELD_TO_FRAME = {"50": "25", "59.94": "29.97", "60": "30"}
SIZES = {"480": (720, 486), "576": (720, 576), "720": (1280, 720),
         "1080": (1920, 1080), "2160": (3840, 2160), "4320": (7680, 4320)}


class VideoMode:
    def __init__(self, mode):
        m = re.fullmatch(r"(\d+)([pi])([\d.]+)", mode.strip())
        if not m:
            raise ValueError(f"unrecognised videoMode {mode!r}")
        lines, scan, rate = m.groups()
        if lines not in SIZES:
            raise ValueError(f"unsupported resolution in videoMode {mode!r}")
        self.mode = mode
        self.interlaced = scan == "i"
        frame_rate = FIELD_TO_FRAME.get(rate) if self.interlaced else rate
        if frame_rate not in RATES:
            raise ValueError(f"unsupported frame rate in videoMode {mode!r}")
        num, den, self.tc_fps, _ = RATES[frame_rate]
        self.frame_duration = Fraction(num, den)
        self.fd_num, self.fd_den = num, den
        self.width, self.height = SIZES[lines]
        code = RATES[rate][3]  # interlaced names use the field rate (1080i5994)
        res = lines if lines in ("720", "1080", "480", "576") else f"{self.width}x{self.height}"
        self.format_name = f"FFVideoFormat{res}{scan}{code}"
        self.fps = 1 / self.frame_duration

    def t(self, frames):
        """Rational FCPXML time for a frame count, on this mode's timebase."""
        n = frames * self.fd_num
        if n % self.fd_den == 0:
            return f"{n // self.fd_den}s"
        return f"{n}/{self.fd_den}s"

    def tc_to_frames(self, tc):
        m = re.fullmatch(r"(\d+):(\d+):(\d+)[:;.](\d+)", tc.strip())
        if not m:
            raise ValueError(f"bad timecode {tc!r}")
        h, mi, s, f = map(int, m.groups())
        if f >= self.tc_fps:
            raise ValueError(f"timecode {tc!r} has frame >= {self.tc_fps}")
        return ((h * 60 + mi) * 60 + s) * self.tc_fps + f

    def frames_to_tc(self, frames):
        f = frames % self.tc_fps
        s = frames // self.tc_fps
        return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"

    def seconds_to_frames(self, seconds):
        return int(Fraction(seconds) / self.frame_duration)


def duration_str(vm, frames):
    s = frames // vm.tc_fps
    return f"{s // 3600}h {s // 60 % 60:02d}m {s % 60:02d}s {frames % vm.tc_fps:02d}f"


def load_settings():
    settings = dict(SETTING_DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        settings.update({k: v for k, v in saved.items() if k in SETTING_DEFAULTS})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump({k: settings.get(k, v) for k, v in SETTING_DEFAULTS.items()}, fh, indent=2)


# ---------------------------------------------------------------- .drp parsing

def read_drp(path):
    not_atem = f"{os.path.basename(path)} is not an ATEM ISO recording project"
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        header = json.loads(lines[0]) if lines else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"{not_atem} (a DaVinci Resolve .drp, perhaps?)") from None
    if not isinstance(header, dict) or "sources" not in header or "mixEffectBlocks" not in header:
        raise ValueError(not_atem)
    changes = []
    for n, ln in enumerate(lines[1:], start=2):
        try:
            changes.append(json.loads(ln))
        except json.JSONDecodeError as e:
            raise ValueError(f"line {n}: {e}") from None
    return header, changes


def me0(obj):
    for me in obj.get("mixEffectBlocks") or []:
        if me.get("_index_", 0) == 0:
            return me
    return None


def parse_switching(vm, header, changes, warnings):
    """Return (start_frame, end_frame, initial_source, events).

    Each event is a dict: kind ('cut'|'mix'), start, end (frames; equal for a
    cut) and source (the source index the program lands on).
    """
    start = vm.tc_to_frames(header["masterTimecode"])
    me = me0(header) or {}
    if "source" not in me:
        raise ValueError("header has no mixEffectBlocks[0].source")
    initial = current = me["source"]
    events, end, last_tc = [], None, start
    mix_start = None
    for ch in changes:
        tc = vm.tc_to_frames(ch["masterTimecode"])
        if tc < last_tc:
            warnings.append(f"event at {ch['masterTimecode']} is earlier than the previous one; ignored")
            continue
        last_tc = tc
        me = me0(ch)
        if me is None:
            continue
        if me.get("onAir") is False:
            end = tc
            break
        active = me.get("transitionActive")
        new = me.get("source")
        if new is not None and new != current:
            if mix_start is not None and active is not True:
                events.append({"kind": "mix", "start": mix_start, "end": tc, "source": new})
                mix_start = None
            else:
                events.append({"kind": "cut", "start": tc, "end": tc, "source": new})
            current = new
        if active is True and mix_start is None:
            mix_start = tc
        elif active is False:
            mix_start = None  # finished mix, or a no-op mix with no source change
    if end is None:
        end = last_tc + vm.tc_fps
        warnings.append("no onAir:false event; recording end taken as 1 second after the last event "
                        f"({vm.frames_to_tc(end)})")
    return start, end, initial, events


# --------------------------------------------------------------- media lookup

def probe(path):
    """Return ffprobe's JSON for path, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


class Media:
    """One file: where it lives and what's in it."""

    def __init__(self, vm, name, path, found, start, default_duration, kind):
        self.name = name              # display name (file stem)
        self.path = path              # absolute path (possibly not present)
        self.found = found
        self.start = start            # start frame (timecode)
        self.duration = default_duration
        self.has_video = kind == "video"
        self.has_audio = True
        self.audio_sources = 1
        self.audio_channels = 2
        self.audio_rate = 48000
        self.probed = False
        self.vm = vm

    def apply_probe(self, info, warnings):
        vm = self.vm
        streams = info.get("streams", [])
        fmt = info.get("format", {})
        video = [s for s in streams if s.get("codec_type") == "video"
                 and not s.get("disposition", {}).get("attached_pic")]
        audio = [s for s in streams if s.get("codec_type") == "audio"]
        self.probed = True
        self.has_video = bool(video)
        self.has_audio = bool(audio)
        if audio:
            self.audio_sources = len(audio)
            self.audio_channels = sum(int(s.get("channels") or 0) for s in audio) or 2
            self.audio_rate = int(audio[0].get("sample_rate") or 48000)
        frames = None
        if video:
            v = video[0]
            if v.get("nb_frames", "").isdigit():
                frames = int(v["nb_frames"])
            rate = v.get("r_frame_rate") or v.get("avg_frame_rate") or "0/1"
            try:
                r = Fraction(rate)
            except (ValueError, ZeroDivisionError):
                r = Fraction(0)
            if r and r != vm.fps and r != vm.fps * 2:
                warnings.append(f"{self.name}: frame rate {float(r):g} does not match videoMode {vm.mode}")
        if frames is None:
            dur = fmt.get("duration") or (video or audio or [{}])[0].get("duration")
            if dur:
                frames = vm.seconds_to_frames(dur)
        if frames:
            self.duration = frames
        # start timecode
        tc = None
        for s in streams:
            tc = tc or (s.get("tags") or {}).get("timecode")
        tc = tc or (fmt.get("tags") or {}).get("timecode")
        start = None
        if tc:
            if ";" in tc:
                warnings.append(f"{self.name}: file timecode {tc} is drop-frame; treated as non-drop")
            try:
                start = vm.tc_to_frames(tc.replace(";", ":"))
            except ValueError:
                warnings.append(f"{self.name}: could not parse file timecode {tc!r}")
        else:
            ref = (fmt.get("tags") or {}).get("time_reference")
            if ref and ref.isdigit() and self.audio_rate:
                start = round(Fraction(int(ref), self.audio_rate) / vm.frame_duration)
        if start is not None and start != self.start:
            warnings.append(f"{self.name}: file timecode {vm.frames_to_tc(start)} differs from .drp "
                            f"{vm.frames_to_tc(self.start)}; using the file's")
            self.start = start


class MediaFinder:
    def __init__(self, drp_dir, media_root):
        self.drp_dir = drp_dir
        self.media_root = media_root
        self.missing = 0

    def roots(self, volume, project_path):
        roots = []
        if self.media_root:
            roots.append(self.media_root)
        roots.append(self.drp_dir)
        if volume and project_path:
            roots.append(os.path.join("/Volumes", volume, project_path))
        return roots

    def recorded_path(self, volume, project_path, rel):
        if volume and project_path:
            return os.path.join("/Volumes", volume, project_path, rel)
        return os.path.join(self.drp_dir, rel)

    def find(self, volume, project_path, rel, required=True):
        for root in self.roots(volume, project_path):
            p = os.path.join(root, rel)
            if os.path.isfile(p):
                return os.path.abspath(p), True
        if required:
            self.missing += 1
        return self.recorded_path(volume, project_path, rel), False


def file_url(path):
    return "file://" + urllib.parse.quote(os.path.abspath(path), safe="/")


def angle_id(name, n):
    digest = hashlib.md5(f"{n}:{name}".encode()).digest()
    return base64.b64encode(digest).decode().rstrip("=").replace("+", "A").replace("/", "B")


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def audio_label(file_rel, project_path):
    """'Audio Source Files/Show MADI 3 01.wav' -> 'MADI 3'."""
    stem = os.path.splitext(os.path.basename(file_rel))[0]
    if project_path and stem.startswith(project_path + " "):
        stem = stem[len(project_path) + 1:]
    return re.sub(r" \d{2}$", "", stem).strip() or stem


# ------------------------------------------------------------------- building

class Angle:
    def __init__(self, name, media, source_index=None, role="video"):
        self.name = name
        self.media = media
        self.source_index = source_index
        self.role = role  # 'video' | 'program' | 'audio'
        self.id = None


def build(args):
    warnings, notes = [], []
    drp_path = os.path.abspath(args.drp)
    drp_dir = os.path.dirname(drp_path)
    header, changes = read_drp(drp_path)
    vm = VideoMode(header.get("videoMode", ""))
    rec_start, rec_end, initial, events = parse_switching(vm, header, changes, warnings)
    if rec_end <= rec_start:
        raise ValueError("recording ends before it starts")
    sources = {s["_index_"]: s for s in header.get("sources", [])}
    project_path = next((s.get("projectPath") for s in sources.values() if s.get("projectPath")), None)
    volume = next((s.get("volume") for s in sources.values() if s.get("volume")), None)
    title = args.title or project_path or os.path.splitext(os.path.basename(drp_path))[0]

    finder = MediaFinder(drp_dir, args.media_root)
    have_ffprobe = shutil.which("ffprobe") is not None
    rec_len = rec_end - rec_start

    def make_media(src_volume, src_project, rel, start_tc, kind):
        path, found = finder.find(src_volume, src_project, rel)
        try:
            start = vm.tc_to_frames(start_tc) if start_tc else rec_start
        except ValueError:
            warnings.append(f"{rel}: bad startTimecode {start_tc!r}; using recording start")
            start = rec_start
        m = Media(vm, os.path.splitext(os.path.basename(rel))[0], path, found, start,
                  max(1, rec_end - start), kind)
        if found and have_ffprobe:
            info = probe(path)
            if info:
                m.apply_probe(info, warnings)
            else:
                warnings.append(f"ffprobe could not read {path}; using .drp values")
        return m

    # camera angles
    angles, cam_by_source = [], {}
    for idx in sorted(sources):
        s = sources[idx]
        if s.get("type") == "Video" and s.get("file"):
            m = make_media(s.get("volume"), s.get("projectPath"), s["file"], s.get("startTimecode"), "video")
            a = Angle(s.get("name") or m.name, m, idx, "video")
            angles.append(a)
            cam_by_source[idx] = a
    if not angles:
        raise ValueError("no ISO camera sources (type Video with a file) in the .drp")

    # program recording
    if args.program:
        ppath = os.path.abspath(args.program)
        if not os.path.isfile(ppath):
            raise ValueError(f"--program file not found: {ppath}")
        m = make_media(None, None, ppath, header["masterTimecode"], "video")
        angles.append(Angle("Program", m, None, "program"))
    elif project_path:
        rel = f"{project_path} 01.mp4"
        if finder.find(volume, project_path, rel, required=False)[1]:
            m = make_media(volume, project_path, rel, header["masterTimecode"], "video")
            angles.append(Angle("Program", m, None, "program"))
            if finder.find(volume, project_path, f"{project_path} 02.mp4", required=False)[1]:
                warnings.append(f"program recording is split ('{project_path} 02.mp4' exists); "
                                "only ' 01' is used")
    # split ISO recordings
    for a in [a for a in angles if a.role == "video"]:
        base = a.media.path
        if re.search(r" 01(\.[^.]+)$", base):
            p2 = re.sub(r" 01(\.[^.]+)$", r" 02\1", base)
            if os.path.isfile(p2):
                warnings.append(f"{a.name}: recording is split ('{os.path.basename(p2)}' exists); "
                                "only ' 01' is used")

    # WAV angles
    if not args.skip_wavs:
        wavs = []
        for idx in sorted(sources):
            s = sources[idx]
            if s.get("type") != "Audio" or not s.get("file"):
                continue
            label = audio_label(s["file"], s.get("projectPath"))
            is_cam = re.match(r"(?i)cam(era)?\s*\d+$", label) is not None
            if is_cam and not args.camera_wavs:
                continue
            m = make_media(s.get("volume"), s.get("projectPath"), s["file"], s.get("startTimecode"), "audio")
            m.has_video = False
            wavs.append((0 if label.lower().startswith("mic") else 1 if label.lower().startswith("madi")
                         else 2 if not is_cam else 3, natural_key(label), label + (" Audio" if is_cam else ""), m, idx))
        for _, _, label, m, idx in sorted(wavs, key=lambda w: (w[0], w[1])):
            angles.append(Angle(label, m, idx, "audio"))

    # unique names and IDs
    seen = {}
    for n, a in enumerate(angles):
        base, k = a.name, 2
        while a.name.lower() in seen:
            a.name = f"{base} ({k})"
            k += 1
        seen[a.name.lower()] = a
        a.id = angle_id(a.name, n)

    # audio angle
    if args.audio_angle:
        audio = seen.get(args.audio_angle.lower())
        if audio is None and not getattr(args, "audio_angle_is_preference", False):
            raise ValueError(f"--audio-angle {args.audio_angle!r} not found; angles are: "
                             + ", ".join(a.name for a in angles))
        if audio is None:
            warnings.append(f"saved audio angle {args.audio_angle!r} is not in this show; using the default")
            audio = (next((a for a in angles if a.role == "program"), None)
                     or seen.get("mic 1") or angles[0])
        elif not audio.media.has_audio:
            warnings.append(f"audio angle {audio.name} has no audio")
    else:
        audio = (next((a for a in angles if a.role == "program"), None)
                 or seen.get("mic 1")
                 or angles[0])

    # shots; boundaries[i] is the event between shots[i] and shots[i + 1]
    shots, boundaries = [], []
    cur, pos = initial, rec_start
    for ev in events:
        edit = min(max(ev["start"] + (ev["end"] - ev["start"]) // 2, pos), rec_end)
        if edit > pos:
            if shots and shots[-1]["source"] == cur:
                shots[-1]["end"] = edit  # a zero-length shot in between was dropped
                boundaries[-1] = ev
            else:
                shots.append({"source": cur, "start": pos, "end": edit})
                boundaries.append(ev)
        pos, cur = edit, ev["source"]
    if pos < rec_end:
        if shots and shots[-1]["source"] == cur:
            shots[-1]["end"] = rec_end
            boundaries.pop()
        else:
            shots.append({"source": cur, "start": pos, "end": rec_end})
    elif boundaries:
        boundaries.pop()
    n_cuts = sum(1 for b in boundaries if b["kind"] == "cut")
    n_mixes = sum(1 for b in boundaries if b["kind"] == "mix")

    gaps = []
    for s in shots:
        s["angle"] = cam_by_source.get(s["source"])
        if s["angle"] is None:
            src = sources.get(s["source"], {})
            gaps.append(f"{vm.frames_to_tc(s['start'])}-{vm.frames_to_tc(s['end'])} "
                        f"{src.get('name', 'source ' + str(s['source']))} ({src.get('type', 'unknown')})")

    # transitions: half-length for each boundary, 0 = cut
    halves = [0] * len(boundaries)
    fallbacks, rounded = [], 0
    if not args.no_transitions:
        for i, b in enumerate(boundaries):
            if b["kind"] != "mix":
                continue
            length = b["end"] - b["start"]
            h = length // 2
            if h == 0:
                fallbacks.append(f"{vm.frames_to_tc(b['start'])} (mix too short)")
                continue
            if length % 2:
                rounded += 1
            left, right = shots[i], shots[i + 1]
            edit = left["end"]
            reason = None
            if left["angle"] is None or right["angle"] is None:
                reason = "next to a gap"
            elif left["end"] - left["start"] < (halves[i - 1] if i else 0) + h:
                reason = "previous shot too short"
            elif right["end"] - right["start"] < h:
                reason = "next shot too short"
            elif left["angle"].media.start + left["angle"].media.duration < edit + h:
                reason = f"{left['angle'].name} media ends too soon"
            elif right["angle"].media.start > edit - h:
                reason = f"{right['angle'].name} media starts too late"
            if reason:
                fallbacks.append(f"{vm.frames_to_tc(b['start'])} ({reason})")
            else:
                halves[i] = h

    # media extent covered by the multicam
    mc_start = min([rec_start] + [a.media.start for a in angles])
    mc_end = max([rec_end] + [a.media.start + a.media.duration for a in angles])
    short = [a.name for a in angles if a.media.probed and a.media.start + a.media.duration < rec_end
             and a.role != "audio"]
    if short:
        warnings.append("media ends before the recording end for: " + ", ".join(short))
    if finder.missing:
        notes.append(f"{finder.missing} media file(s) were not found; the paths the ATEM recorded were "
                     f"written. Mount the '{volume}' drive or relink the media after import.")
        notes.append("without the media, durations and channel counts come from the .drp (assumed stereo)")
    elif not have_ffprobe:
        notes.append("ffprobe not found; durations, timecode and channel counts come from the .drp")

    # ------------------------------------------------------------------ XML
    root = ET.Element("fcpxml", version="1.10")
    res = ET.SubElement(root, "resources")
    fmt_attrs = {"id": "r1", "name": vm.format_name, "frameDuration": vm.t(1),
                 "width": str(vm.width), "height": str(vm.height),
                 "colorSpace": "1-1-1 (Rec. 709)"}
    if vm.interlaced:
        fmt_attrs["fieldOrder"] = "upper first"
    ET.SubElement(res, "format", fmt_attrs)
    ET.SubElement(res, "effect", id="r2", name="Cross Dissolve", uid=CROSS_DISSOLVE_UID)
    rid = 3
    for a in angles:
        m = a.media
        m.id = f"r{rid}"
        rid += 1
        attrs = {"id": m.id, "name": m.name, "start": vm.t(m.start), "duration": vm.t(m.duration),
                 "hasVideo": "1" if m.has_video else "0"}
        if m.has_video:
            attrs["format"] = "r1"
            attrs["videoSources"] = "1"
        attrs["hasAudio"] = "1" if m.has_audio else "0"
        if m.has_audio:
            attrs["audioSources"] = str(m.audio_sources)
            attrs["audioChannels"] = str(m.audio_channels)
            attrs["audioRate"] = str(m.audio_rate)
        asset = ET.SubElement(res, "asset", attrs)
        ET.SubElement(asset, "media-rep", kind="original-media", src=file_url(m.path))
    mc_id = f"r{rid}"
    mc_name = f"{title} Multicam"
    media = ET.SubElement(res, "media", id=mc_id, name=mc_name)
    mc = ET.SubElement(media, "multicam", format="r1", tcStart=vm.t(rec_start), tcFormat="NDF")
    for a in angles:
        ang = ET.SubElement(mc, "mc-angle", name=a.name, angleID=a.id)
        m = a.media
        clip = {"ref": m.id, "offset": vm.t(m.start), "name": a.name, "start": vm.t(m.start),
                "duration": vm.t(m.duration), "tcFormat": "NDF"}
        if m.has_video:
            clip["format"] = "r1"
        ET.SubElement(ang, "asset-clip", clip)

    def mc_sources(parent, video_angle):
        if video_angle is audio:
            ET.SubElement(parent, "mc-source", angleID=audio.id, srcEnable="all")
        else:
            ET.SubElement(parent, "mc-source", angleID=video_angle.id, srcEnable="video")
            ET.SubElement(parent, "mc-source", angleID=audio.id, srcEnable="audio")

    lib = ET.SubElement(root, "library")
    event = ET.SubElement(lib, "event", name=title)
    browser_clip = ET.SubElement(event, "mc-clip", ref=mc_id, offset="0s", name=mc_name,
                                 start=vm.t(mc_start), duration=vm.t(mc_end - mc_start))
    mc_sources(browser_clip, angles[0])
    project = ET.SubElement(event, "project", name=title)
    seq = ET.SubElement(project, "sequence", format="r1", duration=vm.t(rec_len),
                        tcStart=vm.t(rec_start), tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(seq, "spine")
    for i, s in enumerate(shots):
        dur = s["end"] - s["start"]
        if s["angle"] is None:
            src = sources.get(s["source"], {})
            ET.SubElement(spine, "gap", name=src.get("name", "Gap"), offset=vm.t(s["start"]),
                          start="3600s", duration=vm.t(dur))
        else:
            clip = ET.SubElement(spine, "mc-clip", ref=mc_id, offset=vm.t(s["start"]),
                                 name=s["angle"].name, start=vm.t(s["start"]), duration=vm.t(dur))
            mc_sources(clip, s["angle"])
        if i < len(halves) and halves[i]:
            h = halves[i]
            tr = ET.SubElement(spine, "transition", name="Cross Dissolve",
                               offset=vm.t(s["end"] - h), duration=vm.t(2 * h))
            ET.SubElement(tr, "filter-video", ref="r2", name="Cross Dissolve")

    ET.indent(root, space="  ")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n'
           + ET.tostring(root, encoding="unicode") + "\n")

    summary = {
        "vm": vm, "title": title, "rec_start": rec_start, "rec_end": rec_end,
        "shots": len(shots), "cuts": n_cuts, "mixes": n_mixes,
        "transitions": sum(1 for h in halves if h), "fallbacks": fallbacks, "rounded": rounded,
        "angles": angles, "audio": audio, "gaps": gaps, "warnings": warnings, "notes": notes,
        "no_transitions": args.no_transitions,
    }
    return xml, summary


def format_summary(s, out_path=None):
    vm = s["vm"]
    lines = []
    p = lines.append
    if out_path:
        p(f"Wrote {out_path}")
    p(f"  Title:       {s['title']}")
    p(f"  Video mode:  {vm.mode} ({vm.format_name}, frameDuration {vm.t(1)})")
    p(f"  Recording:   {vm.frames_to_tc(s['rec_start'])} -> {vm.frames_to_tc(s['rec_end'])} "
      f"({duration_str(vm, s['rec_end'] - s['rec_start'])}, {s['rec_end'] - s['rec_start']} frames)")
    p(f"  Shots:       {s['shots']} ({s['cuts']} cuts, {s['mixes']} mixes)")
    if s["no_transitions"]:
        p("  Transitions: off; mixes cut at their midpoint")
    else:
        p(f"  Transitions: {s['transitions']} Cross Dissolves")
        if s["rounded"]:
            p(f"               {s['rounded']} odd-length mix(es) shortened by 1 frame to stay centred")
        for f in s["fallbacks"]:
            p(f"               mix at {f} -> cut")
    p(f"  Angles ({len(s['angles'])}):")
    for n, a in enumerate(s["angles"], 1):
        m = a.media
        kind = {"video": "camera", "program": "program", "audio": "audio only"}[a.role]
        status = "" if m.found else "  [NOT FOUND]"
        p(f"    {n:2d}. {a.name:<18} {kind:<10} {m.audio_channels}ch  {m.name}{status}")
    p(f"  Audio from:  {s['audio'].name} (same angle for the whole show)")
    if s["gaps"]:
        p(f"  Gaps ({len(s['gaps'])}, non-camera sources on program):")
        for g in s["gaps"]:
            p(f"    {g}")
    for w in s["warnings"]:
        p(f"  Warning: {w}")
    for n in s["notes"]:
        p(f"  Note: {n}")
    return "\n".join(lines)


def default_output(drp):
    return os.path.splitext(os.path.abspath(drp))[0] + ".fcpxml"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert an ATEM ISO recording (.drp) to FCPXML 1.10 "
                                             "with a multicam clip and the live-switched timeline.")
    ap.add_argument("drp", help="the ATEM .drp project file")
    ap.add_argument("-o", "--output", help="output .fcpxml (default: <drp name>.fcpxml next to the .drp)")
    ap.add_argument("--media-root", help="folder holding the ATEM project media (checked first)")
    ap.add_argument("--program", metavar="FILE", help="program recording to add as the 'Program' angle "
                                                      "(default: '<project> 01.mp4' if found)")
    ap.add_argument("--audio-angle", metavar="NAME", help="angle to take audio from for the whole show "
                                                          "(default: Program, else Mic 1, else first camera)")
    ap.add_argument("--no-transitions", action="store_true", help="make every mix a cut at its midpoint")
    ap.add_argument("--skip-wavs", action="store_true", help="don't add any WAV angles")
    ap.add_argument("--camera-wavs", action="store_true", help="also add the per-camera WAVs as angles")
    ap.add_argument("--title", help="event/project name (default: the ATEM project name)")
    ap.add_argument("--use-settings", action="store_true",
                    help=f"apply the options saved by the front end ({SETTINGS_PATH}); "
                         "flags given on the command line still apply")
    args = ap.parse_args(argv)
    if args.use_settings:
        saved = load_settings()
        args.no_transitions |= bool(saved["no_transitions"])
        args.skip_wavs |= bool(saved["skip_wavs"])
        args.camera_wavs |= bool(saved["camera_wavs"])
        if not args.audio_angle and saved["audio_angle"]:
            args.audio_angle = saved["audio_angle"]
            args.audio_angle_is_preference = True

    try:
        xml, summary = build(args)
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    out = args.output or default_output(args.drp)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(xml)
    print(format_summary(summary, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
