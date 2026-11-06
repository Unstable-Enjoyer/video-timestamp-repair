# fix_timestamps.py

Repairs long screen/game recordings (MP4 or MKV, H.264 video) whose timestamps are broken, **without re-encoding**. The compressed audio and video are copied byte for byte. Only the timing information is rebuilt.

## The symptoms

- A recording that is really a few hours long shows up as **about 26 hours** in the player.
- From some point on, playback **stutters** or the frame rate looks **inconsistent** (MPC-HC reports it as variable), and seeking jumps to the wrong place.
- The first part of the recording often plays fine. The problem starts at one point and lasts to the end.

## Why this happens (plain English)

A video file stores two clocks for every frame:

- **When to decode it** (DTS, decode timestamp)
- **When to show it** (PTS, presentation timestamp)

These are different because modern video uses *B-frames*. A B-frame is built from the frame before it *and the frame after it*, so the encoder has to send a "future" frame first. Frames therefore arrive in one order and are shown in another, and the PTS is what puts them back in the right order.

In the broken recordings we examined, three things had gone wrong:

1. **Two garbage timestamps made the file look 26 hours long.** At the moment things went wrong, two frames were stamped with a "show at" time of about 26.4 hours. The file's header records the latest timestamp it saw as the length of the whole movie, so players report 26 hours. That's very close to the point where the 90 kHz, 33-bit clock used by MPEG transport streams wraps around (about 26.5 hours). That suggests the bad values were small negative or wrapped-around numbers, but this is an inference, not something we proved.

2. **Frames were stamped in bursts instead of one by one.** From that point to the end, frames were stamped in clumps, usually about four frames at a time, with almost no time between them. Then the clock jumped ahead to catch up. The frames are all there (exactly 30 or 60 per second on average), but their individual times are wrong. That's what causes the stutter. The tell-tale detail: frames inside a clump are exactly **one tick** (1/16000 s) apart. That's what FFmpeg's file writer does when it gets several frames with the same timestamp: it nudges each one forward by the smallest possible step so the timestamps keep increasing. So the recorder most likely handed over whole batches of frames with the same time, probably stamped when the batch *arrived* rather than when each frame was *captured*.

3. **The "show it" order was lost.** In the same broken stretch, every frame's "show at" time equals its "decode at" time. For a stream with B-frames that can't be right, because it means frames are shown in the order they arrive, which is the wrong order. The player gets a jumbled sequence, which adds to the stutter and breaks seeking.

The recorder also really did stall for a moment (about 1 second, and in one file another 6 to 7 seconds), so there are genuine short gaps in the recording. Those have to be kept, or the audio would drift out of sync.

What *triggers* the recorder to switch into this broken mode can't be determined from the files alone. What the files do show is that the switch happened at a single moment (a stall followed by a fresh keyframe), and everything after it carries the damage.

## How the fix works

The key insight is that **the correct display order is still inside the video data itself**. Every H.264 frame carries a small header field, the *picture order count*, that says where it belongs in display order. The script reads that header field from each frame. It never decodes any pictures. Then it:

1. **Rebuilds the display order** from the picture order count. Where the file's original timestamps were still good, the script checks that the rebuilt order matches them exactly, and refuses to continue if it doesn't.
2. **Picks the frame rate** from the stream's own settings and cross-checks it against the measured rate (for example, it correctly tells 29.97 from 30).
3. **Lays the frames out on an even clock** at that frame rate. It keeps the real gaps (recorder stalls, or runs of dropped frames). Each stretch between gaps is lined up with the recorder's own long-run clock, so audio stays in sync.
4. **Writes a new MP4** with the same compressed data and new timestamps, plus a correct duration in the header.
5. **Verifies the result** by re-reading every packet of the new file and comparing it with the input. The new file only gets its real name once that check passes.

The original file is never modified.

## Usage

```
python fix_timestamps.py input.mp4 [output.mp4] [--fps 30] [--dry-run] [--no-faststart]
                         [--json] [--force] [--overwrite] [--no-verify]
```

| Option | Meaning |
|---|---|
| `output.mp4` | Where to write the result (default: `<input>_fixed.mp4`) |
| `--dry-run` | Analyze and report only, write nothing |
| `--json` | Print one machine-readable result on stdout (logs go to stderr) |
| `--fps` | Override the detected frame rate (`30`, `29.97`, `60000/1001`, ...) |
| `--force` | Remux even if the file looks healthy |
| `--overwrite` | Replace an existing output file |
| `--no-faststart` | Leave the index at the end of the file (skips one extra pass over the file) |
| `--no-verify` | Skip re-reading the output for verification |

### Exit codes (for use in a backend)

| Code | Meaning |
|---|---|
| 0 | Fixed and verified (with `--dry-run`: a fix would be made) |
| 10 | File is healthy, nothing written |
| 2 | Unsupported input (codec, container, stream layout, not enough disk space, ...) |
| 3 | A safety check failed, nothing written |
| 1 | Unexpected error |

The script makes every decision itself. Anything it can't handle safely is refused with a reason, never guessed.

## Requirements

- Python 3 (tested with 3.11)
- [PyAV](https://pyav.org/) (`pip install av`, tested with 18.1)
- Free disk space roughly equal to the input file's size

## What it handles, and what it doesn't

**Handles:** H.264 in MP4/MOV/MKV with any number of audio tracks (copied unchanged), constant frame rate recordings at any common rate, recorder stalls, dropped frames, and garbage timestamps.

**Refuses:**
- HEVC/H.265 and other codecs
- Interlaced video
- Other containers
- Files with more than one video stream
- Files whose audio timestamps are also broken

**Accuracy limits:**
- The recorder's clock in the broken stretch wobbles by about ±0.1 s, so small amounts of dropped frames (under 0.25 s in total) can't be told apart from that wobble and aren't corrected.
- On MKV input, which stores no decode times, a garbage timestamp can cost about one frame of accuracy.

## Tested on

- Two real 6.5-hour recordings with this problem (30 fps and 60 fps). Both were restored to their true length, and every audio and video packet was verified identical to the original.
- Synthetic test clips with deliberately broken timestamps, where the correct answer is known: healthy MP4/MKV, 29.97 fps, injected stalls, garbage timestamps, dropped frames and HEVC. In every repaired clip, the display order matched the original exactly.
