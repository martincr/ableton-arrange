# ableton-arrange

Builds an Ableton Live arrangement from a JSON song structure file.

## Usage

```
python3 arrangement_tool.py structure.json base.als output.als
python3 arrangement_tool.py --xml base.als
```

- `structure.json` — song structure and automation config
- `base.als` — source Ableton project; must contain at least one MIDI clip per MIDI track
- `output.als` — path for the generated arrangement file
- `--xml ALS` — decompress a `.als` to readable XML for inspection (e.g. finding `parameter_pointee` IDs)

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
  ]
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

## How it works

1. Reads and decompresses the base `.als` (gzip XML)
2. For each MIDI track, uses that track's own clip as the source template
3. Places a single clip per track spanning the full arrangement length, looping the source pattern
4. Injects clip-level automation envelopes for tracks listed in `automations`
5. Writes named markers at each section boundary
6. Sets BPM (handles Live 12 `MainTrack`, Live 10/11 `MasterTrack`, and pre-v10 legacy paths)
7. Writes the output `.als`

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
