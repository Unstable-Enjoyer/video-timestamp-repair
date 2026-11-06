# fix_timestamps.py

Fixes a specific timestamp fault in long H.264 recordings: the player shows an inflated length (about 26 hours in the examined files), and playback stutters from some point to the end. The script rebuilds the video timestamps and writes a new MP4. Audio and video are not re-encoded: every compressed packet is copied unchanged. Files without the fault are left alone.

## Quick start

```
pip install av
python fix_timestamps.py recording.mp4 --dry-run    # analyze only; exit code 0 means a fix is needed
python fix_timestamps.py recording.mp4              # writes recording_fixed.mp4
```

Dry-run report for a test clip with the fault:

```
video frames: 900  slice types: {'I': 15, 'P': 262, 'B': 623}  reorder delay: 2
frame rate: 30.0000 (30) from stream VUI timing; measured 30.0668
header duration: 32.021s -> rebuilt span 32.000s
original problems: bad PTS 0, bunched DTS 606, display-order conflicts 376, header inflated False
order cross-check vs trustworthy original PTS: 63 frames, 0 conflicts
recorder clock vs rebuilt timeline (leader frames): p1 +0.000s, median +0.000s, p99 +0.000s
gaps kept: 1
  at     16.967s (frame 451): 2.000s
video ends 32.000s, audio ends 31.995s
needs fix: yes
```

## Symptoms

- The player reports a length far beyond the real one. In the two files examined, it showed 26.4 h and 26.3 h for 6.5 h and 6.6 h of content. Both were MP4 files written by FFmpeg's libavformat (encoder tag `Lavf62.12.102`).
- From one point to the end, playback stutters and the frame rate is reported as inconsistent.
- The part before that point plays normally. In the two recordings examined, the fault started 3,773 s (about 63 minutes) into one and 67 s into the other.

## Cause

Every frame carries two timestamps:

- **DTS** (decode timestamp): when the frame is decoded.
- **PTS** (presentation timestamp): when it is shown.

These differ when the stream uses B-frames, which are frames predicted partly from a later frame. That later frame has to be stored first, so frames are stored in one order and shown in another, and the PTS restores the display order.

What we measured in a 6.5-hour and a 6.6-hour recording (30 fps and 60 fps, H.264 + AAC):

1. **Header duration.** The MP4 header gave the length as 95,207 s (26.4 h) and 94,731 s (26.3 h). The real content was 23,290 s and 23,924 s, which matches the audio tracks, whose headers were correct. Where the fault starts, the keyframe carries a PTS of 95,207.58 s and 94,731.72 s, while the frames around it are at about 3,773 s and 67 s. In the first file, the frame after the keyframe has one too (95,207.72 s). The header duration matches the largest PTS plus one frame duration (95,207.72 s + 0.03 s = 95,207.75 s in the first file), so these values set the length players show.
2. **Bunched timestamps.** From that point to the end, frames come in groups: mostly 4 frames in the 30 fps file and 3 in the 60 fps file. Within a group, each DTS is one tick after the previous one. A tick is the file's smallest time unit, 1/16000 s in these files. The next group starts about as many frame durations later as the group has frames. The frame count still matches the nominal rate (every 10 s of the 30 fps file holds 297 to 302 frames; every 15 minutes holds 26,997 to 27,003, except the stretch with the stalls), so no frames are missing; only the individual times are wrong. Measured against a constant rate, this clock wobbles by about ±0.1 s, with no long-term drift. Players schedule frames by these timestamps, so the frames of a group are shown almost together and then nothing until the next group. This is the likely cause of the stutter; it was not tested on a player.
3. **Lost display order.** In the same stretch, every frame's PTS equals its DTS, although both streams use B-frames (reorder depth 2 and 1). The order in which frames should be shown was lost from the timestamps.
4. **Real stalls.** The recorder also paused for real: about 1.1 s and 7.1 s in one file (the audio has a gap of about 6.1 s there), and about 1.05 s in the other. These gaps are genuine and have to be kept to stay in sync with the audio.

Not established:

