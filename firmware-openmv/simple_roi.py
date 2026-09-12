import sensor, image, time

sensor.reset()
sensor.set_pixformat(sensor.RGB565)
sensor.set_framesize(sensor.QVGA)
sensor.skip_frames(time=2000)

clock = time.clock()

# Random and placeholder values for now until the actualy coordinates are set.

rois = [
    (20, 20, 100, 80),
    (140, 20, 100, 80),
]

while True:
    clock.tick()
    img = sensor.snapshot()

    for roi in rois:
        img.draw_rectangle(roi, color=(255, 0, 0))

    print("FPS:", clock.fps())
