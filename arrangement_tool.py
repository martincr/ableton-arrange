"""
arrangement_tool.py
-------------------
Build an Ableton arrangement from a JSON song structure.

Usage:
    python3 arrangement_tool.py structure.json base.als output.als

The base .als must contain at least one clip somewhere (session or arrangement)
on the first MIDI track — that clip is used as the loop source template.

JSON format:
{
  "bpm": 130,
  "time_signature": [4, 4],
  "structure": [
    {"section": "intro",     "bars": 8},
    {"section": "build",     "bars": 8},
    {"section": "drop",      "bars": 16},
    {"section": "breakdown", "bars": 8},
    {"section": "drop",      "bars": 16},
    {"section": "outro",     "bars": 8}
  ],
  "automations": [
    {
      "track": "1-Analog",
      "parameter_pointee": 22278,
      "points": [
        {"bar": 1,  "value": 0.05},
        {"bar": 17, "value": 0.9},
        {"bar": 33, "value": 0.05}
      ]
    }
  ]
}

parameter_pointee: the ModulationTarget/Pointee Id of the parameter.
values: normalised 0.0–1.0.
bars: 1-indexed from start of song.
"""

import gzip, re, json, sys, copy
import xml.etree.ElementTree as ET


GUPAT = (r'(<(?:AutomationTarget|ModulationTarget|Pointee|ControllerTargets\.\d+|'
         r'VolumeModulationTarget|TranspositionModulationTarget|'
         r'TransientEnvelopeModulationTarget|GrainSizeModulationTarget|'
         r'FluxModulationTarget|SampleOffsetModulationTarget|'
         r'ComplexProFormantsModulationTarget|ComplexProEnvelopeModulationTarget'
         r')\s+Id=")(\d+)(")')


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_next_id(xml_str):
    m = re.search(r'NextPointeeId Value="(\d+)"', xml_str)
    return int(m.group(1)) if m else 30000

def set_next_id(xml_str, value):
    return re.sub(r'(<NextPointeeId Value=")[^"]*(")', rf'\g<1>{value}\2',
                  xml_str, count=1)

def set_bpm(xml_str, bpm):
    return re.sub(r'(<Tempo>.*?<Manual Value=")[^"]*(")',
                  rf'\g<1>{bpm}\2', xml_str, count=1, flags=re.DOTALL)

def find_any_clip(track_el):
    """Find any MidiClip in the track — session or arrangement."""
    # Try session slots first
    for clip in track_el.findall('.//MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/MidiClip'):
        return clip
    # Fall back to arrangement
    for clip in track_el.findall('.//ClipTimeable/ArrangerAutomation/Events/MidiClip'):
        return clip
    return None


# ── Clip builder ──────────────────────────────────────────────────────────────

def make_arrangement_clip(source_el, start_beat, length_beats, clip_id,
                          automations=None):
    """
    Clone source_el and configure it as an arrangement clip.

    automations: dict of {pointee_id: [{"beat": float, "value": float}, ...]}
    """
    clip = copy.deepcopy(source_el)
    clip.set('Id', str(clip_id))
    clip.set('Time', str(start_beat))

    # Clip extent
    clip.find('CurrentStart').set('Value', '0')
    clip.find('CurrentEnd').set('Value', str(length_beats))

    # Loop region
    loop = clip.find('Loop')
    loop.find('LoopStart').set('Value', '0')
    loop.find('LoopEnd').set('Value', str(length_beats))
    loop.find('OutMarker').set('Value', str(length_beats))
    loop.find('HiddenLoopStart').set('Value', '0')
    loop.find('HiddenLoopEnd').set('Value', str(length_beats))
    loop.find('StartRelative').set('Value', '0')
    loop.find('LoopOn').set('Value', 'true')

    # Remove/rebuild Envelopes
    env_outer = clip.find('Envelopes')
    if env_outer is None:
        env_outer = ET.SubElement(clip, 'Envelopes')
    env_inner = env_outer.find('Envelopes')
    if env_inner is None:
        env_inner = ET.SubElement(env_outer, 'Envelopes')
    for child in list(env_inner):
        env_inner.remove(child)

    if automations:
        for env_idx, (pointee, points) in enumerate(automations.items()):
            env_el = ET.SubElement(env_inner, 'ClipEnvelope', Id=str(env_idx))

            target = ET.SubElement(env_el, 'EnvelopeTarget')
            ET.SubElement(target, 'PointeeId', Value=str(pointee))

            automation = ET.SubElement(env_el, 'Automation')
            events = ET.SubElement(automation, 'Events')

            # Sentinel: initial value applied before the clip starts
            ET.SubElement(events, 'FloatEvent',
                          Id='0', Time='-63072000',
                          Value=str(points[0]['value']))
            for i, pt in enumerate(points):
                ET.SubElement(events, 'FloatEvent',
                              Id=str(i + 1),
                              Time=str(pt['beat']),
                              Value=str(pt['value']))

            xf = ET.SubElement(automation, 'AutomationTransformViewState')
            ET.SubElement(xf, 'IsTransformPending', Value='false')
            ET.SubElement(xf, 'TimeAndValueTransforms')

            loop_slot = ET.SubElement(env_el, 'LoopSlot')
            ET.SubElement(loop_slot, 'Value')
            scr = ET.SubElement(env_el, 'ScrollerTimePreserver')
            ET.SubElement(scr, 'LeftTime', Value='0')
            ET.SubElement(scr, 'RightTime', Value='0')

    return clip