- **What triggers the fault.** It starts at one point and continues to the end of the file.
- **Where the 26-hour values come from.** They are near the 26.5-hour point where MPEG transport-stream timestamps (a 33-bit counter at 90 kHz) wrap around, but about 4 minutes away from it, so this may be a coincidence.
- **What produced the one-tick spacing.** It is the pattern the `ffmpeg` command-line tool produces when it receives timestamps that don't increase: it moves each one to the previous value plus one tick. Whether that is what happened here is not known.

## How the fix works

The display order is still stored in the video data. Every H.264 frame header contains a *picture order count*, a number that gives the frame's position in display order. The script reads these headers without decoding any images.

1. **Display order.** Rebuilt from the picture order counts. Where the original PTS still carry ordering information (they go backwards in decode order), they must agree with the rebuilt order, or the script refuses the file.
2. **Frame rate.** Taken from the `--fps` option if given. Otherwise from the H.264 stream's own timing information, if it is within 2% of the measured rate. Otherwise the measured rate is snapped to a standard rate within 1%, and failing that the measured rate is used as is.
3. **Timeline.** The frames are laid out at that constant rate. The original DTS are used as the recorder's clock, split into segments, and each segment is aligned to the median of that clock over the whole segment, so the ±0.1 s wobble averages out. A segment boundary is never placed inside a group of frames whose display order differs from their storage order.
4. **Remux.** The same packets are copied into a new MP4 container with the new timestamps. The MP4 writer computes the header duration from them.
5. **Verification.** The new file is re-read. For every packet, the size and CRC32 checksum must match the input and the video timestamps must match the plan; packet counts must match per stream; and the header duration must not exceed the content by more than 1 s. The file is written as `<output>.partial` and renamed only after these checks pass.

| Threshold | Value |
|---|---|
| Clock jump that starts a new segment (a stall) | more than 0.5 s between two frames |
| Sustained step that starts a new segment (e.g. dropped frames) | more than 0.25 s or 3 frames (whichever is larger), judged on medians of 10 s blocks |
| PTS treated as garbage | missing, before its DTS, or more than 5 s after it |
| Allowed conflicts with trustworthy original PTS | 2, or 0.1% of the frames checked (whichever is larger) |
| Header counted as inflated | more than 1 s longer than the content |
| Allowed gap between video end and audio end | 2 s, or 0.1% of the length (whichever is larger) |

A file is counted as faulty if it has any garbage PTS, two consecutive frames (in storage order) whose DTS are less than half a frame apart, a display-order conflict (a frame whose original PTS is not later than that of the frame before it in the rebuilt display order), or an inflated header. Otherwise the script exits with code 10 and writes nothing.

## Usage

```
python fix_timestamps.py input [output] [--fps 30] [--dry-run] [--json] [--force]
                         [--overwrite] [--no-faststart] [--no-verify]
```

| Option | Meaning |
|---|---|
| `output` | Output path. Default: `<input without extension>_fixed.mp4`, in the input's folder. The output is always MP4. |
| `--dry-run` | Analyze and report; write nothing. |
| `--json` | Print one JSON object on stdout; the log goes to stderr. Without it, the log goes to stdout. |
| `--fps` | Use this frame rate instead of detecting it (`30`, `29.97`, `60000/1001`, ...). |
| `--force` | Remux even if no fault is found. All safety checks still apply. |
| `--overwrite` | Replace an existing output file. |
| `--no-faststart` | Leave the MP4 index at the end of the file. Saves one rewrite of the output; a player reading the file over plain HTTP has to fetch the end of the file before it can start. |
| `--no-verify` | Skip verification. The output is then renamed without being checked. |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Output written. It was verified unless `--no-verify` was given. With `--dry-run`: a fault was found, or `--force` was given. |
| 10 | No fault found; nothing written. |
| 2 | Not handled, nothing written: unsupported input (see Limits), output path equals the input, output already exists (without `--overwrite`), or not enough disk space. Invalid command-line arguments also exit with 2. |
| 3 | A safety check failed; nothing written. For example: display order disagrees with trustworthy timestamps, broken audio timestamps, video and audio ends too far apart, more frames than elapsed time, or a verification mismatch. |
| 1 | Unexpected error, for example a file that can't be opened. |

### JSON output

