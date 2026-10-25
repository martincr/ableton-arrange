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
- The bar grid assumes a fixed tempo: one steady grid is fitted to the whole
  track, and the groove entry (first sustained rise in low-end energy and
  onsets) is taken as a downbeat, with bars counted back from it. That suits
  electronic music; for anything played live, or if the entry is really a
  pickup (e.g. the groove comes in on beat 3), use --no-anchor to get the
  beat tracker's beats with bar 1 = first detected beat. The report says
  where it anchored — check that bar line by ear.
- Section *boundaries* and repetition *groups* are algorithmic. Section
  *names* are never inferred — this script does not claim to know what a
  "drop" sounds like.
- Boundaries inside slowly evolving passages are fragile: nudging the bar
  grid by a frame can move or remove them. Each boundary is re-checked
  against 12 such re-runs and shown as "firm" or "moves (n/12)" — treat the
  ones that move as approximate.
- --sections is the number of distinct section *types* (repetition groups),
  not the number of rows: types recur, so 6 types can produce 15-20 rows on a
  long track. Too low merges different material; too high fragments the
  track into slivers. Start around 4-8 and adjust.
- The beat tracker can lock onto the wrong tempo — not only half/double, but
  e.g. 4/5 of the real tempo on delay-heavy dub techno, where an echo pattern
  can out-score the kick. When the tracked tempo disagrees with an
  independent measurement, the measured tempo is used automatically if a
  steady grid at it fits the onsets better (the report says so). Half/double
  disagreements only print a WARNING: a half-tempo grid can fit better than
  the true one, so the fit can't settle them. --bpm-hint pins the tempo, and
  is itself checked against the audio (a hint copied from a BPM website can
  be off). Tempi are refined to ~0.01 BPM by fitting steady
  grids to the onsets, so they aren't rounded to librosa's coarse lag steps.
  Check by ear either way — the warnings can't tell which value is right.
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


# ── Tempo ────────────────────────────────────────────────────────────────────

def detect_beats(y, sr, bpm_hint):
    # trim=False keeps the grid running through soft or beatless passages at
    # either end — with the default trim=True a quiet intro is dropped from the
    # grid entirely (the first 1:47 of a 10-minute track, in testing). A hint
    # is passed as bpm= to pin the tempo: start_bpm is only a prior, and the
    # tracker can ignore it and lock onto a wrong tempo regardless.
    kwargs = {'bpm': bpm_hint} if bpm_hint else {}
    tempo, beat_frames = librosa.beat.beat_track(
        y=y, sr=sr, trim=False, units='frames', **kwargs
    )
    tempo = float(np.asarray(tempo).item())
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    return tempo, beat_frames, beat_times


COMB_HOP = 128           # ~5.8 ms frames, fine enough to resolve tempo between lag bins
TEMPO_TOLERANCE = 0.04   # relative; small tracking errors (~2%) don't warrant a warning
HINT_TOLERANCE = 0.001   # a pinned tempo 0.1% off drifts about a beat over a 10-minute track


def comb_envelope(y, sr):
    return librosa.onset.onset_strength(y=y, sr=sr, hop_length=COMB_HOP), sr / COMB_HOP


def comb_fit(env, fps, bpms):
    """
    Best constant-tempo grid among candidate BPMs, as (bpm, phase_seconds).

    For each candidate, slides a fixed grid across the onset envelope in
    half-frame phase steps and scores the mean onset strength on the grid. A
    wrong tempo drifts out of phase within a few bars, so over a whole track
    the true one stands out sharply. librosa's own estimates snap to whole
    frame lags — near 120 BPM the only values available are 117.5, 123.0 and
    129.2 — which is how a track at exactly 122.00 came out as "123".
    """
    (score, phase), bpm = max(((grid_fit(env, fps, b), b) for b in bpms), key=lambda f: f[0][0])
    if score < 0:
        raise ValueError("Audio too short to fit a tempo")
    return float(bpm), phase


