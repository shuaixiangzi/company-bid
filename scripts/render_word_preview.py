"""Use native Word PDF output with the bundled render_docx PNG pipeline.

Windows workspace runtime does not contain LibreOffice. No installed desktop
LibreOffice is used. Inputs remain byte-identical; PDF/PNGs are QA-only.
"""
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--renderer", type=Path, required=True)
    parser.add_argument("--poppler", type=Path, required=True)
    parser.add_argument("--engine", choices=["Word", "WPS"], default="Word")
    args = parser.parse_args()
    source = args.input.resolve()
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pdf = output / (source.stem + ".pdf")
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(Path(__file__).with_name("export_word_pdf.ps1")),
                    "-InputPath", str(source), "-OutputPath", str(pdf), "-Engine", args.engine], check=True, timeout=180)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original, "Immutable DOCX changed during preview"
    os.environ["PATH"] = str(args.poppler.resolve()) + os.pathsep + os.environ.get("PATH", "")
    spec = importlib.util.spec_from_file_location("bundled_render_docx", args.renderer)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    # Replace just the unavailable conversion backend. All page scaling and PNG
    # production still use the packaged renderer's rasterize implementation.
    renderer.convert_to_pdf = lambda *a, **k: (str(pdf), "Native Word read-only PDF export")
    pages = renderer.rasterize(str(source), str(output), 150, False, False)
    print(f"Rendered {len(pages)} pages; immutable input hash verified")


if __name__ == "__main__":
    main()
