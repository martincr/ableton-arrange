"""
arrangement_tool.py
-------------------
Build an Ableton arrangement from a JSON song structure.

Usage:
    python3 arrangement_tool.py structure.json base.als output.als [--backup]
    python3 arrangement_tool.py --xml base.als                    # dump XML for inspection
    python3 arrangement_tool.py --inspect base.als [track]        # structured track/parameter summary

The base .als must contain at least one clip somewhere (session or arrangement)
on each MIDI track.

JSON format:
{
  "bpm": 130,
  "time_signature": [4, 4],
  "track_height": 68,
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
  ],
  "track_segments": {
    "2-DS Kick": [[1, 49], [65, 97]]
  },
  "send_throws": [
    {
      "send_index": 0,
      "points": [
        {"bar": 49, "value": 0.9},
        {"bar": 51, "value": 0.05}
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

parameter_pointee: the ModulationTarget/Pointee Id of the parameter.
  Use --xml or --inspect to find these IDs.
values: normalised 0.0–1.0.
bars: 1-indexed from start of song.
track_height: arrangement lane height in pixels (17–425, default 68).
track_segments: optional. Tracks not listed get one clip spanning the whole
  arrangement (default behaviour). Listed tracks instead get one looping clip
  per [start_bar, end_bar) range, so they can drop in/out of the song —
  end_bar is exclusive, e.g. [[1, 49], [65, 97]] covers bars 1-48 and 65-96.
send_throws: optional. Applies the same automation points to the Nth Send
  (0-indexed) on every track that has one, without listing a parameter_pointee
  per track — e.g. a reverb "dub throw" spike across the whole song at once.
track_notes: optional. Replaces the note content of a track's source clip
  before it is placed, so you can write patterns from JSON instead of playing
  them in. pitch is a MIDI number (0-127) or a name (C3 = 60, e.g. "F#2",
  "Bb4"); start/duration are in beats from the clip start; velocity defaults
  to 100 and off_velocity to 64. loop_bars resizes the loop region so the new
  pattern repeats at that length — set it whenever the pattern you write is a
  different length from the source clip, or the placed clips will loop at the
  old length.
"""

import gzip, re, json, sys, copy, argparse, os, shutil
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

def set_bpm(root, bpm):
    """Set BPM on the XML tree. Handles Live 12 (MainTrack), v10+ (MasterTrack), and legacy."""
    # Live 12: MainTrack > DeviceChain > Mixer > Tempo > Manual
    # Live 10/11: MasterTrack > DeviceChain > Mixer > Tempo > Manual
    for track_tag in ('MainTrack', 'MasterTrack'):
        manual = root.find(f'.//{track_tag}/DeviceChain/Mixer/Tempo/Manual')
        if manual is not None:
            manual.set('Value', str(bpm))
            return
    # Legacy (pre-v10): BPM stored as a FloatEvent under Tempo > ArrangerAutomation
    for track_tag in ('MainTrack', 'MasterTrack'):
        float_event = root.find(
            f'.//{track_tag}/DeviceChain/Mixer/Tempo/ArrangerAutomation/Events/FloatEvent')
        if float_event is not None:
            float_event.set('Value', str(bpm))
            return

def increment_overwrite_protection(xml_str):
    m = re.search(r'OverwriteProtectionNumber Value="(\d+)"', xml_str)
    if m:
        new_val = int(m.group(1)) + 1
        return re.sub(r'(<OverwriteProtectionNumber Value=")[^"]*(")',
                      rf'\g<1>{new_val}\2', xml_str, count=1)
    return xml_str

def set_track_heights(tracks, height):
    """Set arrangement LaneHeight on all MIDI tracks (clamped 17–425)."""
    height = max(17, min(425, height))
    for t in tracks:
        if t.tag != 'MidiTrack':
            continue
        lh = t.find('LaneHeight')
        if lh is not None:
            lh.set('Value', str(height))

def find_any_clip(track_el):
    """Find any MidiClip in the track — session or arrangement."""
    # Try session slots first
    for clip in track_el.findall('.//MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/MidiClip'):
        return clip
    # Fall back to arrangement
    for clip in track_el.findall('.//ClipTimeable/ArrangerAutomation/Events/MidiClip'):
        return clip
    return None

