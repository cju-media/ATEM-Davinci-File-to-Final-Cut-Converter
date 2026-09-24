# atem2fcp

Convert a Blackmagic ATEM ISO recording project (`.drp`) into an FCPXML 1.10
file that imports into **Final Cut Pro** and **DaVinci Resolve**
(File → Import → Timeline).

The output contains:

- a **multicam clip** with one angle per ISO camera (named after the ATEM
  input label), plus the program recording and the Mic/MADI WAVs, all synced
  by timecode;
- a **project timeline** that reproduces the live switching: one multicam
  clip per shot, with ATEM mixes turned into Cross Dissolves and audio taken
  from a single angle for the whole show.

The Mic and MADI WAVs are written as audio-only angles (`hasVideo="0"`).
Final Cut Pro has no separate type for audio angles, so they still appear in
the Angle Viewer next to the cameras, as black tiles. To leave them out,
untick the WAV option in the front end or use `--skip-wavs`.

Python 3, standard library only. If `ffprobe` is on the `PATH` it is used to
read real clip durations, audio channel counts and start timecodes; otherwise
the values come from the `.drp`.

There are three ways to run it:

- **Finder:** right-click a `.drp` → Quick Actions → **Convert ATEM to FCPXML**.
- **Front end:** a window for choosing the options and previewing the result
  before converting.
- **Command line:** `atem2fcp.py`.

## Finder Quick Actions

Install them once:

```bash
python3 install_services.py
```

That adds two items to the right-click menu for `.drp` files, under
**Quick Actions** (and under **Services**):

- **Convert ATEM to FCPXML** writes `<name>.fcpxml` next to each selected
  `.drp`, replacing any earlier one. Select several `.drp` files to convert
  them all at once.
  - When it works cleanly, you get a notification with the shot count.
  - If there are warnings, you get an alert listing them. For example, when
    the media drive isn't mounted or a file isn't an ATEM project.
- **Convert ATEM to FCPXML with Options…** opens the front end with that
  `.drp` loaded.

The Quick Actions use the options you last saved with **Save as Finder
Defaults** in the front end, stored in
`~/Library/Application Support/atem2fcp/settings.json`. If you haven't saved
any, they use the command-line defaults.

Notes:

- The Quick Actions run the scripts from this folder, using the Python that
  ran the installer. If you move the folder or change Python, run the
  installer again.
- The first time a Quick Action reads media from an external drive, macOS
  may ask for permission to access removable volumes. Allow it.
- If the items don't appear, turn them on in **System Settings → Keyboard →
  Keyboard Shortcuts → Services → Files and Folders**.
- The menu items are limited to `.drp` files through the type macOS assigns
  to the `.drp` extension. If an app installed later claims `.drp` (DaVinci
  Resolve uses it for project files), the items may stop appearing.
- To remove them, run `python3 install_services.py --uninstall`.

## Front end

```bash
python3 atem2fcp_gui.py                # choose a .drp in the window
python3 atem2fcp_gui.py show.drp       # open with a .drp loaded
```

Choose a `.drp` and the window shows a live preview of the conversion:

- video mode
- shots, cuts and mixes
- angles, with any missing media highlighted
- where the audio comes from
- any gaps and warnings

The preview updates whenever you change an option. Then click **Convert**
(or press Return). The window closes once the file is written. If there were warnings, they are shown first.

The window has the same options as the command line:

- where to save the file;
- the media folder and program recording;
- the project title;
- which angle the audio comes from, picked from the angles in this show;
- cross dissolves on or off;
- which WAVs to include.

**Save as Finder Defaults** stores the transition, WAV and audio-angle options
for the Finder Quick Action. The paths and title are per show, so they aren't
saved. The window also opens with the saved options.

The front end needs a Python with tkinter. The python.org installers include
it. With Homebrew, install `python-tk`.

## Command line

```
atem2fcp.py file.drp [-o out.fcpxml] [--media-root DIR] [--program FILE]
                     [--audio-angle NAME] [--no-transitions]
                     [--skip-wavs] [--camera-wavs] [--title TITLE]
                     [--use-settings]
```