def grid_fit(env, fps, bpm):
    """Best phase for a steady grid at `bpm`, as (score, phase_seconds); score is mean onset strength on the grid."""
    period = 60.0 / bpm * fps
    n = int((len(env) - 1 - period) // period)
    if n < 2:
        return -1.0, 0.0
    phases = np.arange(0, period, 0.5)
    idx = np.round(phases[:, None] + period * np.arange(n)[None, :]).astype(int)
    scores = env[idx].mean(axis=1)
    return float(scores.max()), phases[scores.argmax()] / fps


def refine_tempo(env, fps, bpm, span=0.05):
    """The steadiest tempo within ±span of `bpm`, to 0.005 BPM."""
    coarse, _ = comb_fit(env, fps, np.arange(bpm * (1 - span), bpm * (1 + span), 0.05))
    fine, _ = comb_fit(env, fps, np.arange(coarse - 0.06, coarse + 0.06, 0.005))
    return fine


def check_tempo(y, sr, env, fps, tracked):
    """
    Cross-check the tracked tempo against an independent estimate.

    Returns the independent estimate if it disagrees with `tracked` by more
    than TEMPO_TOLERANCE, else None. beat_track builds its onset envelope with
    median aggregation, which on reverb-heavy material can favour an echo
    pattern over the kick (99.4 vs a real 122 BPM, in testing). This uses the
    default mean-aggregated envelope instead — an estimate from the median
    envelope just repeats the tracker's mistake — refined with comb_fit so the
    suggested value isn't stuck on a lag bin.
    """
    oenv = librosa.onset.onset_strength(y=y, sr=sr)
    pulse = refine_tempo(env, fps, float(librosa.feature.tempo(onset_envelope=oenv, sr=sr)[0]))
    return None if abs(tracked / pulse - 1) <= TEMPO_TOLERANCE else pulse


def octave_relation(tracked, measured):
    """'double' or 'half' if `measured` is ~2x or ~0.5x `tracked`, else ''."""
    ratio = measured / tracked
    if abs(ratio / 2 - 1) <= TEMPO_TOLERANCE:
        return 'double'
    if abs(ratio * 2 - 1) <= TEMPO_TOLERANCE:
        return 'half'
    return ''


def resolve_tempo(env, fps, tracked, measured):
    """
    Choose between the tracker's tempo and the independent measurement when
    they disagree. Returns (tempo, report line).

    Switches to `measured` when its steady grid fits the onsets better than
    `tracked`'s. Half/double disagreements only warn: a grid at half the true
    tempo lands on just the strongest hits and can fit *better* (61 BPM
    outscored the real 122 on "Transport"), so the fit can't settle them.
    """
    if octave_relation(tracked, measured) or grid_fit(env, fps, measured)[0] <= grid_fit(env, fps, tracked)[0]:
        return tracked, tempo_warning(tracked, measured)
    return measured, (f"NOTE: the beat tracker locked onto {tracked:.2f} BPM, but a steady "
                      f"{measured:.2f} BPM grid fits the onsets better, so {measured:.2f} is used. "
                      f"To keep the tracker's tempo, re-run with --bpm-hint {round(tracked, 2):g}.")


def check_hint(env, fps, hint):
    """The nearest steady tempo if a pinned --bpm-hint drifts against the audio, else None."""
    nearest = refine_tempo(env, fps, hint)
    return None if abs(nearest / hint - 1) <= HINT_TOLERANCE else nearest


# ── Bar grid ─────────────────────────────────────────────────────────────────

def find_groove_entry(y, sr, beat_times, win=16):
    """
    Index into beat_times of the beat where the groove first comes in, or None.

    "Groove" is low-end energy (30-120 Hz, where kick and bass sit) plus onset
    strength, each normalised over the track and averaged per beat. The entry
    is the first sustained rise at least half the size of the track's biggest:
    compare the median of the `win` beats after each beat with the `win`
    before, take the first beat whose rise reaches half the largest, then
    localise to the first two beats in a row above the midpoint of the levels
    either side. (The median comparison alone is flat-topped across a
    transition, so its peak lands early.) Low-end energy on its own isn't
    enough: a dub bass pulsing through a beatless intro reaches the kick's
    level on alternate beats.
    """
    S = np.abs(librosa.stft(y)) ** 2
    freqs = librosa.fft_frequencies(sr=sr)
    low_db = 10 * np.log10(S[(freqs >= 30) & (freqs <= 120)].sum(axis=0) + 1e-10)
    oenv = librosa.onset.onset_strength(y=y, sr=sr)
    n = min(len(low_db), len(oenv))
    t = librosa.frames_to_time(np.arange(n), sr=sr)
    starts = np.searchsorted(t, beat_times)
    ends = np.append(starts[1:], n)

    def per_beat(curve):
        return np.array([curve[a:b].mean() if b > a else curve[min(a, n - 1)]
                         for a, b in zip(starts, ends)])

    def z(v):
        return (v - v.mean()) / (v.std() + 1e-12)

    inten = z(per_beat(low_db[:n])) + z(per_beat(oenv[:n]))
    if len(inten) < 3 * win:
        return None
    step = np.full(len(inten), -np.inf)
    for i in range(win, len(inten) - win):
        step[i] = np.median(inten[i:i + win]) - np.median(inten[i - win:i])
    largest = step.max()
    if largest < 1.0:   # no step worth calling an entry
        return None
    first = int(np.argmax(step >= 0.5 * largest))
    c = first + int(np.argmax(step[first:first + win]))
    mid = (np.median(inten[max(0, c - 24):c - 8]) + np.median(inten[c + 8:c + 24])) / 2
    for i in range(max(1, c - 8), min(len(inten) - 1, c + 9)):
        if inten[i] > mid and inten[i + 1] > mid:
            return i
    return c


def anchored_bar_grid(y, sr, bpm, phase, beats_per_bar):
    """
    Constant-tempo bar grid with the bar lines anchored on the groove entry.

    A beat tracker's beats drift wherever there's nothing to follow: through
    a beatless intro it fitted 63 bars at 122.2 BPM in front of a kick that
    runs at a steady 122.00. Electronic music sits on one fixed grid, so this
    lays a constant grid (tempo and phase from comb_fit) over the whole track
    and makes the groove entry a downbeat. Bars are counted back from the
    entry to the start of the audio: audio before the first full bar becomes
    a pickup bar 1 if it holds at least one beat, and is left off the grid if
    not. With no clear entry, the first beat on the grid is the downbeat.

    Returns (bars, info): bars as in build_bar_grid (bar 1 may start before
    the audio, at a negative time), and info with the beat period, the
    entry's time and bar number (None if no entry was found), and where bar 1
    starts.
    """
    period = 60.0 / bpm
    duration = len(y) / sr
    beat_times = np.arange(phase, duration, period)
    entry = find_groove_entry(y, sr, beat_times)
    anchor = beat_times[entry] if entry is not None else beat_times[0]

    bar_len = beats_per_bar * period
    lead_in = anchor / period                       # beats of audio before the anchor
    whole_bars = int(lead_in // beats_per_bar)
    bars_before = whole_bars + (1 if lead_in - whole_bars * beats_per_bar >= 1.0 else 0)
    bar1 = anchor - bars_before * bar_len

    bars = []
    while bar1 + len(bars) * bar_len < duration - period:   # each bar holds a beat or more
        t = bar1 + len(bars) * bar_len
        bars.append({'bar': len(bars) + 1, 'time': float(t),
                     'frame': int(librosa.time_to_frames(max(t, 0.0), sr=sr))})
    info = {
        'period': period,
        'bar1_time': float(bar1),
        'entry_time': float(anchor) if entry is not None else None,
        'entry_bar': bars_before + 1 if entry is not None else None,
    }
    return bars, info


def build_bar_grid(beat_frames, beat_times, beats_per_bar):
    """
    Group the beat tracker's beats into bars (the --no-anchor grid).
    beat_frames[0] is treated as bar 1, beat 1 — no downbeat detection.

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

def sync_to_bars(data, bar_frames, **kwargs):
    """
    Aggregate frame-level features to exactly one column per bar.

    librosa.util.sync's default pad=True adds frame 0 as a boundary, which
    prepends a "pre-roll" column covering the audio *before* the first beat —
    so column 0 is not bar 1, and every column after it is shifted by one.
    Syncing on [bar starts..., end of data] with pad=False gives column i =
    bar i+1, with the last bar running to the end of the audio.
    """
    idx = np.append(bar_frames, data.shape[-1])
    return librosa.util.sync(data, idx, pad=False, **kwargs)


def segment_features(y, sr):
    """Frame-level CQT and MFCCs — the expensive part, computed once per track."""
    return np.abs(librosa.cqt(y=y, sr=sr)), librosa.feature.mfcc(y=y, sr=sr)


def segment_bars(C, mfcc, bar_frames, n_sections):
    Csync = sync_to_bars(C, bar_frames, aggregate=np.median)

    # Repetition (recurrence) affinity from bar-synchronous CQT
    R = librosa.segment.recurrence_matrix(
        Csync, width=3, mode='affinity', sym=True
    )
    df = librosa.segment.timelag_filter(scipy_median_filter)
    Rf = df(R, size=(1, 7))

    # Local-path similarity from bar-synchronous MFCCs
    Msync = sync_to_bars(mfcc, bar_frames)
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

    labels[i] is the cluster for bar (i+1) — sync_to_bars guarantees exactly
    one label per bar — so every run boundary is already bar-aligned and this
    is just a run-length grouping, no snapping needed.
    """
    if len(labels) != total_bars:
        raise ValueError(f"expected {total_bars} bar labels, got {len(labels)}")
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


STABILITY_FIRM = 0.9   # a boundary that holds in >= 90% of re-runs is reported as firm


def bar_times_to_frames(times, sr):
    frames = librosa.time_to_frames(np.maximum(times, 0.0), sr=sr)
    for i in range(1, len(frames)):          # keep one column per bar after rounding
        frames[i] = max(frames[i], frames[i - 1] + 1)
    return frames


def boundary_stability(C, mfcc, bars, sr, n_sections, segments):
    """
    Mark each segment's start boundary with how often it survives re-runs.

    Re-segments 12 times with the bar grid nudged the way tiny tempo
    differences nudge it — bar times scaled by ±0.0005-0.002% (which shifts
    the frame rounding of some bars and not others), and each bar moved by a
    random -1/0/+1 frame (fixed seed, so the report is deterministic) — and
    counts the re-runs with a boundary within one bar of each reported one.
    Stores seg['stable'] = (agree, runs) on every segment but the first.

    Shifting the whole grid uniformly, or changing KMeans seeds, never moved
    a boundary in testing, while a 0.0004% tempo difference moved splits
    inside a gradual breakdown — so it's uneven rounding that's modelled.
    Varying --sections is deliberately not: asking for fewer section types
    than a track has removes boundaries that are correct.
    """
    times = np.array([b['time'] for b in bars])
    rng = np.random.default_rng(0)
    variants = [times[0] + (times - times[0]) * (1 + e)
                for e in (-2e-5, -1e-5, -5e-6, 5e-6, 1e-5, 2e-5)]
    variants += [times + rng.integers(-1, 2, len(times)) * 512 / sr for _ in range(6)]

    runs = []
    for v in variants:
        labels = segment_bars(C, mfcc, bar_times_to_frames(v, sr), n_sections)
        runs.append([s['bar_start'] for s in labels_to_bar_boundaries(labels, len(bars))[1:]])
    for seg in segments[1:]:
        agree = sum(any(abs(seg['bar_start'] - b) <= 1 for b in run) for run in runs)
        seg['stable'] = (agree, len(runs))
    return segments


# ── Per-section report stats ─────────────────────────────────────────────────

def annotate_energy(segments, y, sr, bars, beats_per_bar):
    rms = librosa.feature.rms(y=y)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=512)
    rms_norm = rms / (rms.max() + 1e-12)

    end_time = len(y) / sr
    bar_edges = [b['time'] for b in bars] + [end_time]
    bar_energy = []
    for t0, t1 in zip(bar_edges[:-1], bar_edges[1:]):
        vals = rms_norm[(rms_times >= t0) & (rms_times < t1)]
        bar_energy.append(float(vals.mean()) if len(vals) else 0.0)
    bar_energy = np.array(bar_energy)

    for seg in segments:
        per_bar = bar_energy[seg['bar_start'] - 1:seg['bar_end'] - 1]
        if len(per_bar) == 0:
            seg['energy'] = 0.0
            seg['trend'] = 'flat'
            continue
        seg['energy'] = round(float(per_bar.mean()), 2)
        # Fit the trend over per-bar means, then threshold the fitted change
        # from first bar to last. Per-bar means ignore envelope shape *within*
        # a bar (a stab decaying every bar is steady, not falling), and a
        # whole-segment change is scale-free — a per-frame slope on ~23 ms RMS
        # frames is too small to ever cross a fixed threshold.
        change = 0.0
        if len(per_bar) >= 2:
            change = np.polyfit(np.arange(len(per_bar)), per_bar, 1)[0] * (len(per_bar) - 1)
        seg['trend'] = 'rising' if change > 0.05 else 'falling' if change < -0.05 else 'flat'
    return segments


# ── Reporting ────────────────────────────────────────────────────────────────

_GROUP_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

def group_letter(group_id):
    return _GROUP_LETTERS[group_id % len(_GROUP_LETTERS)]

def tempo_warning(tracked, pulse):
    octave = octave_relation(tracked, pulse)
    octave = f' ({octave})' if octave else ''
    hint = f"{round(pulse, 2):g}"
    return (f"WARNING: the beat tracker locked onto {tracked:.2f} BPM, but the strongest "
            f"repeating pulse in the audio is {pulse:.2f} BPM{octave}. If {hint} sounds "
            f"right, re-run with --bpm-hint {hint} — a wrong tempo fragments the sections below.")


def hint_warning(hint, nearest, duration):
    drift = abs(hint / nearest - 1) * duration * nearest / 60.0
    return (f"WARNING: --bpm-hint {hint:g} doesn't hold against the audio — the closest "
            f"steady tempo is {nearest:.2f} BPM, and a grid at {hint:g} drifts about "
            f"{drift:.0f} beats over the track. Check by ear before trusting either.")


def grid_note(info):
    if info is None:
        return "Bar grid: the beat tracker's beats, bar 1 = first detected beat (--no-anchor; verify by ear)."
    bpm = 60.0 / info['period']
    into = -info['bar1_time'] / info['period']      # beats of bar 1 before the audio starts
    if abs(into) < 0.1:
        start = "the audio starts on bar 1"
    elif into > 0:
        start = f"the audio starts {into:.1f} beats into bar 1"
    else:
        start = f"the first {-into:.1f} beats of audio sit before bar 1"
    if info['entry_bar'] is None:
        return (f"Bar grid: steady {bpm:.2f} BPM; no clear groove entry found, so bar 1 is the "
                f"first beat on the grid — {start}. Verify the downbeat by ear.")
    m, s = divmod(info['entry_time'], 60)
    return (f"Bar grid: steady {bpm:.2f} BPM, anchored on the groove entry at "
            f"{int(m)}:{s:04.1f} = bar {info['entry_bar']}, beat 1 — {start}.")


def boundary_label(seg):
    if 'stable' not in seg:
        return '—'
    agree, runs = seg['stable']
    return 'firm' if agree >= STABILITY_FIRM * runs else f'moves ({agree}/{runs})'


def print_report(tempo, segments, beats_per_bar, notes=(), tempo_source=''):
    print(f"Tempo: {tempo:.2f} BPM{tempo_source}")
    for note in notes:
        print(note)
    print(f"Time signature assumed: {beats_per_bar}/4\n")
    print(f"{'#':<4} {'Bars':<12} {'Length':<9} {'Energy':<8} {'Trend':<9} {'Group':<7} {'Boundary'}")
    print('-' * 67)
    for i, seg in enumerate(segments, 1):
        length = seg['bar_end'] - seg['bar_start']
        bars_str = f"{seg['bar_start']}–{seg['bar_end']}"
        print(f"{i:<4} {bars_str:<12} {length:<9} {seg['energy']:<8} {seg['trend']:<9} "
              f"{group_letter(seg['group']):<7} {boundary_label(seg)}")
    print()
    if any(seg.get('stable') for seg in segments):
        runs = next(seg['stable'][1] for seg in segments if seg.get('stable'))
        print(f"Boundary = whether the row's start held when the analysis was re-run {runs} times")
        print("with the bar grid nudged by a frame (as tiny tempo differences do). 'moves'")
        print("means some re-runs put it elsewhere or dropped it — treat it as approximate.\n")
    print("Sections sharing a Group letter look structurally similar (likely repeats,")
    print("e.g. two drops). Nothing has been written — use --write to also emit a")
    print("structure.json skeleton with placeholder names for you to rename by ear.")


# ── Main ─────────────────────────────────────────────────────────────────────

def analyze(path, n_sections, beats_per_bar, bpm_hint, anchor=True):
    """
    Returns (tempo, segments, notes, tempo_source): notes are lines for the
    report header, tempo_source says where the tempo came from.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"No such file: {path}")
    y, sr = librosa.load(path)
    tracked, beat_frames, beat_times = detect_beats(y, sr, bpm_hint)
    env, fps = comb_envelope(y, sr)

    warnings, tempo_source = [], ''
    if bpm_hint:
        # A pinned tempo is used as given, but say so if the audio disagrees.
        tempo, tempo_source = bpm_hint, ' (pinned with --bpm-hint)'
        nearest = check_hint(env, fps, bpm_hint)
        if nearest is not None:
            warnings.append(hint_warning(bpm_hint, nearest, len(y) / sr))
    else:
        # Refining stays within ±5% of what the tracker found (it removes
        # lag-bin rounding); a disagreement with the independent measurement
        # is settled by resolve_tempo.
        tempo = refine_tempo(env, fps, tracked) if anchor else tracked
        measured = check_tempo(y, sr, env, fps, tempo)
        if measured is not None:
            resolved, line = resolve_tempo(env, fps, tempo, measured)
            warnings.append(line)
            if resolved != tempo:
                tempo_source = f' (measured; the beat tracker said {tempo:.2f})'
                tempo = resolved
                if not anchor:
                    _, beat_frames, beat_times = detect_beats(y, sr, tempo)

    if anchor:
        _, phase = comb_fit(env, fps, [tempo])
        bars, info = anchored_bar_grid(y, sr, tempo, phase, beats_per_bar)
    else:
        bars, info = build_bar_grid(beat_frames, beat_times, beats_per_bar), None
    notes = warnings + [grid_note(info)]

    if len(bars) < n_sections:
        raise ValueError(
            f"Only {len(bars)} bars detected — fewer than --sections {n_sections}. "
            f"Pass a smaller --sections or check --bpm-hint."
        )

    bar_frames = np.array([b['frame'] for b in bars])
    C, mfcc = segment_features(y, sr)
    labels = segment_bars(C, mfcc, bar_frames, n_sections)
    segments = labels_to_bar_boundaries(labels, len(bars))
    segments = boundary_stability(C, mfcc, bars, sr, n_sections, segments)
    segments = annotate_energy(segments, y, sr, bars, beats_per_bar)
    return tempo, segments, notes, tempo_source


def write_structure_json(path, tempo, segments, beats_per_bar):
    cfg = {
        'bpm': round(tempo, 2),
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
                        help='Number of distinct section types (repetition groups) to '
                             'cluster into (default: 6). Types recur, so the report '
                             'usually lists more rows than this')
    parser.add_argument('--time-signature', type=int, default=4, dest='beats_per_bar',
                        help='Beats per bar (default: 4)')
    parser.add_argument('--bpm-hint', type=float, default=None,
                        help='Pin the tempo to this BPM, for when auto-detection '
                             'locks onto the wrong one')
    parser.add_argument('--no-anchor', action='store_true',
                        help="Use the beat tracker's beats as the bar grid, with bar 1 = "
                             "first detected beat, instead of a steady-tempo grid anchored "
                             "on the groove entry — for music not on a fixed tempo, or if "
                             "the anchor lands on the wrong beat")
    parser.add_argument('--write', metavar='PATH', default=None,
                        help='Also write a structure.json skeleton with placeholder names')
    args = parser.parse_args()

    try:
        tempo, segments, notes, tempo_source = analyze(
            args.path, args.sections, args.beats_per_bar, args.bpm_hint, anchor=not args.no_anchor)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print_report(tempo, segments, args.beats_per_bar, notes, tempo_source)

    if args.write:
        write_structure_json(args.write, tempo, segments, args.beats_per_bar)