def get_send_pointee(track_el, send_index):
    """Return the AutomationTarget Id of the track's Nth Send, or None."""
    holders = track_el.findall('.//Sends/TrackSendHolder')
    if send_index >= len(holders):
        return None
    at = holders[send_index].find('Send/AutomationTarget')
    return int(at.get('Id')) if at is not None else None


# ── Note writer ───────────────────────────────────────────────────────────────

_SEMITONES = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}

def parse_pitch(pitch):
    """
    Accept a MIDI note number (0-127) or a note name and return the number.

    Names follow Ableton's convention where C3 = 60: 'C3', 'F#2', 'Bb4', 'Eb-1'.
    """
    if isinstance(pitch, int):
        value = pitch
    else:
        m = re.fullmatch(r'\s*([A-Ga-g])([#b]?)(-?\d+)\s*', str(pitch))
        if not m:
            raise ValueError(f"Unrecognised pitch {pitch!r} — use 0-127 or a name like 'C3'")
        letter, accidental, octave = m.group(1).upper(), m.group(2), int(m.group(3))
        value = (octave + 2) * 12 + _SEMITONES[letter]
        if accidental == '#':
            value += 1
        elif accidental == 'b':
            value -= 1
    if not 0 <= value <= 127:
        raise ValueError(f"Pitch {pitch!r} resolves to {value}, outside the MIDI range 0-127")
    return value

def set_clip_notes(clip_el, notes, start_id, beats_per_bar, loop_bars=None):
    """
    Replace a MidiClip's note content.

    notes: list of {"pitch", "start", "duration", "velocity", "off_velocity"} —
           `start`/`duration` in beats relative to the clip start.
    loop_bars: if given, resize the clip's loop region so the new pattern repeats
           at that length instead of the source clip's original length.

    Returns the next free id (KeyTrack ids are drawn from the same counter as
    clip ids, so callers keep NextPointeeId in sync).
    """
    notes_el = clip_el.find('Notes')
    if notes_el is None:
        raise ValueError("Clip has no <Notes> element to write into")
    keytracks_el = notes_el.find('KeyTracks')
    if keytracks_el is None:
        keytracks_el = ET.SubElement(notes_el, 'KeyTracks')

    # Group by pitch — Ableton stores one KeyTrack per distinct note.
    by_pitch = {}
    for n in notes:
        by_pitch.setdefault(parse_pitch(n['pitch']), []).append(n)

    for child in list(keytracks_el):
        keytracks_el.remove(child)

    note_id = 1
    for pitch in sorted(by_pitch):
        kt = ET.SubElement(keytracks_el, 'KeyTrack', Id=str(start_id))
        start_id += 1
        kt_notes = ET.SubElement(kt, 'Notes')
        for n in sorted(by_pitch[pitch], key=lambda x: float(x['start'])):
            velocity = n.get('velocity', 100)
            ET.SubElement(kt_notes, 'MidiNoteEvent',
                          Time=str(float(n['start'])),
                          Duration=str(float(n.get('duration', 1.0))),
                          Velocity=str(velocity),
                          OffVelocity=str(n.get('off_velocity', 64)),
                          NoteId=str(note_id))
            note_id += 1
        # MidiKey follows Notes — Ableton relies on this child order.
        ET.SubElement(kt, 'MidiKey', Value=str(pitch))

    gen = notes_el.find('NoteIdGenerator/NextId')
    if gen is not None:
        gen.set('Value', str(note_id))

    if loop_bars is not None:
        length = loop_bars * beats_per_bar
        loop = clip_el.find('Loop')
        for tag in ('LoopStart', 'HiddenLoopStart'):
            el = loop.find(tag)
            if el is not None:
                el.set('Value', '0')
        for tag in ('LoopEnd', 'HiddenLoopEnd', 'OutMarker'):
            el = loop.find(tag)
            if el is not None:
                el.set('Value', str(length))
        clip_el.find('CurrentStart').set('Value', '0')
        clip_el.find('CurrentEnd').set('Value', str(length))

    return start_id


# ── Inspection ───────────────────────────────────────────────────────────────

def get_bpm_value(root):
    tempo = root.find('.//Tempo/Manual')
    return float(tempo.get('Value', 120)) if tempo is not None else 120.0

