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

last_print = time.ticks_ms()

while True:
    img = sensor.snapshot()
    for roi in rois:
        img.draw_rectangle(roi, color=(255, 0, 0))

    if time.ticks_diff(time.ticks_ms(), last_print) > 500:
        last_print = time.ticks_ms()
        for i, roi in enumerate(rois):
            s = img.get_statistics(roi=roi)
            print("ROI", "AB"[i], "| L:", s.l_mean, "A:", s.a_mean, "B:", s.b_mean)
        print("-----")
