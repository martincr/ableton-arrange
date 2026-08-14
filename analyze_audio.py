"""
analyze_audio.py
-----------------
Propose a song-structure JSON (bpm, bar-aligned sections) from a WAV/MP3 file.

Usage:
    python3 analyze_audio.py song.wav
    python3 analyze_audio.py song.mp3 --sections 6 --time-signature 4 --write structure.json

Prints a table of candidate sections: bar range, length, mean energy, energy
trend, and a repetition "group" letter (sections sharing a letter look
structurally similar — e.g. two drops). Nothing is written to disk unless
--write is given, and even then the section names are neutral placeholders
("section_1", "section_2", ...) — this tool proposes bar-aligned boundaries
and groups repeats, it does not know which one is the "drop". Rename the
sections by ear before feeding the file to arrangement_tool.py.

Requires: numpy, scipy, scikit-learn, librosa<1.0.0, soundfile
    pip install numpy scipy scikit-learn "librosa<1.0.0" soundfile
(librosa 1.0.0+ requires Python >=3.12; pip resolves the compatible 0.10.x/0.11.x
line automatically on earlier interpreters.)

Limitations (read before trusting the output):
- Bar 1 / downbeat alignment is a simplifying assumption (first detected beat
  = beat 1 of bar 1), not a detected downbeat. There is no downbeat model
  here — nudge the result by hand if the grid is visibly off.
- Section *boundaries* and repetition *groups* are algorithmic. Section
  *names* are never inferred — this script does not claim to know what a
  "drop" sounds like.
- --sections needs hand-tuning: too low under-segments, too high fragments
  the track into slivers. Start around 6-8 and adjust.
- BPM detection can octave-error (report half or double the real tempo,
  common on electronic music) — use --bpm-hint if the reported tempo is
  obviously wrong.
- MP3 support depends on the installed soundfile/libsndfile build actually
  including MP3 decoding (libsndfile >= 1.1.0). No ffmpeg dependency either way.
- Segments shorter than 2 bars are folded into a neighbor rather than shown
  as their own section — sharp quiet-to-loud transitions otherwise tend to
  grab a stray bar into a spurious one-bar cluster.
"""

import argparse
import json
import os
import sys

import numpy as np
import librosa
from scipy.ndimage import median_filter as scipy_median_filter
from scipy.sparse.csgraph import laplacian as scipy_laplacian
from scipy.linalg import eigh as scipy_eigh
from sklearn.cluster import KMeans


# ── Beat / bar grid ─────────────────────────────────────────────────────────

def detect_beats(y, sr, bpm_hint):
    tempo, beat_frames = librosa.beat.beat_track(
        y=y, sr=sr, start_bpm=bpm_hint or 120.0, units='frames'
    )
    tempo = float(np.asarray(tempo).item())
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    return tempo, beat_frames, beat_times


def build_bar_grid(beat_frames, beat_times, beats_per_bar):
    """
    Group consecutive beats into bars. beat_frames[0] is treated as bar 1,
    beat 1 — there is no downbeat detection, so this is a simplifying
    assumption the caller should verify by ear.

    Returns a list of {"bar": int, "frame": int, "time": float} for each bar
    start (one entry per beats_per_bar-th beat).
    """
    bars = []
    for i in range(0, len(beat_frames), beats_per_bar):
        bars.append({
            'bar': len(bars) + 1,
            'frame': int(beat_frames[i]),
            'time': float(beat_times[i]),
        })
    return bars


# ── Structural segmentation ──────────────────────────────────────────────────
# Adapts librosa's own "Laplacian segmentation" example pipeline: CQT + MFCC
# -> combined affinity matrix -> normalized Laplacian eigendecomposition ->
# KMeans clustering. The reference example syncs features to individual
# beats; this syncs to *bars* instead, since a song section is a bar-level
# concept — beat-level clustering picks up sub-bar rhythmic contrast (e.g. a
# chord's attack vs. its decay within one bar) as spurious cluster changes,
# which shows up as single-beat label flicker with no smoothing window able
# to fix it (it's a real, periodic signal, not noise). Bar-synchronous
# features make that class of error structurally impossible, and as a bonus
# every cluster boundary is already bar-aligned — no snapping step needed.

