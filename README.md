# ableton-arrange

A command-line tool that builds an Ableton Live arrangement from a JSON song structure. Define your sections, BPM, and parameter automation in a single JSON file — the script writes a ready-to-open `.als` with clips placed, looping, and markers set.

## Requirements

- Python 3 (stdlib only — no dependencies)
- Ableton Live 12

## Usage

```
python3 arrangement_tool.py structure.json base.als output.als
```

| Argument | Description |
|---|---|
| `structure.json` | Song structure and automation config |
| `base.als` | Source project containing one MIDI clip per track |
| `output.als` | Path for the generated arrangement |

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
  ]
}
```

### Fields

| Field | Description |
|---|---|
| `bpm` | Project tempo |
| `time_signature` | `[beats_per_bar, beat_unit]` — only `beats_per_bar` is currently used |
| `structure[].section` | Section name — used as the arrangement marker label |
| `structure[].bars` | Length of the section in bars |
| `automations[].track` | Must exactly match the `EffectiveName` of a MIDI track in the `.als` |
| `automations[].parameter_pointee` | The `Pointee Id` of the target parameter (see below) |
| `automations[].points[].bar` | 1-indexed bar number from the start of the song |
| `automations[].points[].value` | Normalised value `0.0–1.0` |

## How it works

1. Decompresses the base `.als` (gzip XML)
2. For each MIDI track, uses that track's own session/arrangement clip as the loop template
3. Places a single clip per track spanning the full arrangement length, with the original loop region preserved so the source pattern repeats throughout
4. Writes clip-level automation envelopes for any tracks listed in `automations`
5. Adds named arrangement markers at each section boundary
6. Sets BPM and writes the output `.als`

## Base project requirements

Each MIDI track that should appear in the arrangement needs at least one MIDI clip — either in a session slot or already in the arrangement. The script will skip any track with no clip. Track names used in `automations` must exactly match the track's `EffectiveName` in Ableton.

## Finding parameter_pointee IDs

To automate a parameter, you need its `Pointee Id` from the `.als` XML:

```bash
# Decompress the .als and search for the parameter name
python3 -c "
import gzip
print(gzip.open('base.als','rb').read().decode('utf-8'))
" | grep -i "FilterFreq" | head -20
```

Look for `<AutomationTarget Id="...">` or `<ModulationTarget Id="...">` adjacent to the parameter element — that `Id` value is the `parameter_pointee`.

## Output

Opening `output.als` in Ableton will show:
- Arrangement view clips on every MIDI track, each looping their source pattern
- Named markers at each section start (visible in the arrangement timeline)
- BPM set as specified

If clips are not visible, press **Tab** to switch to Arrangement View.