# ── Main ───────────────────────────────────────────────────────────────────────

def build_arrangement(cfg, base_als_path, output_als_path):
    bpm            = cfg.get('bpm', 120)
    beats_per_bar  = cfg.get('time_signature', [4, 4])[0]
    sections       = cfg['structure']
    auto_specs     = cfg.get('automations', [])

    with gzip.open(base_als_path, 'rb') as f:
        als_xml = f.read().decode('utf-8')

    root   = ET.fromstring(als_xml)
    tracks = root.find('LiveSet/Tracks')

    # Index tracks by name
    track_by_name = {t.find('.//EffectiveName').get('Value'): t for t in tracks}

    # Source clip template — from first MIDI track
    t0 = next(t for t in tracks if t.tag == 'MidiTrack')
    source_clip = find_any_clip(t0)
    if source_clip is None:
        raise ValueError("No MIDI clip found in the file to use as source template")
    print(f"Source clip: {source_clip.find('Name').get('Value')!r}, "
          f"notes={len(source_clip.findall('.//MidiNoteEvent'))}")

    # Compute section positions
    sections_info = []
    cursor = 0
    for sec in sections:
        lb = sec['bars'] * beats_per_bar
        sections_info.append({'name': sec['section'], 'start': cursor, 'length': lb})
        cursor += lb
    total_beats = cursor

    # Prepare automation per track: {track_name: {pointee: [{beat, value}]}}
    # Global bar numbers → beats within the single full-length clip (start=0)
    auto_by_track = {}
    for spec in auto_specs:
        tname   = spec['track']
        pointee = spec['parameter_pointee']
        pts = [
            {'beat': (pt['bar'] - 1) * beats_per_bar, 'value': pt['value']}
            for pt in spec['points']
        ]
        auto_by_track.setdefault(tname, {})[pointee] = pts

    clip_id = get_next_id(als_xml)

    # Place clips on every MIDI track
    for t in tracks:
        if t.tag != 'MidiTrack':
            continue
        tname     = t.find('.//EffectiveName').get('Value')
        events_el = t.find('.//ClipTimeable/ArrangerAutomation/Events')
        if events_el is None:
            continue

        # Clear existing arrangement clips
        for child in list(events_el):
            events_el.remove(child)

        automations = auto_by_track.get(tname, {}) or None

        # Single clip spanning full arrangement
        arr_clip = make_arrangement_clip(
            source_clip,
            start_beat=0,
            length_beats=total_beats,
            clip_id=clip_id,
            automations=automations,
        )
        clip_id += 1
        events_el.append(arr_clip)

    # Write out
    new_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode')
    new_xml = set_bpm(new_xml, bpm)
    new_xml = set_next_id(new_xml, clip_id)

    with gzip.open(output_als_path, 'wb') as f:
        f.write(new_xml.encode('utf-8'))

    # Report
    print(f"\nBPM: {bpm}  |  Total: {total_beats/beats_per_bar:.0f} bars ({total_beats} beats)")
    print(f"\nSections:")
    for s in sections_info:
        b1 = s['start'] / beats_per_bar + 1
        b2 = b1 + s['length'] / beats_per_bar
        print(f"  {s['name']:12}  bars {b1:.0f}–{b2:.0f}  ({s['length']:.0f} beats from beat {s['start']})")
    if auto_by_track:
        print(f"\nAutomation written for: {list(auto_by_track.keys())}")
    print(f"\nOutput: {output_als_path}")


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    build_arrangement(cfg, sys.argv[2], sys.argv[3])