def segment_bars(y, sr, bar_frames, n_sections):
    C = np.abs(librosa.cqt(y=y, sr=sr))
    Csync = librosa.util.sync(C, bar_frames, aggregate=np.median)

    # Repetition (recurrence) affinity from bar-synchronous CQT
    R = librosa.segment.recurrence_matrix(
        Csync, width=3, mode='affinity', sym=True
    )
    df = librosa.segment.timelag_filter(scipy_median_filter)
    Rf = df(R, size=(1, 7))

    # Local-path similarity from bar-synchronous MFCCs
    mfcc = librosa.feature.mfcc(y=y, sr=sr)
    Msync = librosa.util.sync(mfcc, bar_frames)
    path_distance = np.sum(np.diff(Msync, axis=1) ** 2, axis=0)
    sigma = np.median(path_distance)
    path_sim = np.exp(-path_distance / (sigma + 1e-12))
    R_path = np.diag(path_sim, k=1) + np.diag(path_sim, k=-1)

    deg_path = np.sum(R_path, axis=1)
    deg_rec = np.sum(Rf, axis=1)
    mu = deg_path.dot(deg_path + deg_rec) / np.sum((deg_path + deg_rec) ** 2)
    A = mu * Rf + (1 - mu) * R_path

    L = scipy_laplacian(A, normed=True)
    _, evecs = scipy_eigh(L)

    evecs = scipy_median_filter(evecs, size=(9, 1))
    Cnorm = np.cumsum(evecs ** 2, axis=1) ** 0.5
    # Use a fixed number of leading eigenvectors as the clustering embedding.
    k_embed = min(n_sections, evecs.shape[1])
    X = evecs[:, :k_embed] / (Cnorm[:, k_embed - 1:k_embed] + 1e-12)

    labels = KMeans(n_clusters=n_sections, n_init=10, random_state=0).fit_predict(X)
    return labels  # one label per bar


def labels_to_bar_boundaries(labels, total_bars):
    """
    Turn a per-bar cluster label array into contiguous segments.

    labels[i] is the cluster for bar (i+1); since features were bar-synced,
    every run boundary is already bar-aligned, so this is just a run-length
    grouping — no snapping needed. labels may have one extra trailing entry
    from librosa.util.sync's boundary padding, which is dropped.
    """
    labels = labels[:total_bars]
    segments = []
    seg_start = 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[seg_start]:
            segments.append({
                'bar_start': seg_start + 1,
                'bar_end': i + 1,
                'group': int(labels[seg_start]),
            })
            seg_start = i
    return merge_short_segments(segments)


def merge_short_segments(segments, min_bars=2):
    """
    Fold any segment shorter than min_bars into a neighbor.

    A cluster boundary right at a sharp transition (e.g. quiet-to-loud) often
    grabs one bar of "blended" content into its own tiny cluster. These
    slivers are noise, not sections, so merge each into whichever neighbor
    it's adjacent to (preferring the following segment, falling back to the
    previous one at the end of the list) rather than surfacing it as a
    one-bar "section." Runs to a fixed point since a merge can shrink a
    neighbor below the threshold too.
    """
    segments = [dict(s) for s in segments]
    changed = True
    while changed and len(segments) > 1:
        changed = False
        for i, seg in enumerate(segments):
            if seg['bar_end'] - seg['bar_start'] >= min_bars:
                continue
            if i + 1 < len(segments):
                segments[i + 1]['bar_start'] = seg['bar_start']
            else:
                segments[i - 1]['bar_end'] = seg['bar_end']
            del segments[i]
            changed = True
            break
    return segments


# ── Per-section report stats ─────────────────────────────────────────────────

