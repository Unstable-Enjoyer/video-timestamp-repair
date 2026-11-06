"""
Lossless timestamp repair for MP4/MKV recordings with broken H.264 video timestamps.

What it fixes (no re-encoding, compressed data is copied byte-for-byte):
  * frames with absurd PTS values that blow the container duration up (e.g. 26 h)
  * runs of frames whose DTS are bunched 1 tick apart (stutter / bad seeking)
  * regions where PTS == DTS although the stream has B-frames (wrong display order)

How:
  1. Read every packet (no decoding) and parse the H.264 slice headers to get the
     picture order count (POC), which gives the true display order.
  2. Pick the frame rate (stream VUI timing, cross-checked against the measured rate).
  3. Rebuild the video timeline at that constant rate. It is split into segments at
     recorder stalls (clock jumps) and sustained clock steps (e.g. dropped frames); each
     segment is aligned to the median of the recorder's own clock over that segment, so
     audio sync is kept. Segment boundaries never split a group of reordered frames.
  4. Remux video + untouched audio (+ mov_text subtitles) into a new MP4, verify it
     against the input packet by packet, then move it into place.

All decisions are made automatically. Anything the script cannot handle safely is
refused with a reason instead of guessed.

Exit codes:
   0  fixed and verified (or --dry-run: a fix would be made)
  10  file is healthy, nothing written (use --force to remux anyway)
   2  unsupported input (codec, container, stream layout, missing disk space, ...)
   3  safety check failed (timeline, sync or verification); nothing written
   1  unexpected error

Usage:
  python fix_timestamps.py input.mp4 [output.mp4] [--fps 30] [--dry-run] [--no-faststart]
                           [--json] [--force] [--overwrite] [--no-verify]
"""
import argparse
import collections
import json
import os
import shutil
import sys
import time
import traceback
import zlib
from array import array
from fractions import Fraction

import av

EXIT_OK, EXIT_ERROR, EXIT_UNSUPPORTED, EXIT_UNSAFE, EXIT_NOT_NEEDED = 0, 1, 2, 3, 10

STANDARD_FPS = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
                Fraction(30), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60),
                Fraction(72), Fraction(90), Fraction(100), Fraction(120000, 1001),
                Fraction(120), Fraction(144), Fraction(240)]
FPS_AGREE = 0.02            # VUI fps must be within 2% of the measured rate to be trusted
FPS_SNAP = 0.01             # measured rate snaps to a standard rate within 1%
STEP_TOL_SECONDS = 0.25     # a sustained clock step larger than this (or STEP_TOL_FRAMES,
STEP_TOL_FRAMES = 3         #   whichever is larger) starts a new segment. Real recorder
STEP_BLOCK_SECONDS = 10.0   #   clocks wander about +-0.1 s in bunched regions; steps are
                            #   judged on medians of blocks this long.
ORDER_CONFLICTS_ALLOWED = 2         # conflicts with trustworthy PTS tolerated (boundaries of
ORDER_CONFLICTS_ALLOWED_RATIO = 0.001   # broken regions), whichever is larger
JUMP_SECONDS = 0.5          # a DTS jump above this is a real gap (recorder stall)
SANE_PTS_SECONDS = 5.0      # original PTS more than this after DTS is treated as garbage
HEADER_SLACK_SECONDS = 1.0  # header duration this much above the real span = inflated
END_ALIGN_SECONDS = 2.0     # video and audio must end within this (or 0.1% of length)
NOPTS = -(1 << 63)

LOG = sys.stdout


def log(msg):
    print(msg, file=LOG, flush=True)


