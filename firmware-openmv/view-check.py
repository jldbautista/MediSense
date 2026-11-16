# Full frame view check
import csi

csi0 = csi.CSI()
csi0.reset()
csi0.pixformat(csi.GRAYSCALE)
csi0.framesize(csi.VGA)      # if MemoryError, change VGA to QVGA
csi0.snapshot(time=2000)

while True:
    csi0.snapshot()
