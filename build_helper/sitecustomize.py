# Loaded by every Python process of the build: onnxruntime must be imported before WinRT (winocr),
# otherwise PyInstaller's analysis subprocess crashes while importing rapidocr.
try:
    import onnxruntime  # noqa: F401
except Exception:
    pass