Always present: `input`, `status` (`fixed`, `analyzed`, `not_needed`, `unsupported`, `unsafe`, or `error`), `exit_code`. On a stop or error: `reason`. After analysis: `needs_fix`, `fps`, `fps_source`, `measured_fps`, `frames`, `gaps` (each with `frame`, `at`, `length`), `checks`, `dropped_streams`, `analysis_seconds`. `output` is set as soon as the output path is chosen, so it is also present on failed runs; check `status` before using it. After a successful remux: `remux_seconds`, and unless `--no-verify` is given, `verify` and `verify_seconds`.

## Limits

**Input the script refuses** (exit 2):

- **Codec:** anything other than H.264, for example HEVC.
- **Container:** anything PyAV doesn't identify as MP4, MOV or Matroska (MKV, and probably WebM, which is Matroska-based but was not tested).
- **Stream layout:** none or more than one video stream, or fewer than 10 video frames.
- **H.264 features:** interlaced (field) coding, picture order count type 1, a missing `avcC` configuration record (for example a raw Annex B stream), a frame without a picture slice, or video packets without any timestamps.

An H.264 frame header that can't be parsed stops the script with exit 3.

**Streams that are dropped:** the output contains the video stream, every audio stream, and `mov_text` subtitles. Any other stream (other subtitle formats, data tracks, attachments) is dropped and listed in `dropped_streams`.

**Accuracy:**

- **Constant frame rate is assumed.** Variable-frame-rate input is not supported and was not tested. It is refused (exit 3) when it shows up as more frames than elapsed time, and other safety checks can also refuse it.
- **Timing comes from the recorder's own clock.** Each segment follows that clock's median, so in the damaged stretch, frame times are only as accurate as that clock (it wobbles about ±0.1 s). No independent record of the capture times was found in the file.
- **Small steps aren't corrected.** A step in the clock smaller than the segment threshold (0.25 s or 3 frames) is treated as wobble, and a stall shorter than 0.5 s is only detected through that same step rule.
- **Audio was tested with AAC only.** Other audio codecs are copied the same way but were not tested.
- **MKV is less precise.** MKV stores no DTS, so the clock is derived from the PTS. A garbage PTS in an MKV file lowers accuracy (by one frame in the synthetic test).
- **No prevention.** The script repairs files; it doesn't prevent the fault.

## Verification done

- **Two real recordings, 6.5 h at 30 fps and 6.6 h at 60 fps:**
  - The header duration went from 26.4 h and 26.3 h to 23,291 s and 23,925 s, matching each file's audio track.
  - Every audio and video packet in the output was identical to the input.
  - The stalls are kept as gaps in the outputs (about 1 s and 7 s in one file, about 1 s in the other), and the video and audio ends stay within 1 s of each other, as in the original files.
  - The 60 fps file was repaired with this version. The 30 fps file was repaired with an earlier version and re-analyzed (dry run) with this one.
  - Not verifiable on real files: how close the rebuilt frame times in the damaged stretch are to the true capture times, since no independent record of those was found.
- **Synthetic clips** with faults injected on purpose, so the right answer was known:
  - A 2 s stall was rebuilt as 2.000 s at 30 fps and 2.002 s at 29.97 fps (60 frames).
  - The display order matched the undamaged clip exactly, at 30 fps, 29.97 fps, and in MKV.
  - In the MKV clip, which also had one garbage PTS, the 2 s stall was rebuilt as 1.966 s (one frame short).
  - Healthy MP4 and MKV clips exited with 10.
  - HEVC was refused with exit 2.

## Requirements and cost

- **Software:** Python 3 (tested with 3.11.9) and [PyAV](https://pyav.org/) (`pip install av`, tested with 18.1.0).
- **Disk:** free space on the output drive of at least the input size plus 64 MB.
- **I/O:** a full run reads the input twice (analysis and remux), writes the output, rewrites it once to move the index to the front (skipped with `--no-faststart`), and reads it once more to verify. On a 22.9 GB, 6.6-hour, 60 fps file already in the disk cache, that took 45 s for analysis, 73 s for the remux and 35 s for verification. With an earlier version of the script, the analysis of a different, 20 GB file took 469 s with a cold disk cache; the analysis speed is limited by the disk.
- **Memory:** the script keeps per-frame data for the whole file in RAM, so memory grows with the frame count. An earlier version peaked at 180 MB (0.7 million frames) and 280 MB (1.4 million frames). This version was not measured on files of that size.