class Stop(Exception):
    """Controlled stop with an exit code and a reason."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ---------------------------------------------------------------- H.264 bit reading
class Bits:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def u(self, n):
        v = 0
        for _ in range(n):
            byte = self.d[self.p >> 3]
            v = (v << 1) | ((byte >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError("bad exp-golomb code")
        return (1 << z) - 1 + self.u(z)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def unescape(nal, limit=None):
    """Remove emulation prevention bytes (00 00 03 -> 00 00)."""
    src = nal if limit is None else nal[:limit]
    out = bytearray()
    zeros = 0
    for b in src:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def parse_sps(nal):
    b = Bits(unescape(nal[1:]))
    profile = b.u(8)
    b.u(16)  # constraint flags + level
    sps_id = b.ue()
    sep_colour = 0
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        cf = b.ue()
        if cf == 3:
            sep_colour = b.u(1)
        b.ue(); b.ue(); b.u(1)
        if b.u(1):  # seq_scaling_matrix_present
            for i in range(8 if cf != 3 else 12):
                if b.u(1):
                    last, nxt = 8, 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + b.se() + 256) % 256
                        last = nxt or last
    log2_fn = b.ue() + 4
    poc_type = b.ue()
    log2_poc = 0
    if poc_type == 0:
        log2_poc = b.ue() + 4
    elif poc_type == 1:
        raise Stop(EXIT_UNSUPPORTED, "H.264 pic_order_cnt_type 1 is not supported")
    b.ue(); b.u(1); b.ue(); b.ue()
    frame_mbs_only = b.u(1)
    vui_fps = None
    try:
        if not frame_mbs_only:
            b.u(1)
        b.u(1)                                   # direct_8x8_inference
        if b.u(1):                               # frame_cropping
            b.ue(); b.ue(); b.ue(); b.ue()
        if b.u(1):                               # vui_parameters_present
            if b.u(1) and b.u(8) == 255:         # aspect_ratio_info / extended SAR
                b.u(32)
            if b.u(1):                           # overscan_info_present
                b.u(1)
            if b.u(1):                           # video_signal_type_present
                b.u(4)
                if b.u(1):
                    b.u(24)
            if b.u(1):                           # chroma_loc_info_present
                b.ue(); b.ue()
            if b.u(1):                           # timing_info_present
                tick, scale = b.u(32), b.u(32)
                if tick and scale:
                    vui_fps = Fraction(scale, 2 * tick)
    except (IndexError, ValueError):
        vui_fps = None
    return sps_id, dict(sep_colour=sep_colour, log2_fn=log2_fn, poc_type=poc_type,
                        log2_poc=log2_poc, frame_mbs_only=frame_mbs_only, vui_fps=vui_fps)


def parse_pps(nal):
    b = Bits(unescape(nal[1:], 32))
    pps_id = b.ue()
    sps_id = b.ue()
    return pps_id, sps_id


def iter_nals(mv, nal_len_size):
    """Yields (offset, size) of each NAL unit in a length-prefixed packet."""
    p, n = 0, len(mv)
    while p + nal_len_size <= n:
        size = int.from_bytes(mv[p:p + nal_len_size], "big")
        p += nal_len_size
        if size == 0 or p + size > n:
            break
        yield p, size
        p += size


def parse_avcc(extra):
    """Returns (nal_length_size, list of SPS/PPS NALs) from avcC extradata."""
    nls = (extra[4] & 3) + 1
    nals, p = [], 5
    for count_mask in (0x1F, 0xFF):
        count = extra[p] & count_mask
        p += 1
        for _ in range(count):
            size = int.from_bytes(extra[p:p + 2], "big")
            nals.append(extra[p + 2:p + 2 + size])
            p += 2 + size
    return nls, nals


def mp4_header_durations(path):
    """Durations stored in the MP4 header boxes (mvhd, tkhd, mdhd), in seconds.
    Players show these; FFmpeg recomputes its own, so they are read directly."""
    import struct
    out = []
    with open(path, "rb") as f:
        end_file = os.path.getsize(path)

        def boxes(start, end):
            pos = start
            while pos + 8 <= end:
                f.seek(pos)
                size, typ = struct.unpack(">I4s", f.read(8))
                hdr = 8
                if size == 1:
                    size = struct.unpack(">Q", f.read(8))[0]
                    hdr = 16
                elif size == 0:
                    size = end - pos
                if size < hdr:
                    return
                yield typ, pos + hdr, pos + size
                pos += size

        def full(start, n):
            f.seek(start)
            return f.read(n)

        movie_ts = None
        for typ, s, e in boxes(0, end_file):
            if typ != b"moov":
                continue
            for t2, s2, e2 in boxes(s, e):
                if t2 == b"mvhd":
                    b = full(s2, 32)
                    ts, du = struct.unpack(">II", b[12:20]) if b[0] == 0 else struct.unpack(">IQ", b[20:32])
                    movie_ts = ts
                    if ts:
                        out.append(du / ts)
                elif t2 == b"trak":
                    for t3, s3, e3 in boxes(s2, e2):
                        if t3 == b"tkhd" and movie_ts:
                            b = full(s3, 36)
                            du = struct.unpack(">I", b[20:24])[0] if b[0] == 0 else struct.unpack(">Q", b[28:36])[0]
                            out.append(du / movie_ts)
                        elif t3 == b"mdia":
                            for t4, s4, e4 in boxes(s3, e3):
                                if t4 == b"mdhd":
                                    b = full(s4, 32)
                                    ts, du = (struct.unpack(">II", b[12:20]) if b[0] == 0
                                              else struct.unpack(">IQ", b[20:32]))
                                    if ts:
                                        out.append(du / ts)
            break
    return out


# ---------------------------------------------------------------- analysis pass
class Track:
    """Per-packet data of one stream, in compact arrays."""

    def __init__(self):
        self.pts = array("q")
        self.dts = array("q")
        self.size = array("L")
        self.crc = array("L")
        self.pos = array("q")


class Video(Track):
    def __init__(self):
        super().__init__()
        self.lsb = array("l")
        self.log2poc = bytearray()
        self.poctype = bytearray()
        self.idr = bytearray()
        self.ref = bytearray()
        self.stype = bytearray()
        self.key = bytearray()


def analyze(path):
    """Demux all kept streams (no decoding) and collect per-packet info."""
    with av.open(path) as c:
        fmt = c.format.name.split(",")
        if not set(fmt) & {"mov", "mp4", "matroska"}:
            raise Stop(EXIT_UNSUPPORTED, f"container {c.format.name} is not supported (MP4/MOV/MKV only)")
        videos = [s for s in c.streams if s.type == "video"]
        if len(videos) != 1:
            raise Stop(EXIT_UNSUPPORTED, f"expected exactly one video stream, found {len(videos)}")
        vs = videos[0]
        if vs.codec_context.name != "h264":
            raise Stop(EXIT_UNSUPPORTED, f"video codec {vs.codec_context.name} is not supported (H.264 only)")
        extra = bytes(vs.codec_context.extradata or b"")
        if len(extra) < 7 or extra[0] != 1:
            raise Stop(EXIT_UNSUPPORTED, "H.264 without an avcC configuration record is not supported")
        keep = [vs] + [s for s in c.streams if s.type == "audio"] + \
               [s for s in c.streams if s.type == "subtitle" and s.codec_context.name == "mov_text"]
        keep_idx = {s.index for s in keep}
        dropped = [f"#{s.index} {s.type} {getattr(s.codec_context, 'name', '?')}"
                   for s in c.streams if s.index not in keep_idx]
        hdr = [c.duration / av.time_base] if c.duration else []
        if vs.duration:
            hdr.append(float(vs.duration * vs.time_base))
        if set(fmt) & {"mov", "mp4"}:
            hdr += mp4_header_durations(path)
        info = dict(
            format=c.format.name,
            header_duration=max(hdr) if hdr else 0.0,
            keep=[s.index for s in keep],
            audio=[s.index for s in keep if s.type == "audio"],
            dropped=dropped,
            tb={s.index: s.time_base for s in keep},
            video_index=vs.index,
        )

        nls, init_nals = parse_avcc(extra)
        sps, pps = {}, {}

        def param_nal(nal):
            t = nal[0] & 0x1F
            if t == 7:
                i, s = parse_sps(nal)
                sps[i] = s
            elif t == 8:
                i, s = parse_pps(nal)
                pps[i] = s

        for nal in init_nals:
            param_nal(nal)

        V = Video()
        others = {s.index: Track() for s in keep[1:]}
        vui = []
        total = os.path.getsize(path)
        next_log = 0.1
        for pkt in c.demux(keep):
            if pkt.size == 0:
                continue
            mv = memoryview(pkt)
            si = pkt.stream.index
            tr = V if si == vs.index else others[si]
            tr.pts.append(NOPTS if pkt.pts is None else pkt.pts)
            tr.dts.append(NOPTS if pkt.dts is None else pkt.dts)
            tr.size.append(pkt.size)
            tr.crc.append(zlib.crc32(mv))
            tr.pos.append(-1 if pkt.pos is None else pkt.pos)
            if pkt.pos and pkt.pos / total >= next_log:
                log(f"  analysis {int(next_log * 100)}%")
                next_log += 0.1
            if tr is not V:
                continue
            frame = None
            try:
                for off, size in iter_nals(mv, nls):
                    t = mv[off] & 0x1F
                    if t in (7, 8):
                        param_nal(bytes(mv[off:off + size]))
                    elif t in (1, 5) and frame is None:
                        b = Bits(unescape(bytes(mv[off + 1:off + min(size, 64)])))
                        b.ue()                      # first_mb_in_slice
                        slice_type = b.ue() % 5
                        s = sps[pps[b.ue()]]
                        if s["sep_colour"]:
                            b.u(2)
                        b.u(s["log2_fn"])           # frame_num
                        if not s["frame_mbs_only"] and b.u(1):
                            raise Stop(EXIT_UNSUPPORTED, "interlaced (field) coding is not supported")
                        if t == 5:
                            b.ue()                  # idr_pic_id
                        lsb = b.u(s["log2_poc"]) if s["poc_type"] == 0 else 0
                        frame = (t == 5, (mv[off] >> 5) & 3, lsb, s["log2_poc"], s["poc_type"],
                                 "PBIPI"[slice_type])
                        vui.append(s["vui_fps"])
            except (IndexError, ValueError, KeyError) as e:
                raise Stop(EXIT_UNSAFE, f"could not parse slice header of video packet {len(V.dts) - 1}: {e!r}")
            if frame is None:
                raise Stop(EXIT_UNSUPPORTED, f"video packet {len(V.dts) - 1} contains no picture slice")
            V.idr.append(frame[0]); V.ref.append(frame[1]); V.lsb.append(frame[2])
            V.log2poc.append(frame[3]); V.poctype.append(frame[4]); V.stype.append(ord(frame[5]))
            V.key.append(pkt.is_keyframe)
        if len(V.dts) < 10:
            raise Stop(EXIT_UNSUPPORTED, f"only {len(V.dts)} video frames")
        if NOPTS in V.dts:
            # e.g. MKV stores only PTS: use the sorted PTS as the recorder clock in decode order
            if NOPTS in V.pts:
                raise Stop(EXIT_UNSUPPORTED, "video packets without any timestamps")
            # shifted back by the reorder depth so that DTS <= PTS holds for every frame.
            # Absurd PTS (far from their decode-order neighbours) are replaced for this
            # purpose only, so a single garbage value cannot poison the clock.
            n = len(V.pts)
            far = SANE_PTS_SECONDS / vs.time_base
            clock = array("q", V.pts)
            for i in range(n):
                nb = sorted(V.pts[max(0, i - 8):i + 9])
                med = nb[len(nb) // 2]
                if abs(V.pts[i] - med) > far:
                    clock[i] = med
            by_pts = sorted(range(n), key=lambda i: (clock[i], i))
            prank = [0] * n
            for r, i in enumerate(by_pts):
                prank[i] = r
            delay = max(0, max(i - prank[i] for i in range(n)))
            sp = [clock[i] for i in by_pts]
            step = max(1, sp[1] - sp[0])
            V.dts = array("q", (sp[i - delay] if i >= delay else sp[0] - (delay - i) * step
                                for i in range(n)))
            info["dts_derived"] = True
        vfps = [f for f in vui if f]
        info["vui_fps"] = collections.Counter(vfps).most_common(1)[0][0] if vfps else None
    return V, others, info


def display_ranks(V):
    """Global display rank of every frame (decode order index -> display index)."""
    n = len(V.dts)
    keys = [0] * n
    period = 0
    prev_msb = prev_lsb = 0
    seen_idr = False
    for i in range(n):
        if V.idr[i]:
            if seen_idr:
                period += 1
            seen_idr = True
            prev_msb = prev_lsb = 0
        if V.poctype[i] == 2:
            poc = i
        else:
            lsb, mx = V.lsb[i], 1 << V.log2poc[i]
            if lsb < prev_lsb and prev_lsb - lsb >= mx // 2:
                msb = prev_msb + mx
            elif lsb > prev_lsb and lsb - prev_lsb > mx // 2:
                msb = prev_msb - mx
            else:
                msb = prev_msb
            if V.ref[i]:
                prev_msb, prev_lsb = msb, lsb
            poc = msb + lsb
        keys[i] = (period << 40) + poc + (1 << 39)
    order = sorted(range(n), key=keys.__getitem__)
    for a, b in zip(order, order[1:]):
        if keys[a] == keys[b]:
            raise Stop(EXIT_UNSAFE, f"duplicate picture order count at frames {a} and {b}")
    rank = array("l", [0]) * n
    for r, i in enumerate(order):
        rank[i] = r
    return rank


# ---------------------------------------------------------------- decisions
def measure_fps(V, tb):
    """Frame rate measured from the original DTS over stretches without big jumps."""
    n = len(V.dts)
    jump = JUMP_SECONDS / tb
    frames = 0
    span = 0
    s = 0
    for i in range(1, n + 1):
        if i == n or V.dts[i] - V.dts[i - 1] > jump:
            if i - 1 > s:
                frames += i - 1 - s
                span += V.dts[i - 1] - V.dts[s]
            s = i
    if span <= 0:
        raise Stop(EXIT_UNSAFE, "cannot measure the frame rate (no usable timestamp span)")
    return frames / float(span * tb)


def choose_fps(V, tb, info, override):
    measured = measure_fps(V, tb)
    if override:
        return override, measured, "override"
    vui = info["vui_fps"]
    if vui and abs(float(vui) / measured - 1) <= FPS_AGREE:
        return vui, measured, "stream VUI timing"
    best = min(STANDARD_FPS, key=lambda f: abs(float(f) / measured - 1))
    if abs(float(best) / measured - 1) <= FPS_SNAP:
        return best, measured, "measured, snapped to standard rate"
    return Fraction(measured).limit_denominator(1001), measured, "measured (non-standard)"


def timescale_for(fps):
    """Output video timescale and frame duration (both integers)."""
    a, b = fps.numerator, fps.denominator
    if (90000 * b) % a == 0:
        return 90000, 90000 * b // a
    m = -(-10000 // a)
    return a * m, b * m


def build_timeline(V, tb, fps, rank):
    """New PTS/DTS (in 1/T units, decode order) at a constant frame rate.

    The recorder clock (original DTS) is noisy in broken regions: frames come in bunches
    and the bunch phase wanders slowly by about +-0.1 s, with no long-term trend. So the
    timeline is split into segments only at
      * hard jumps of the clock (> JUMP_SECONDS), e.g. recorder stalls, and
      * sustained steps (10 s block medians moving by more than STEP_TOL), e.g. a run of
        dropped frames,
    and each segment gets one offset: the median over all its leader frames (first frame
    of each bunch). Every boundary is moved back to the nearest point that does not split
    a group of reordered frames.
    """
    n = len(V.dts)
    T, fd = timescale_for(fps)
    scale = tb * T
    orig = array("q", (round(d * scale) for d in V.dts))
    tol = max(round(STEP_TOL_SECONDS * T), STEP_TOL_FRAMES * fd)
    jump = round(JUMP_SECONDS * T)
    block = max(1, round(STEP_BLOCK_SECONDS * fps))   # frames per block

    # closed[i]: frames before decode index i are exactly the first i in display order,
    # so a segment can start there without splitting a group of reordered frames
    closed = bytearray(n + 1)
    hi = -1
    for i in range(n):
        closed[i] = hi == i - 1
        hi = max(hi, rank[i])
    leader = bytearray(n)
    leader[0] = 1
    for i in range(1, n):
        leader[i] = orig[i] - orig[i - 1] >= fd // 2

    def snap(g, lo):
        s = g
        while s > lo and not closed[s]:
            s -= 1
        if not closed[s]:
            raise Stop(EXIT_UNSAFE, f"no clean boundary to start a segment near frame {g}")
        return s

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2]

    # 1. hard jumps; a segment is (s, g): timeline starts at frame s, clock trusted from g
    bounds = [(0, 0)]
    for g in range(1, n):
        if orig[g] - orig[g - 1] > jump:
            bounds.append((snap(g, bounds[-1][0] + 1), g))

    # 2. sustained steps inside each hard segment
    segs = []
    for k, (s, g) in enumerate(bounds):
        e = bounds[k + 1][0] if k + 1 < len(bounds) else n
        segs.append((s, g))
        meds = []                           # (first frame of block, block median)
        for b0 in range(g, e, block):
            r = [orig[j] - j * fd for j in range(b0, min(e, b0 + block)) if leader[j]]
            if r:
                meds.append((b0, med(r)))
        if not meds:
            continue
        level = meds[0][1]
        for bi in range(1, len(meds)):
            b0, m = meds[bi]
            if m > level + tol:
                # locate the step: first leader from the previous block on whose clock is
                # past the midpoint and stays there for about a second
                lo_f = meds[bi - 1][0]
                mid = level + (m - level) // 2
                g2 = b0
                for j in range(lo_f, min(e, b0 + block)):
                    if leader[j] and orig[j] - j * fd > mid:
                        w = [orig[x] - x * fd for x in range(j, min(e, j + round(fps))) if leader[x]]
                        if med(w) > mid:
                            g2 = j
                            break
                if g2 > segs[-1][0] + 1:
                    segs.append((snap(g2, segs[-1][0] + 1), g2))
                level = m
            elif m < level - tol:
                raise Stop(EXIT_UNSAFE,
                           f"more frames than time around frame {b0} ({(level - m) / T:.3f}s ahead of "
                           f"the recorder clock at {float(fps):.3f} fps); not constant frame rate?")

    # 3. one offset per segment: median over its leaders of (clock - constant-rate position)
    slot = array("q", [0]) * n
    gaps = []
    for k, (s, g) in enumerate(segs):
        e = segs[k + 1][0] if k + 1 < len(segs) else n
        r = [orig[j] - (j - s) * fd for j in range(g, e) if leader[j]]
        base = med(r) if r else orig[g] - (g - s) * fd
        if k:
            prev_end = slot[s - 1] + fd
            if base < prev_end:
                if prev_end - base > tol:
                    raise Stop(EXIT_UNSAFE, f"segment at frame {s} would overlap the previous one by "
                                            f"{(prev_end - base) / T:.3f}s")
                base = prev_end
            gaps.append(dict(frame=s, at=base / T, length=(base - prev_end) / T))
        for i in range(s, e):
            slot[i] = base + (i - s) * fd
    gaps = [x for x in gaps if x["length"] > 0]

    resid = sorted(orig[j] - slot[j] for j in range(n) if leader[j])
    drift_stats = dict(  # recorder clock minus rebuilt timeline, on leader frames, seconds
        p01=resid[len(resid) // 100] / T, median=resid[len(resid) // 2] / T,
        p99=resid[len(resid) * 99 // 100] / T)
    delay = max(0, max(i - rank[i] for i in range(n)))
    pts = array("q", (slot[rank[i]] + delay * fd for i in range(n)))
    return dict(T=T, fd=fd, pts=pts, dts=slot, delay=delay, gaps=gaps, orig=orig, drift=drift_stats)


def assess(V, tb, rank, plan, info, others):
    """Decide whether the file needs fixing and run the safety gates on the plan."""
    n = len(V.dts)
    T, fd = plan["T"], plan["fd"]
    sane = SANE_PTS_SECONDS / tb
    ok = bytearray(n)
    bad_pts = 0
    for i in range(n):
        p, d = V.pts[i], V.dts[i]
        if p == NOPTS or p < d or p - d > sane:
            bad_pts += 1
        else:
            ok[i] = 1
    # order check: where the original PTS carry real reordering information (they go
    # backwards in decode order) they must agree with the POC order, otherwise our
    # parsing does not match this stream. Runs with PTS in decode order (as in the
    # broken regions) carry no reordering information and are not used.
    trusted = [i for i in range(n) if ok[i] and (
        (i + 1 < n and ok[i + 1] and V.pts[i + 1] < V.pts[i]) or
        (i > 0 and ok[i - 1] and V.pts[i - 1] > V.pts[i]))]
    trusted.sort(key=rank.__getitem__)
    order_conflicts_trusted = sum(1 for a, b in zip(trusted, trusted[1:]) if V.pts[b] <= V.pts[a])
    by_rank = sorted(range(n), key=rank.__getitem__)
    order_conflicts_all = sum(1 for a, b in zip(by_rank, by_rank[1:])
                              if V.pts[a] != NOPTS and V.pts[b] != NOPTS and V.pts[b] <= V.pts[a])
    half = fd / 2 / T / tb
    bunched = sum(1 for i in range(1, n) if V.dts[i] - V.dts[i - 1] < half)
    span = (max(plan["pts"]) - min(plan["pts"]) + fd) / T
    v_end = (max(plan["pts"]) + fd) / T

    a_end = None
    for si in info["audio"]:
        tr = others[si]
        if any(d == NOPTS for d in tr.dts) or any(b < a for a, b in zip(tr.dts, tr.dts[1:])):
            raise Stop(EXIT_UNSAFE, f"audio stream #{si} has broken timestamps (not handled by this script)")
        if tr.dts:
            atb = info["tb"][si]
            end = float(tr.dts[-1] * atb)
            a_end = end if a_end is None else max(a_end, end)
    hdr = info["header_duration"]
    content_end = max(v_end, a_end or 0.0)
    inflated = hdr > content_end + HEADER_SLACK_SECONDS

    checks = dict(
        bad_pts=bad_pts, bunched_dts=bunched, order_conflicts=order_conflicts_all,
        trusted_frames_checked=len(trusted), trusted_order_conflicts=order_conflicts_trusted,
        header_inflated=inflated, header_duration=hdr, new_span=span, video_end=v_end,
        audio_end=a_end, content_end=content_end, clock_drift=plan["drift"],
    )
    if order_conflicts_trusted > max(ORDER_CONFLICTS_ALLOWED, ORDER_CONFLICTS_ALLOWED_RATIO * len(trusted)):
        raise Stop(EXIT_UNSAFE, f"display order from the bitstream disagrees with {order_conflicts_trusted} "
                                f"trustworthy original timestamps")
    if a_end is not None and abs(v_end - a_end) > max(END_ALIGN_SECONDS, 0.001 * span):
        raise Stop(EXIT_UNSAFE, f"rebuilt video ends at {v_end:.3f}s but audio ends at {a_end:.3f}s")
    pts, dts = plan["pts"], plan["dts"]
    if not all(dts[i] > dts[i - 1] for i in range(1, n)):
        raise Stop(EXIT_UNSAFE, "rebuilt DTS not strictly increasing")
    if any(pts[i] < dts[i] for i in range(n)):
        raise Stop(EXIT_UNSAFE, "rebuilt timeline has frames shown before they are decoded")
    if len(set(pts)) != n:
        raise Stop(EXIT_UNSAFE, "rebuilt PTS not unique")
    needs = bool(bad_pts or bunched or order_conflicts_all or inflated)
    return needs, checks


def report(V, info, fps, measured, fps_src, plan, checks, needs):
    types = collections.Counter(chr(x) for x in V.stype)
    log(f"video frames: {len(V.dts)}  slice types: {dict(types)}  reorder delay: {plan['delay']}")
    log(f"frame rate: {float(fps):.4f} ({fps}) from {fps_src}; measured {measured:.4f}")
    log(f"header duration: {checks['header_duration']:.3f}s -> rebuilt span {checks['new_span']:.3f}s")
    log(f"original problems: bad PTS {checks['bad_pts']}, bunched DTS {checks['bunched_dts']}, "
        f"display-order conflicts {checks['order_conflicts']}, header inflated {checks['header_inflated']}")
    log(f"order cross-check vs trustworthy original PTS: {checks['trusted_frames_checked']} frames, "
        f"{checks['trusted_order_conflicts']} conflicts")
    d = plan["drift"]
    log(f"recorder clock vs rebuilt timeline (leader frames): p1 {d['p01']:+.3f}s, "
        f"median {d['median']:+.3f}s, p99 {d['p99']:+.3f}s")
    log(f"gaps kept: {len(plan['gaps'])}")
    for g in plan["gaps"][:20]:
        log(f"  at {g['at']:10.3f}s (frame {g['frame']}): {g['length']:.3f}s")
    if len(plan["gaps"]) > 20:
        log(f"  ... {len(plan['gaps']) - 20} more")
    if checks["audio_end"] is not None:
        log(f"video ends {checks['video_end']:.3f}s, audio ends {checks['audio_end']:.3f}s")
    if info["dropped"]:
        log(f"streams not carried over: {', '.join(info['dropped'])}")
    log("needs fix: " + ("yes" if needs else "no"))


# ---------------------------------------------------------------- remux + verify
def remux(src, dst, info, plan, faststart):
    opts = {"video_track_timescale": str(plan["T"])}
    if faststart:
        opts["movflags"] = "+faststart"
    vtb = Fraction(1, plan["T"])
    pts, dts = plan["pts"], plan["dts"]
    total = os.path.getsize(src)
    next_log = 0.1
    with av.open(src) as ic, av.open(dst, "w", format="mp4", options=opts) as oc:
        omap = {}
        for si in info["keep"]:
            o = oc.add_stream_from_template(ic.streams[si])
            if si == info["video_index"]:
                o.time_base = vtb
            omap[si] = o
        vi = info["video_index"]
        k = 0
        for pkt in ic.demux([ic.streams[i] for i in info["keep"]]):
            if pkt.size == 0:
                continue
            if pkt.pos and pkt.pos / total >= next_log:
                log(f"  remux {int(next_log * 100)}%")
                next_log += 0.1
            if pkt.stream.index == vi:
                pkt.time_base = vtb
                pkt.pts, pkt.dts, pkt.duration = pts[k], dts[k], plan["fd"]
                k += 1
            pkt.stream = omap[pkt.stream.index]
            oc.mux(pkt)
    if k != len(pts):
        raise Stop(EXIT_UNSAFE, f"video packet count changed during remux: {k} vs {len(pts)}")


def read_raw(path, pos, size):
    with open(path, "rb") as f:
        f.seek(pos)
        return f.read(size)


def verify(src, dst, V, others, info, plan):
    """Re-read the output and compare every packet with the analysis pass."""
    tracks = [V] + [others[si] for si in info["keep"][1:]]
    src_is_mp4 = bool(set(info["format"].split(",")) & {"mov", "mp4"})
    reread = 0
    with av.open(dst) as c:
        if len(c.streams) != len(tracks):
            raise Stop(EXIT_UNSAFE, f"output has {len(c.streams)} streams, expected {len(tracks)}")
        vtb = c.streams[0].time_base
        T = plan["T"]
        counts = [0] * len(tracks)
        offset = None   # the MP4 edit list shifts timestamps on read by one constant
        for pkt in c.demux():
            if pkt.size == 0:
                continue
            oi = pkt.stream.index
            tr = tracks[oi]
            k = counts[oi]
            counts[oi] += 1
            if k >= len(tr.size) or pkt.size != tr.size[k]:
                raise Stop(EXIT_UNSAFE, f"output stream {oi} packet {k}: size/count mismatch")
            if zlib.crc32(memoryview(pkt)) != tr.crc[k]:
                # re-read both sides straight from disk to rule out a transient read error
                out_raw = read_raw(dst, pkt.pos, pkt.size)
                if zlib.crc32(out_raw) != tr.crc[k] and not (
                        src_is_mp4 and tr.pos[k] >= 0 and read_raw(src, tr.pos[k], tr.size[k]) == out_raw):
                    raise Stop(EXIT_UNSAFE, f"output stream {oi} packet {k}: data differs from input")
                reread += 1
            if oi == 0:
                p = pkt.pts * vtb * T
                d = pkt.dts * vtb * T
                if offset is None:
                    offset = p - plan["pts"][k]
                    if abs(offset) > T:
                        raise Stop(EXIT_UNSAFE, f"output timeline shifted by {float(offset / T):.3f}s")
                if p - plan["pts"][k] != offset or d - plan["dts"][k] != offset:
                    raise Stop(EXIT_UNSAFE, f"output video packet {k}: timestamps differ from the plan")
        for oi, tr in enumerate(tracks):
            if counts[oi] != len(tr.size):
                raise Stop(EXIT_UNSAFE, f"output stream {oi}: {counts[oi]} packets, expected {len(tr.size)}")
        dur = c.duration / av.time_base if c.duration else 0
    dur = max([dur] + mp4_header_durations(dst))
    return dict(output_header_duration=dur, packets_reread=reread)


def parse_fps(s):
    f = Fraction(s)
    for std in STANDARD_FPS:
        if abs(float(f) / float(std) - 1) < 0.0005:
            return std
    return f


# ---------------------------------------------------------------- main
def run(a, result):
    t0 = time.time()
    log("analyzing (reading packets, no decoding)...")
    V, others, info = analyze(a.input)
    result["dropped_streams"] = info["dropped"]
    vtb = info["tb"][info["video_index"]]
    rank = display_ranks(V)
    fps, measured, fps_src = choose_fps(V, vtb, info, a.fps)
    plan = build_timeline(V, vtb, fps, rank)
    needs, checks = assess(V, vtb, rank, plan, info, others)
    report(V, info, fps, measured, fps_src, plan, checks, needs)
    result.update(fps=str(fps), fps_source=fps_src, measured_fps=round(measured, 4),
                  frames=len(V.dts), gaps=plan["gaps"], checks=checks, needs_fix=needs,
                  analysis_seconds=round(time.time() - t0, 1))
    if not needs and not a.force:
        result["status"] = "not_needed"
        return EXIT_NOT_NEEDED
    if a.dry_run:
        result["status"] = "analyzed"
        return EXIT_OK

    out = a.output or os.path.splitext(a.input)[0] + "_fixed.mp4"
    if os.path.abspath(out) == os.path.abspath(a.input):
        raise Stop(EXIT_UNSUPPORTED, "output path equals the input path")
    if os.path.exists(out) and not a.overwrite:
        raise Stop(EXIT_UNSUPPORTED, f"output exists: {out} (use --overwrite)")
    need = os.path.getsize(a.input) + (64 << 20)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(out))).free
    if free < need:
        raise Stop(EXIT_UNSUPPORTED, f"not enough disk space: need {need >> 20} MB, free {free >> 20} MB")
    tmp = out + ".partial"
    result["output"] = out
    try:
        t1 = time.time()
        log(f"remuxing to {tmp} ...")
        remux(a.input, tmp, info, plan, not a.no_faststart)
        result["remux_seconds"] = round(time.time() - t1, 1)
        if not a.no_verify:
            t2 = time.time()
            log("verifying output against input ...")
            v = verify(a.input, tmp, V, others, info, plan)
            if v["output_header_duration"] > checks["content_end"] + HEADER_SLACK_SECONDS:
                raise Stop(EXIT_UNSAFE, f"output header duration {v['output_header_duration']:.3f}s "
                                        f"is longer than the content")
            result.update(verify=v, verify_seconds=round(time.time() - t2, 1))
            log(f"verified: header duration {v['output_header_duration']:.3f}s, "
                f"{v['packets_reread']} packet(s) needed a re-read")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    result["status"] = "fixed"
    log(f"done: {out}")
    return EXIT_OK


def main():
    global LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", help="default: <input>_fixed.mp4")
    ap.add_argument("--fps", type=parse_fps, help="override the detected frame rate (e.g. 30, 29.97, 60000/1001)")
    ap.add_argument("--dry-run", action="store_true", help="analyze and report only")
    ap.add_argument("--no-faststart", action="store_true",
                    help="leave the index at the end (skips the second pass over the file)")
    ap.add_argument("--json", action="store_true", help="print one JSON result on stdout, logs go to stderr")
    ap.add_argument("--force", action="store_true", help="remux even if the file looks healthy")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    ap.add_argument("--no-verify", action="store_true", help="skip re-reading the output for verification")
    a = ap.parse_args()
    if a.json:
        LOG = sys.stderr
    result = dict(input=a.input, status=None)
    try:
        code = run(a, result)
    except Stop as e:
        code = e.code
        result.update(status={EXIT_UNSUPPORTED: "unsupported", EXIT_UNSAFE: "unsafe"}.get(code, "error"),
                      reason=e.reason)
        log(f"stopped: {e.reason}")
    except Exception as e:
        code = EXIT_ERROR
        result.update(status="error", reason=repr(e))
        log(traceback.format_exc())
    result["exit_code"] = code
    if a.json:
        print(json.dumps(result, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