| Option | Effect |
| --- | --- |
| `-o`, `--output` | Output file. Default: `<drp name>.fcpxml` next to the `.drp`. |
| `--media-root DIR` | Folder holding the ATEM project media. Checked before the default locations. |
| `--program FILE` | Program recording to add as the `Program` angle. Default: `<project> 01.mp4` in the project folder, if it exists. |
| `--audio-angle NAME` | Angle to take audio from for the whole show (case-insensitive). Default: `Program`, else `Mic 1`, else the first camera. |
| `--no-transitions` | Turn every mix into a cut at its midpoint. |
| `--skip-wavs` | Don't add any WAV angles. |
| `--camera-wavs` | Also add the per-camera WAVs (`CAM 1 Audio`, …) as angles. Off by default. |
| `--title TITLE` | Event and project name. Default: the ATEM project name. |
| `--use-settings` | Apply the options saved by the front end (what the Finder Quick Action uses). Flags on the command line still apply as well. If the saved audio angle isn't in a show, the default is used, with a warning. |

## Examples

Convert a recording straight off the ATEM drive. The FCPXML is written next to
the `.drp`:

```bash
python3 atem2fcp.py "/Volumes/ATEM SSD/Sunday Service/Sunday Service.drp"
```

Write the output somewhere else and give the project a different name:

```bash
python3 atem2fcp.py show.drp -o ~/Desktop/show.fcpxml --title "Evening Concert"
```

The media was copied off the ATEM drive to a RAID:

```bash
python3 atem2fcp.py show.drp --media-root "/Volumes/RAID/Shows/Sunday Service"
```

Take the audio from the desk mix on MADI 3 rather than the program recording:

```bash
python3 atem2fcp.py show.drp --audio-angle "madi 3"
```

Cuts only, video angles only (no WAVs), for a quick rough cut:

```bash
python3 atem2fcp.py show.drp --no-transitions --skip-wavs
```

Include each camera's own audio as extra angles:

```bash
python3 atem2fcp.py show.drp --camera-wavs
```

### Sample output

```
Wrote /Volumes/ATEM SSD/Sunday Service/Sunday Service.fcpxml
  Title:       Sunday Service
  Video mode:  1080p30 (FFVideoFormat1080p30, frameDuration 100/3000s)
  Recording:   09:59:59:01 -> 11:37:22:27 (1h 37m 23s 26f, 175316 frames)
  Shots:       33 (25 cuts, 7 mixes)
  Transitions: 7 Cross Dissolves
               3 odd-length mix(es) shortened by 1 frame to stay centred
  Angles (26):
     1. Camera 1           camera     2ch  Sunday Service CAM 1 01
     ...
     9. Program            program    2ch  Sunday Service 01
    10. Mic 1              audio only 2ch  Sunday Service Mic 1 01
    11. MADI 1             audio only 2ch  Sunday Service MADI 1 01
     ...
  Audio from:  Program (same angle for the whole show)
```

## Finding the media

Each file named in the `.drp` is looked for in this order:

1. `--media-root`, if given
2. the `.drp`'s own folder
3. `/Volumes/<volume>/<projectPath>` as recorded by the ATEM

If a file isn't found, the path the ATEM recorded is written anyway and the
summary tells you to mount the drive or relink the media after import. In that
case durations come from the recording length and audio is assumed to be
stereo.

If a recording was split into ` 01` and ` 02` files (long shows), the tool
warns and uses only the ` 01` file.

## How the switching is converted

- A `source` change is a **cut** at that timecode.
- `transitionActive: true` followed by `transitionActive: false` with a new
  `source` is a **mix**. The edit point is the mix midpoint, and the Cross
  Dissolve starts where the mix started. A mix with an odd number of frames is
  shortened by one frame so the dissolve stays centred on the edit.
- A mix that ends without a source change is ignored.
- A mix falls back to a cut if a neighbouring shot is too short, is a gap, or
  the media doesn't extend far enough.
- Switches to non-camera sources (Black, Color Bars, colors, media players)
  become **gaps**, and the summary lists them.
- `onAir: false` marks the end of the recording. If it's missing, the end is 1
  second after the last event.

Supported video modes include 1080p23.98/24/25/29.97/30/50/59.94/60,
1080i50/59.94, 720p and 2160p. All times are written as exact frame-based
rationals, and timecode is non-drop-frame.

## Checking the output

`verify_fcpxml.py` runs structural checks on a generated file:

- the spine is contiguous;
- the last clip ends at the sequence end;
- each transition is centred on an edit point between two camera clips;
- the audio angle never changes.

Optionally it also checks the expected shot and transition counts:

```bash
python3 verify_fcpxml.py "Sunday Service.fcpxml" 33 7
```

It doesn't replace a test import. Sync, audio routing and media linking can
only be confirmed in Final Cut Pro or Resolve.