def get_session_clips(track_el):
    """Summarise session-view clips (name, bars) on a track."""
    clips = []
    for tag, ttype in (('MidiClip', 'MIDI'), ('AudioClip', 'audio')):
        for clip_el in track_el.findall(f'.//MainSequencer/ClipSlotList//{tag}'):
            name_el = clip_el.find('Name')
            name = name_el.get('Value', '') if name_el is not None else ''
            loop = clip_el.find('Loop')
            if loop is not None:
                length_beats = (float(loop.find('LoopEnd').get('Value'))
                                 - float(loop.find('LoopStart').get('Value')))
            else:
                length_beats = 4.0
            clips.append({'name': name or f'{ttype} clip', 'bars': length_beats / 4.0})
    return clips

def get_automation_params(track_el):
    """Find automatable parameters: elements with a direct AutomationTarget child."""
    parent_map = {child: parent for parent in track_el.iter() for child in parent}
    params = []
    for param_el in track_el.findall('.//*[AutomationTarget]'):
        at = param_el.find('AutomationTarget')
        target_id = at.get('Id') if at is not None else None
        if not target_id:
            continue
        manual_el = param_el.find('Manual')
        range_el = param_el.find('MidiControllerRange')
        if range_el is None:
            range_el = param_el
        min_el = range_el.find('Min')
        if min_el is None:
            min_el = range_el.find('MinValue')
        max_el = range_el.find('Max')
        if max_el is None:
            max_el = range_el.find('MaxValue')

        device_el = parent_map.get(param_el)
        device_name = device_el.tag if device_el is not None else None
        if device_el is not None:
            username_el = device_el.find('UserName')
            if username_el is not None and username_el.get('Value'):
                device_name = username_el.get('Value')

        params.append({
            'device': device_name,
            'parameter': param_el.tag,
            'target_id': target_id,
            'manual': manual_el.get('Value') if manual_el is not None else None,
            'min': min_el.get('Value') if min_el is not None else None,
            'max': max_el.get('Value') if max_el is not None else None,
        })
    return params

def get_send_info(track_el):
    sends = []
    for i, holder in enumerate(track_el.findall('.//Sends/TrackSendHolder')):
        send_el = holder.find('Send')
        if send_el is None:
            continue
        at = send_el.find('AutomationTarget')
        manual_el = send_el.find('Manual')
        sends.append({
            'send_index': i,
            'target_id': at.get('Id') if at is not None else None,
            'manual': manual_el.get('Value') if manual_el is not None else None,
        })
    return sends

def print_inspect_report(root, track_filter=None):
    tracks = root.find('LiveSet/Tracks')

    if track_filter:
        target = next((t for t in tracks
                       if t.find('.//EffectiveName') is not None
                       and t.find('.//EffectiveName').get('Value') == track_filter), None)
        if target is None:
            print(f"Track '{track_filter}' not found.")
            return

        print(f"Track: {track_filter}\n")
        params = get_automation_params(target)
        if params:
            for p in params:
                range_str = f" range=[{p['min']}, {p['max']}]" if p['min'] and p['max'] else ''
                dev = f"{p['device']} / " if p['device'] else ''
                print(f"  {dev}{p['parameter']}: target={p['target_id']} manual={p['manual']}{range_str}")
        else:
            print("  No automatable parameters found.")

        sends = get_send_info(target)
        if sends:
            print()
            for s in sends:
                print(f"  Send {s['send_index']}: target={s['target_id']} manual={s['manual']}")
        return

    bpm = get_bpm_value(root)
    print(f"BPM: {bpm}\n")
    print(f"{'Track':<20} {'Clip':<25} {'Bars'}")
    print('-' * 55)
    for t in tracks:
        if t.tag not in ('MidiTrack', 'AudioTrack', 'ReturnTrack', 'GroupTrack'):
            continue
        name_el = t.find('.//EffectiveName')
        name = name_el.get('Value', 'Unknown') if name_el is not None else 'Unknown'
        ttype = {'MidiTrack': 'MIDI', 'AudioTrack': 'audio'}.get(t.tag, t.tag.replace('Track', ''))
        clips = get_session_clips(t)
        arranged = bool(t.findall('.//ClipTimeable/ArrangerAutomation/Events/MidiClip')
                         or t.findall('.//ClipTimeable/ArrangerAutomation/Events/AudioClip'))

        if clips:
            for i, c in enumerate(clips):
                print(f"{(name if i == 0 else ''):<20} {c['name'] + ' ' + ttype:<25} {c['bars']:.1f} bars")
        else:
            print(f"{name:<20} {'(no session clips)':<25}")
        if arranged:
            print(f"{'':<20} (arrangement already populated)")


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

    # Clip extent — stretch to cover the full arrangement
    clip.find('CurrentStart').set('Value', '0')
    clip.find('CurrentEnd').set('Value', str(length_beats))

    # Loop region — preserve the source loop size so the pattern repeats.
    # Only CurrentEnd is extended; LoopEnd/OutMarker stay at the original
    # clip length so Ableton loops the note content rather than playing silence.
    loop = clip.find('Loop')
    loop_end = float(loop.find('LoopEnd').get('Value'))
    loop.find('LoopStart').set('Value', '0')
    loop.find('OutMarker').set('Value', str(loop_end))
    loop.find('HiddenLoopStart').set('Value', '0')
    loop.find('StartRelative').set('Value', '0')
    loop.find('LoopOn').set('Value', 'true')

    # Fix clip editor scroll state to match clip length
    scroller = clip.find('ScrollerTimePreserver')
    if scroller is not None:
        rt = scroller.find('RightTime')
        if rt is not None:
            rt.set('Value', str(length_beats))

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


