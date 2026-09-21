import sensor, image, time

sensor.reset()
sensor.set_pixformat(sensor.RGB565)
sensor.set_framesize(sensor.QVGA)
sensor.skip_frames(time=2000)
sensor.set_auto_gain(False)
sensor.set_auto_whitebal(False)

# ROI Coordinates
rois = [
    (20, 22, 83, 189), # Compartment A
    (109, 14, 102, 202), # Compartment B
]
names = ["A", "B"]

L_DROP = 6    # brightness must drop by at least this much
B_RISE = 3    # B color channel must rise by at least this much

# ---- Calibration: ALL LIDS CLOSED, hands out of frame ----
print("Calibrating in 3 sec: keep ALL lids CLOSED, keep hands out of frame")
sensor.skip_frames(time=3000)
N = 30
sums = [[0.0, 0.0, 0.0] for _ in rois]
for _ in range(N):
    img = sensor.snapshot()
    for i, roi in enumerate(rois):
        s = img.get_statistics(roi=roi)
        sums[i][0] += s.l_mean
        sums[i][1] += s.a_mean
        sums[i][2] += s.b_mean
base = [(t[0] / N, t[1] / N, t[2] / N) for t in sums]
for i in range(len(rois)):
    print("Baseline", names[i], "L:", base[i][0], "B:", base[i][2])
print("Calibration done. Open/close lids now.")

# ---- Detection Loop ----
prev_open = [False] * len(rois)
last_print = time.ticks_ms()

while True:
    img = sensor.snapshot()
    report = time.ticks_diff(time.ticks_ms(), last_print) > 500
    if report:
        last_print = time.ticks_ms()

    for i, roi in enumerate(rois):
        s = img.get_statistics(roi=roi)
        l_drop = base[i][0] - s.l_mean
        b_rise = s.b_mean - base[i][2]
        is_open = (l_drop >= L_DROP) and (b_rise >= B_RISE)

        color = (255, 0, 0) if is_open else (0, 255, 0)
        img.draw_rectangle(roi, color=color)
        label = names[i] + (" OPEN" if is_open else " CLOSED")
        # img.draw_string(roi[0] + 2, roi[1] + 2, label, color=color, scale=2)

        if is_open != prev_open[i]:
            print(">>> Compartment", names[i], "->", "OPEN" if is_open else "CLOSED")
            prev_open[i] = is_open

        if report:
            print(names[i], "| L drop:", l_drop, "B rise:", b_rise)
    if report:
        print("-----")
