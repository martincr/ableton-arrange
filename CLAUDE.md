# ableton-arrange

Builds an Ableton Live arrangement from a JSON song structure file.

## Usage

```
python3 arrangement_tool.py structure.json base.als output.als [--backup]
python3 arrangement_tool.py --xml base.als
python3 arrangement_tool.py --inspect base.als [track]
```

- `structure.json` — song structure and automation config
- `base.als` — source Ableton project; must contain at least one MIDI clip per MIDI track
- `output.als` — path for the generated arrangement file
- `--backup` — if `output.als` already exists, copy it to `output.als.backup` before overwriting
- `--xml ALS` — decompress a `.als` to readable XML for inspection (e.g. finding `parameter_pointee` IDs)
- `--inspect ALS [track]` — print a structured summary: BPM, tracks, session clips and bar lengths, and arrangement status; pass a track name to instead list its automatable parameters (device, parameter, target id, current value, range) and Send target ids

There's also `analyze_audio.py`, a separate script (own dependencies: numpy/scipy/scikit-learn/librosa/soundfile — see its docstring) that analyzes a WAV/MP3 and proposes a bar-aligned `structure.json` skeleton with placeholder section names for a human to rename. It doesn't affect `arrangement_tool.py`, which stays stdlib-only.

## structure.json format

```json
{
  "bpm": 128,
  "time_signature": [4, 4],
  "track_height": 68,
  "structure": [
    {"section": "intro",  "bars": 8},
    {"section": "drop",   "bars": 16},
    {"section": "outro",  "bars": 8}
  ],
  "automations": [
    {
      "track": "1-Analog",
      "parameter_pointee": 22278,
      "points": [
        {"bar": 1,  "value": 0.0},
        {"bar": 17, "value": 1.0}
      ]
    }
  ],
  "track_segments": {
    "2-DS Kick": [[1, 9], [17, 25]]
  },
  "send_throws": [
    {
      "send_index": 0,
      "points": [
        {"bar": 17, "value": 0.9},
        {"bar": 19, "value": 0.05}
      ]
    }
  ],
  "track_notes": {
    "2-DS Kick": {
      "loop_bars": 1,
      "notes": [
        {"pitch": "C1", "start": 0, "duration": 0.5, "velocity": 110},
        {"pitch": "C1", "start": 2, "duration": 0.5}
      ]
    }
  }
}
```

### Key fields

| Field | Description |
|---|---|
| `bpm` | Project tempo |
| `time_signature` | `[beats_per_bar, beat_unit]` — only `beats_per_bar` is used |
| `track_height` | Arrangement lane height in pixels, 17–425 (default: 68) |
| `structure[].section` | Section name — used as the arrangement marker label |
| `structure[].bars` | Length of section in bars |
| `automations[].track` | Must match `EffectiveName` of a MIDI track in the `.als` |
| `automations[].parameter_pointee` | `ModulationTarget` / `Pointee` Id of the target parameter |
| `automations[].points[].bar` | 1-indexed bar number from song start |
| `automations[].points[].value` | Normalised `0.0–1.0` |
| `track_segments` | Optional. Maps a track name to a list of `[start_bar, end_bar)` ranges (end exclusive) — that track gets one looping clip per range instead of one clip spanning the whole song, so it can drop in/out. Tracks not listed keep the default full-length behavior |
| `send_throws[].send_index` | 0-indexed Send slot; applies the same automation `points` to that Send on every track that has one, without listing a `parameter_pointee` per track |
| `send_throws[].points` | Same `{bar, value}` shape as `automations[].points` |
| `track_notes` | Optional. Maps a track name to `{loop_bars, notes}` — replaces the note content of that track's source clip before it is placed, so patterns can be written from JSON instead of played in |
| `track_notes[].loop_bars` | Optional. Resizes the clip's loop region so the written pattern repeats at this length. Set it whenever the pattern differs in length from the source clip, or placed clips keep looping at the old length |
| `track_notes[].notes[].pitch` | MIDI number `0–127`, or a name where C3 = 60 (`"C3"`, `"F#2"`, `"Bb4"`) |
| `track_notes[].notes[].start` | Beats from the clip start (not bars) |
| `track_notes[].notes[].duration` | Beats (default: `1.0`) |
| `track_notes[].notes[].velocity` | `0–127` (default: `100`) |
| `track_notes[].notes[].off_velocity` | `0–127` (default: `64`) |

## How it works

1. Reads and decompresses the base `.als` (gzip XML)
2. For each MIDI track, uses that track's own clip as the source template
3. Rewrites that source clip's notes for any track listed in `track_notes` (in place, so the session clip shows the new pattern too)
4. Places one looping clip per track spanning the full arrangement length by default, or one clip per `track_segments` range for tracks that have segments
5. Injects clip-level automation envelopes for tracks listed in `automations`, plus any `send_throws` (filtered and re-anchored to clip-relative time for segmented tracks)
6. Writes named markers at each section boundary
7. Sets BPM (handles Live 12 `MainTrack`, Live 10/11 `MasterTrack`, and pre-v10 legacy paths)
8. Optionally backs up an existing `output.als` to `output.als.backup` (`--backup`)
9. Writes the output `.als`

## base.als requirements

- Each MIDI track that should appear in the arrangement needs at least one `MidiClip` (session or arrangement) — tracks with no clip are skipped
- Track `EffectiveName` values must match names used in `automations` for automation to apply
- `NextPointeeId` is read and incremented automatically — no manual management needed

## Tracks in base.als

| Track | Tag |
|---|---|
| `1-Analog` | MidiTrack |
| `2-DS Kick` | MidiTrack |

`NextPointeeId` in base.als starts at `22492`.

## Finding parameter_pointee IDs

```
python3 arrangement_tool.py --inspect base.als 1-Analog
```

or manually:

```
python3 arrangement_tool.py --xml base.als
grep -i "ParameterName" base.xml
```

Look for `<AutomationTarget Id="...">` or `<ModulationTarget Id="...">` adjacent to the parameter element — that `Id` is the `parameter_pointee`.

## Notes

- Mismatched track names in `automations` are silently ignored — clips are still placed without automation
- The "Automation written for" line in script output reflects track names from the JSON, **not** confirmed matches against the `.als` — always verify names against the track list above
- Each clip's `CurrentEnd` is extended to the full arrangement length, but `LoopEnd`/`OutMarker` are preserved from the source clip so Ableton repeats the source pattern rather than playing silence
- `OverwriteProtectionNumber` is incremented in the output so Ableton detects the file as modified and reloads the arrangement view correctly
- Section markers are written to `LiveSet/Locators/Locators` at each section's start beat
- `track_height` sets `LaneHeight` directly on track elements — effective on Live ≤11; no-ops silently on Live 12 where height is stored elsewhere
- `automations[].points` and `send_throws[].points` use global song bar numbers. For a track with `track_segments`, points outside a given segment's bar range don't apply to that segment's clip — there's no interpolation across the gap, so add explicit points near each segment boundary if you need a specific value there
- `--backup` only triggers when the output path already exists — the first run to a new path never creates a `.backup` file
- `track_notes` edits the track's source clip in place, so the rewritten pattern appears in the session view as well as in every placed arrangement clip. A track with more than one session clip only has its *first* clip (the one `find_any_clip` returns) rewritten
- `track_notes[].notes[].start` is in **beats**, unlike `automations[].points[].bar` which is in bars — a 4/4 bar is 4 beats
- Writing notes without setting `loop_bars` keeps the source clip's original loop length, so a 1-bar pattern written into a 4-bar clip still repeats every 4 bars with 3 bars of silence
- An empty `notes` list is valid and clears the clip