# ── Locator builder ───────────────────────────────────────────────────────────

def build_locators(sections_info):
    """Return a <Locators> element containing one locator per section start."""
    outer = ET.Element('Locators')
    inner = ET.SubElement(outer, 'Locators')
    for i, sec in enumerate(sections_info):
        loc = ET.SubElement(inner, 'Locator', Id=str(i))
        ET.SubElement(loc, 'LomId', Value='0')
        ET.SubElement(loc, 'Time', Value=str(sec['start']))
        ET.SubElement(loc, 'Name', Value=sec['name'])
        ET.SubElement(loc, 'Annotation', Value='')
        ET.SubElement(loc, 'IsSongStart', Value='false')
    return outer


# ── Main ───────────────────────────────────────────────────────────────────────

def build_arrangement(cfg, base_als_path, output_als_path, backup=False):
    bpm            = cfg.get('bpm', 120)
    beats_per_bar  = cfg.get('time_signature', [4, 4])[0]
    track_height   = cfg.get('track_height', 68)
    sections       = cfg['structure']
    auto_specs     = cfg.get('automations', [])
    track_segments = cfg.get('track_segments', {})
    send_throws    = cfg.get('send_throws', [])
    track_notes    = cfg.get('track_notes', {})

    with gzip.open(base_als_path, 'rb') as f:
        als_xml = f.read().decode('utf-8')

    root   = ET.fromstring(als_xml)
    tracks = root.find('LiveSet/Tracks')

    # Index tracks by name
    track_by_name = {t.find('.//EffectiveName').get('Value'): t for t in tracks}

    # Verify at least one MIDI track has a clip
    t0 = next(t for t in tracks if t.tag == 'MidiTrack')
    if find_any_clip(t0) is None:
        raise ValueError("No MIDI clip found in the file to use as source template")

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
    notes_written = set()

    # Place clips on every MIDI track
    for t in tracks:
        if t.tag != 'MidiTrack':
            continue
        tname     = t.find('.//EffectiveName').get('Value')
        events_el = t.find('.//ClipTimeable/ArrangerAutomation/Events')
        if events_el is None:
            continue

        source_clip = find_any_clip(t)
        if source_clip is None:
            continue

        # Note-writing: rewrite the source clip's contents before it gets cloned
        # into the arrangement, so every placed copy carries the new pattern.
        # The source clip is edited in place, so the session view shows it too.
        note_spec = track_notes.get(tname)
        if note_spec is not None:
            if 'notes' not in note_spec:
                raise ValueError(f"track_notes['{tname}'] has no 'notes' list")
            notes_written.add(tname)
            clip_id = set_clip_notes(
                source_clip,
                note_spec['notes'],
                start_id=clip_id,
                beats_per_bar=beats_per_bar,
                loop_bars=note_spec.get('loop_bars'),
            )

        # Clear existing arrangement clips
        for child in list(events_el):
            events_el.remove(child)

        track_automations = dict(auto_by_track.get(tname, {}))

        # Send-throw convenience: same points on this track's Nth Send, for every
        # track that has one.
        for spec in send_throws:
            pointee = get_send_pointee(t, spec['send_index'])
            if pointee is None:
                continue
            track_automations[pointee] = [
                {'beat': (pt['bar'] - 1) * beats_per_bar, 'value': pt['value']}
                for pt in spec['points']
            ]

        segments = track_segments.get(tname)
        if segments:
            # One looping clip per [start_bar, end_bar) range, so the track can
            # drop in/out of the arrangement instead of playing the whole song.
            for start_bar, end_bar in segments:
                seg_start = (start_bar - 1) * beats_per_bar
                seg_end   = (end_bar - 1) * beats_per_bar
                seg_automations = {}
                for pointee, pts in track_automations.items():
                    seg_pts = [
                        {'beat': pt['beat'] - seg_start, 'value': pt['value']}
                        for pt in pts if seg_start <= pt['beat'] < seg_end
                    ]
                    if seg_pts:
                        seg_automations[pointee] = seg_pts

                arr_clip = make_arrangement_clip(
                    source_clip,
                    start_beat=seg_start,
                    length_beats=seg_end - seg_start,
                    clip_id=clip_id,
                    automations=seg_automations or None,
                )
                clip_id += 1
                events_el.append(arr_clip)
        else:
            # Single clip spanning full arrangement
            arr_clip = make_arrangement_clip(
                source_clip,
                start_beat=0,
                length_beats=total_beats,
                clip_id=clip_id,
                automations=track_automations or None,
            )
            clip_id += 1
            events_el.append(arr_clip)
        print(f"  {tname}: {len(source_clip.findall('.//MidiNoteEvent'))} notes")

    # Write locators
    locators_el = root.find('LiveSet/Locators')
    if locators_el is not None:
        locators_el.getparent() if hasattr(locators_el, 'getparent') else None
        new_locators = build_locators(sections_info)
        live_set = root.find('LiveSet')
        idx = list(live_set).index(locators_el)
        live_set.remove(locators_el)
        live_set.insert(idx, new_locators)

    # Track heights and BPM (tree operations before serialisation)
    set_track_heights(tracks, track_height)
    set_bpm(root, bpm)

    # Write out
    new_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode')
    new_xml = set_next_id(new_xml, clip_id)
    new_xml = increment_overwrite_protection(new_xml)

    if backup and os.path.exists(output_als_path):
        backup_path = output_als_path + '.backup'
        shutil.copy2(output_als_path, backup_path)
        print(f"Backup written: {backup_path}")

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
    if track_segments:
        print(f"Segmented tracks: {list(track_segments.keys())}")
    if send_throws:
        print(f"Send-throw automation on send index(es): {[s['send_index'] for s in send_throws]}")
    if track_notes:
        print(f"Notes written for: {sorted(notes_written)}")
        unmatched = sorted(set(track_notes) - notes_written)
        if unmatched:
            print(f"  WARNING: no matching track with a clip for: {unmatched}")
    print(f"\nOutput: {output_als_path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Build an Ableton arrangement from a JSON structure file.',
        epilog='Use --xml or --inspect to find parameter_pointee IDs.')
    parser.add_argument('--xml', metavar='ALS',
                        help='Dump uncompressed XML from ALS to <name>.xml and exit')
    parser.add_argument('--inspect', nargs='+', metavar=('ALS', 'TRACK'),
                        help='Print a track/clip/automation-parameter summary and exit; '
                             'optionally filter to one TRACK')
    parser.add_argument('--backup', action='store_true',
                        help='Back up an existing output file to <output>.backup before overwriting')
    parser.add_argument('structure', nargs='?', help='JSON structure file')
    parser.add_argument('base',      nargs='?', help='Base .als project file')
    parser.add_argument('output',    nargs='?', help='Output .als path')
    args = parser.parse_args()

    if args.xml:
        with gzip.open(args.xml, 'rb') as f:
            xml_bytes = f.read()
        out_path = re.sub(r'\.als$', '', args.xml, flags=re.IGNORECASE) + '.xml'
        with open(out_path, 'wb') as f:
            f.write(xml_bytes)
        print(f"XML written to {out_path}")
        sys.exit(0)

    if args.inspect:
        als_path = args.inspect[0]
        track_filter = args.inspect[1] if len(args.inspect) > 1 else None
        with gzip.open(als_path, 'rb') as f:
            inspect_root = ET.fromstring(f.read().decode('utf-8'))
        print_inspect_report(inspect_root, track_filter)
        sys.exit(0)

    if not all([args.structure, args.base, args.output]):
        parser.print_help()
        sys.exit(1)

    with open(args.structure) as f:
        cfg = json.load(f)
    build_arrangement(cfg, args.base, args.output, backup=args.backup)
