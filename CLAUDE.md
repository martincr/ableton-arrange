# ableton-arrange

Builds an Ableton Live arrangement from a JSON song structure file.

## Usage

```
python3 arrangement_tool.py structure.json base.als output.als
```

- `structure.json` — song structure and automation config
- `base.als` — source Ableton project; must contain at least one MIDI clip on the first MIDI track (used as clip template)
- `output.als` — path for the generated arrangement file

## structure.json format

```json
{
  "bpm": 128,
  "time_signature": [4, 4],
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
| `structure[].section` | Label (cosmetic only) |
| `structure[].bars` | Length of section in bars |
| `automations[].track` | Must match `EffectiveName` of a MIDI track in the `.als` |
| `automations[].parameter_pointee` | `ModulationTarget` / `Pointee` Id of the target parameter |
| `automations[].points[].bar` | 1-indexed bar number from song start |
| `automations[].points[].value` | Normalised `0.0–1.0` |

## How it works

1. Reads and decompresses the base `.als` (gzip XML)
2. Finds the first MIDI clip in the first MIDI track as a template
3. Places a single looping clip spanning the full arrangement on every MIDI track
4. Injects clip-level automation envelopes for any tracks listed in `automations`
5. Sets BPM and writes the output `.als`

## base.als requirements

- Must contain at least one `MidiClip` (session or arrangement) on the first MIDI track
- Track `EffectiveName` values must match names used in `automations` for automation to apply
- `NextPointeeId` is read and incremented automatically — no manual management needed

## Tracks in base.als

| Track | Tag |
|---|---|
| `1-Analog` | MidiTrack |
| `2-DS Kick` | MidiTrack |

`NextPointeeId` in base.als starts at `22492`.

## Notes

- Automation `parameter_pointee` IDs can be found by inspecting the `.als` XML (decompress with `gzip`, search for `ModulationTarget` or `Pointee Id`)
- Mismatched track names in `automations` are silently ignored — clips are still placed without automation
- The "Automation written for" line in script output reflects track names from the JSON, **not** confirmed matches against the `.als` — always verify names against the track list above
- The source clip name may be empty; this is fine as long as a `MidiClip` element exists with notes
- Each clip's `CurrentEnd` is extended to the full arrangement length, but the loop region (`LoopEnd`/`OutMarker`) is preserved from the source clip — Ableton repeats the source pattern throughout the arrangement rather than playing silence
- `OverwriteProtectionNumber` is incremented in the output so Ableton detects the file as modified and reloads the arrangement view correctly
- Section markers are written to `LiveSet/Locators/Locators` at each section's start beat
