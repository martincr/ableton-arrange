# ableton-arrange

A command-line tool that builds an Ableton Live arrangement from a JSON song structure. Define your sections, BPM, and parameter automation in a single JSON file — the script writes a ready-to-open `.als` with clips placed, looping, and markers set.

## Requirements

- Python 3 (stdlib only — no dependencies)
- Ableton Live 12

## Usage

```
python3 arrangement_tool.py structure.json base.als output.als [--backup]
python3 arrangement_tool.py --xml base.als
python3 arrangement_tool.py --inspect base.als [track]
```

| Argument | Description |
|---|---|
| `structure.json` | Song structure and automation config |
| `base.als` | Source project containing one MIDI clip per track |
| `output.als` | Path for the generated arrangement |
| `--backup` | If `output.als` already exists, copy it to `output.als.backup` before overwriting |
| `--xml ALS` | Decompress a `.als` to readable XML and exit — useful for finding `parameter_pointee` IDs |
| `--inspect ALS [track]` | Print a structured summary of tracks, session clips, and automation parameters; pass a track name to list just that track's automatable parameters and Send targets |

## structure.json

```json
{
  "bpm": 128,
  "time_signature": [4, 4],
  "structure": [
    {"section": "intro",     "bars": 8},
    {"section": "build",     "bars": 8},
    {"section": "drop",      "bars": 16},
    {"section": "breakdown", "bars": 8},
    {"section": "build",     "bars": 8},
    {"section": "drop",      "bars": 16},
    {"section": "outro",     "bars": 8}
  ],
  "automations": [
    {
      "track": "1-Analog",
      "parameter_pointee": 22278,
      "points": [
        {"bar": 1,  "value": 0.0},
        {"bar": 9,  "value": 0.5},
        {"bar": 17, "value": 1.0},
        {"bar": 33, "value": 0.0}
      ]
    }
  ],
  "track_segments": {
    "2-DS Kick": [[1, 9], [17, 33]]
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
        {"pitch": "C1", "start": 0,   "duration": 0.5, "velocity": 110},
        {"pitch": "C1", "start": 2,   "duration": 0.5},
        {"pitch": 42,   "start": 0.5, "duration": 0.25}
      ]
    }
  }
}
```

### Fields

| Field | Description |
|---|---|
| `bpm` | Project tempo |
| `time_signature` | `[beats_per_bar, beat_unit]` — only `beats_per_bar` is currently used |
| `track_height` | Arrangement lane height in pixels, 17–425 (default: `68`) |
| `structure[].section` | Section name — used as the arrangement marker label |
| `structure[].bars` | Length of the section in bars |
| `automations[].track` | Must exactly match the `EffectiveName` of a MIDI track in the `.als` |
| `automations[].parameter_pointee` | The `Pointee Id` of the target parameter (see below) |
| `automations[].points[].bar` | 1-indexed bar number from the start of the song |
| `automations[].points[].value` | Normalised value `0.0–1.0` |
| `track_segments` | Optional: `{"track name": [[start_bar, end_bar), ...]}`. Gives that track one looping clip per bar range instead of one clip spanning the whole song, so it can drop in and out (e.g. drums silent during a breakdown). Tracks not listed get the default full-length clip |
| `send_throws[].send_index` | 0-indexed Send slot. Applies the same `points` to that Send on every track that has one — a shortcut for something like a reverb "dub throw" spike across the whole arrangement without listing a `parameter_pointee` per track |
| `send_throws[].points` | Same `{bar, value}` shape as `automations[].points` |
| `track_notes` | Optional: `{"track name": {"loop_bars": N, "notes": [...]}}`. Replaces the note content of that track's source clip, so you can write patterns in JSON rather than playing them in |
| `track_notes[].loop_bars` | Optional. Resizes the loop region so the written pattern repeats at this length. Set it whenever your pattern is a different length from the source clip |
| `track_notes[].notes[].pitch` | MIDI number `0–127`, or a note name where C3 = 60 — `"C3"`, `"F#2"`, `"Bb4"` |
| `track_notes[].notes[].start` | Position in **beats** from the clip start |
| `track_notes[].notes[].duration` | Length in beats (default `1.0`) |
| `track_notes[].notes[].velocity` | `0–127` (default `100`) |
| `track_notes[].notes[].off_velocity` | `0–127` (default `64`) |

## How it works

1. Decompresses the base `.als` (gzip XML)
2. For each MIDI track, uses that track's own session/arrangement clip as the loop template
3. Rewrites that clip's notes for any track listed in `track_notes`
4. Places a single clip per track spanning the full arrangement length by default — or, for tracks listed in `track_segments`, one clip per bar range — with the loop region preserved so the pattern repeats throughout
5. Writes clip-level automation envelopes for any tracks listed in `automations`, plus any `send_throws`
6. Adds named arrangement markers at each section boundary
7. Sets BPM
8. Optionally backs up an existing output file (`--backup`) before overwriting
9. Writes the output `.als`

## Base project requirements

Each MIDI track that should appear in the arrangement needs at least one MIDI clip — either in a session slot or already in the arrangement. The script will skip any track with no clip. Track names used in `automations` must exactly match the track's `EffectiveName` in Ableton.

This applies to `track_notes` too: it rewrites the notes *inside* an existing clip, so the track still needs a clip to write into. An empty clip is enough — the notes in it are replaced wholesale — but a track with no clip at all is skipped, and the script prints a warning naming any `track_notes` entry that never matched.

## Finding parameter_pointee IDs

Use `--inspect` for a structured view of a track's automatable parameters:

```bash
python3 arrangement_tool.py --inspect base.als 1-Analog
```

Or use `--xml` to dump a readable copy of any `.als` and search it manually:

```bash
python3 arrangement_tool.py --xml base.als
grep -i "FilterFreq" base.xml | head -20
```

Look for `<AutomationTarget Id="...">` or `<ModulationTarget Id="...">` adjacent to the parameter — that `Id` is the `parameter_pointee`.

## Automation ideas by genre

Rough starting points for hand-authoring `automations` and `send_throws` — anchor every point to a specific bar, and give every spike a return value shortly after:

**Classic dub**
- Filter cutoff: open gradually through the groove section, close in the outro
- Delay feedback: spike during the breakdown for dub throws, at specific bars
- Reverb send: low throughout; spike on the same bars as the delay throws, then let it bloom into the next build
- Bass echo: a single feedback spike at one key moment in the breakdown

**Techno**
- Filter: very slow open over 32–64 bars, no dramatic peaks
- Reverb: stays low; only brief throws at key moments
- Delay feedback: builds gradually, pulls back at the drop

**House**
- Filter: opens into the drop, closes for the verse
- Sidechain/pump automation, if the device has one
- Reverb: more ambient throughout, bigger in breakdowns

## Output

Opening `output.als` in Ableton will show:
- Arrangement view clips on every MIDI track, each looping their source pattern (or dropping in/out per `track_segments`)
- Named markers at each section start (visible in the arrangement timeline)
- BPM set as specified

If clips are not visible, press **Tab** to switch to Arrangement View.
