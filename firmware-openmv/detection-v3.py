# MediSense detection v3: AprilTag based lid detection with sensor windowing.

import csi
import time
from machine import UART

COMPARTMENTS = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}

STRIP = [(36, 184, 592, 96)]                            # all 7 tags (too big for memory)
HALVES = [(36, 178, 330, 96), (372, 184, 264, 96)]      # Sun-Wed, Thu-Sat
HALF_NAMES = ["Sun-Wed", "Thu-Sat"]

# The Frame Buffer shows whichever window is captured last.
# 0 = Sun-Wed, 1 = Thu-Sat. Only changes the order of the scan, not what is detected.
PREVIEW = 0

MIN_MARGIN = 0.10  # ignore weak detections
OPEN_SCANS = 2     # tag seen in this many scans in a row -> OPEN
CLOSE_MS = 1000    # tag missing this long -> CLOSED

uart = UART(3, baudrate=115200)


def send(line):
    uart.write(line + "\n")


def pixels(windows):
    return sum(w * h for (_, _, w, h) in windows)


csi0 = csi.CSI()
csi0.reset()
csi0.pixformat(csi.GRAYSCALE)
csi0.framesize(csi.VGA)
csi0.window(STRIP[0])
csi0.snapshot(time=2000)   # let exposure settle, then lock it
csi0.auto_gain(False)
csi0.auto_whitebal(False)

# Try the single strip once. If AprilTag detection runs out of memory, uses the halves.
try:
    csi0.snapshot().find_apriltags()
    windows = STRIP
    mode = "one strip"
except (MemoryError, OSError) as err:
    # Put the preview window last so the Frame Buffer shows it.
    order = [i for i in range(len(HALVES)) if i != PREVIEW] + [PREVIEW]
    windows = [HALVES[i] for i in order]
    mode = "two halves (strip failed: %s), Frame Buffer shows %s" % (err, HALF_NAMES[PREVIEW])
    csi0.window(windows[0])
    csi0.snapshot()

is_open = {c: False for c in COMPARTMENTS}
seen_run = {c: 0 for c in COMPARTMENTS}
last_seen_ms = {c: 0 for c in COMPARTMENTS}
peak_margin = {c: 0.0 for c in COMPARTMENTS}
tag_width = {c: 0 for c in COMPARTMENTS}
frame_errors = 0


def scan():
    global frame_errors
    best = {c: 0.0 for c in COMPARTMENTS}
    for win in windows:
        # A failed capture skips this window for one scan instead of stopping the script.
        # One missed scan can't cause a false CLOSED (that needs 1000 ms without the tag).
        try:
            if len(windows) > 1:
                csi0.window(win)
                csi0.snapshot()   # discard first frame after a window change
            img = csi0.snapshot()
            tags = img.find_apriltags()
        except (RuntimeError, OSError, MemoryError) as err:
            frame_errors += 1
            print("Camera frame error (%d so far): %s" % (frame_errors, err))
            continue
        for tag in tags:
            if tag.id in COMPARTMENTS and tag.decision_margin >= MIN_MARGIN:
                img.draw_detection(tag, color1=(255, 0, 0), color2=(0, 255, 0))
                if tag.decision_margin > best[tag.id]:
                    best[tag.id] = tag.decision_margin
                    tag_width[tag.id] = tag.w
    return best


print("=== MediSense detection v3 (AprilTags, Windowed) ===")
print("Compartments:", ", ".join("%s=tag %d" % (COMPARTMENTS[c], c) for c in sorted(COMPARTMENTS)))
print("Scan mode: %s, %d window(s), %d pixels" % (mode, len(windows), pixels(windows)))

scan_count = 0
while True:
    t0 = time.ticks_ms()
    best = scan()
    now = time.ticks_ms()
    scan_ms = time.ticks_diff(now, t0)
    scan_count += 1

    for c in COMPARTMENTS:
        if best[c] > 0:
            seen_run[c] += 1
            last_seen_ms[c] = now
            peak_margin[c] = max(peak_margin[c], best[c])
            if not is_open[c] and seen_run[c] >= OPEN_SCANS:
                is_open[c] = True
                print("Compartment %d (%s): OPEN   (margin %.2f, width %d px)"
                      % (c, COMPARTMENTS[c], best[c], tag_width[c]))
                send("CAM,%d,OPEN,%.2f" % (c, best[c]))
        else:
            seen_run[c] = 0
            if is_open[c] and time.ticks_diff(now, last_seen_ms[c]) >= CLOSE_MS:
                is_open[c] = False
                print("Compartment %d (%s): CLOSED (peak margin while open %.2f)"
                      % (c, COMPARTMENTS[c], peak_margin[c]))
                send("CAM,%d,CLOSED,%.2f" % (c, peak_margin[c]))
                peak_margin[c] = 0.0

    if scan_count % 20 == 0:
        print("scan time %d ms" % scan_ms)
        send("HB,%d" % scan_ms)