def annotate_energy(segments, y, sr, bars, beats_per_bar):
    rms = librosa.feature.rms(y=y)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=512)
    rms_norm = rms / (rms.max() + 1e-12)

    bar_time = {b['bar']: b['time'] for b in bars}
    total_bars = bars[-1]['bar'] if bars else 1

    for seg in segments:
        t0 = bar_time.get(seg['bar_start'], 0.0)
        t1 = bar_time.get(seg['bar_end'], rms_times[-1] if len(rms_times) else t0)
        mask = (rms_times >= t0) & (rms_times < t1)
        vals = rms_norm[mask]
        if len(vals) == 0:
            seg['energy'] = 0.0
            seg['trend'] = 'flat'
            continue
        seg['energy'] = round(float(vals.mean()), 2)
        if len(vals) >= 4:
            slope = np.polyfit(np.arange(len(vals)), vals, 1)[0]
        else:
            slope = 0.0
        seg['trend'] = 'rising' if slope > 0.002 else 'falling' if slope < -0.002 else 'flat'
    return segments


# ── Reporting ────────────────────────────────────────────────────────────────

_GROUP_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

def group_letter(group_id):
    return _GROUP_LETTERS[group_id % len(_GROUP_LETTERS)]

def print_report(tempo, segments, beats_per_bar):
    print(f"Detected tempo: {tempo:.1f} BPM  (bar 1 = first detected beat — verify by ear)")
    print(f"Time signature assumed: {beats_per_bar}/4\n")
    print(f"{'#':<4} {'Bars':<12} {'Length':<9} {'Energy':<8} {'Trend':<9} {'Group'}")
    print('-' * 55)
    for i, seg in enumerate(segments, 1):
        length = seg['bar_end'] - seg['bar_start']
        bars_str = f"{seg['bar_start']}–{seg['bar_end']}"
        print(f"{i:<4} {bars_str:<12} {length:<9} {seg['energy']:<8} {seg['trend']:<9} {group_letter(seg['group'])}")
    print()
    print("Sections sharing a Group letter look structurally similar (likely repeats,")
    print("e.g. two drops). Nothing has been written — use --write to also emit a")
    print("structure.json skeleton with placeholder names for you to rename by ear.")


# ── Main ─────────────────────────────────────────────────────────────────────

def analyze(path, n_sections, beats_per_bar, bpm_hint):
    if not os.path.exists(path):
        raise FileNotFoundError(f"No such file: {path}")
    y, sr = librosa.load(path)
    tempo, beat_frames, beat_times = detect_beats(y, sr, bpm_hint)
    bars = build_bar_grid(beat_frames, beat_times, beats_per_bar)

    if len(bars) < n_sections:
        raise ValueError(
            f"Only {len(bars)} bars detected — fewer than --sections {n_sections}. "
            f"Pass a smaller --sections or check --bpm-hint."
        )

    bar_frames = np.array([b['frame'] for b in bars])
    labels = segment_bars(y, sr, bar_frames, n_sections)
    segments = labels_to_bar_boundaries(labels, len(bars))
    segments = annotate_energy(segments, y, sr, bars, beats_per_bar)
    return tempo, segments


def write_structure_json(path, tempo, segments, beats_per_bar):
    cfg = {
        'bpm': round(tempo, 1),
        'time_signature': [beats_per_bar, 4],
        'structure': [
            {'section': f'section_{i}', 'bars': seg['bar_end'] - seg['bar_start']}
            for i, seg in enumerate(segments, 1)
        ],
    }
    with open(path, 'w') as f:
        json.dump(cfg, f, indent=2)
    print(f"\nWrote {path} — rename the placeholder section names before running arrangement_tool.py.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Propose a bar-aligned song structure from an audio file.',
        epilog='This proposes boundaries and repetition groups; it does not name sections.')
    parser.add_argument('path', help='Audio file (WAV or MP3)')
    parser.add_argument('--sections', type=int, default=6,
                        help='Target number of structural segments (default: 6)')
    parser.add_argument('--time-signature', type=int, default=4, dest='beats_per_bar',
                        help='Beats per bar (default: 4)')
    parser.add_argument('--bpm-hint', type=float, default=None,
                        help='Prior tempo estimate, for when auto-detection octave-errors')
    parser.add_argument('--write', metavar='PATH', default=None,
                        help='Also write a structure.json skeleton with placeholder names')
    args = parser.parse_args()

    try:
        tempo, segments = analyze(args.path, args.sections, args.beats_per_bar, args.bpm_hint)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print_report(tempo, segments, args.beats_per_bar)

    if args.write:
        write_structure_json(args.write, tempo, segments, args.beats_per_bar)
