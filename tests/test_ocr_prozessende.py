"""Apple Vision darf beim Beenden eines Hilfsprozesses keine Python-Objekte mehr halten.

Vision gibt seine Arbeitsobjekte später auf einem eigenen Thread frei. Lag darin noch eine
Python-Liste (OC_PythonArray), stürzte Python beim planmäßigen Wechsel der Hilfsprozesse ab
(„Python wurde unerwartet beendet“, EXC_BREAKPOINT in PyGILState_Ensure).
"""
import subprocess
import sys

from belegsuche.ocr import native_strings


def test_sprachen_sind_natives_nsarray():
    arr = native_strings(["de-DE", "en-US"])
    assert not arr.className().startswith("OC_Python")
    for i in range(arr.count()):
        assert not arr.objectAtIndex_(i).nsstring().className().startswith("OC_Python")


def test_vision_haelt_keine_python_liste():
    import Vision
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLanguages_(native_strings(["de-DE"]))
    held = req.recognitionLanguages()
    # Hält Vision eine Python-Liste, gibt PyObjC die Liste selbst zurück statt eines NSArray
    assert not isinstance(held, list)
    assert held.className() != "OC_PythonArray"


def test_prozessende_nach_ocr_ohne_absturz():
    code = (
        "from PIL import Image, ImageDraw\n"
        "from belegsuche.ocr import AppleVisionOCR\n"
        "img = Image.new('RGB', (600, 200), 'white')\n"
        "ImageDraw.Draw(img).text((20, 80), 'Rechnung 4711', fill='black')\n"
        "e = AppleVisionOCR()\n"
        "for _ in range(3): e.recognize_image(img)\n"
    )
    for _ in range(4):
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=120)
        assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-2000:]
