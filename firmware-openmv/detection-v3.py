# MediSense detection v3: AprilTag-based lid detection with sensor windowing.
#
# Each lid has a 23 mm TAG36H11 AprilTag on its underside. The tag ID is the
# compartment number. The tag faces the camera only when the lid is open flat,
# so: tag seen = OPEN, tag not seen = CLOSED. A hand can hide a tag (missed
# open) but cannot create one (no false open).
#
# Memory: the H7 R2 cannot run find_apriltags() on a 320x240 frame. Instead the
# sensor runs at VGA (640x480) detail and captures one small window at a time.
# Each scan visits every compartment's window once.

import csi
import time

WINDOWS = {
    1: (52, 160, 240, 160),
    2: (372, 160, 240, 160),
}
NAMES = {1: "A", 2: "B"}

MIN_MARGIN = 0.10  # ignore weak detections (OpenMV docs recommend > 0.1)
OPEN_SCANS = 2     # tag seen in this many scans in a row -> OPEN
CLOSE_SCANS = 3    # tag missing in this many scans in a row -> CLOSED

csi0 = csi.CSI()
csi0.reset()
csi0.pixformat(csi.GRAYSCALE)
csi0.framesize(csi.VGA)
csi0.window(WINDOWS[1])
csi0.snapshot(time=2000)   # let exposure settle, then lock it
csi0.auto_gain(False)
csi0.auto_whitebal(False)

is_open = {c: False for c in WINDOWS}
seen_run = {c: 0 for c in WINDOWS}
missed_run = {c: 0 for c in WINDOWS}
peak_margin = {c: 0.0 for c in WINDOWS}
tag_width = {c: 0 for c in WINDOWS}


def scan():
    best = {c: 0.0 for c in WINDOWS}
    for c in WINDOWS:
        csi0.window(WINDOWS[c])
        csi0.snapshot()       
        img = csi0.snapshot()
        for tag in img.find_apriltags():
            if tag.id in WINDOWS and tag.decision_margin >= MIN_MARGIN:
                img.draw_detection(tag, color1=(255, 0, 0), color2=(0, 255, 0))
                if tag.decision_margin > best[tag.id]:
                    best[tag.id] = tag.decision_margin
                    tag_width[tag.id] = tag.w
    return best


print("=== MediSense detection v3 (AprilTags, windowed) ===")
print("Compartments:", ", ".join("%s=tag %d" % (NAMES[c], c) for c in WINDOWS))

scan_count = 0
while True:
    t0 = time.ticks_ms()
    best = scan()
    scan_ms = time.ticks_diff(time.ticks_ms(), t0)
    scan_count += 1

    for c in WINDOWS:
        if best[c] > 0:
            seen_run[c] += 1
            missed_run[c] = 0
            peak_margin[c] = max(peak_margin[c], best[c])
            if not is_open[c] and seen_run[c] >= OPEN_SCANS:
                is_open[c] = True
                print("Compartment %s: OPEN   (margin %.2f, width %d px)"
                      % (NAMES[c], best[c], tag_width[c]))
        else:
            missed_run[c] += 1
            seen_run[c] = 0
            if is_open[c] and missed_run[c] >= CLOSE_SCANS:
                is_open[c] = False
                print("Compartment %s: CLOSED (peak margin while open %.2f)"
                      % (NAMES[c], peak_margin[c]))
                peak_margin[c] = 0.0

    if scan_count % 20 == 0:
        print("scan time %d ms" % scan_ms)
