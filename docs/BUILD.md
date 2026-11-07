## Building from Source

### Prerequisites

1. Python 3.11 or higher
2. uv package manager
3. Tesseract OCR installed at `C:\Program Files\Tesseract-OCR\`

### Build Steps

1. Clone the repository and install dev dependencies:
   ```bash
   git clone https://github.com/ItzRobD/ARLO-AT.git
   cd ARLO-AT
   uv sync --all-extras
   ```

2. Run the build script:
   ```bash
   uv run python build.py
   ```

3. Find the output in `dist/ARLO/`. Zip that folder to share it. Each person adds their own ARC Tracker key to `.env` inside it.

The build has to run on Windows; PyInstaller can't cross-compile from Linux. `ARLO.spec` bundles the ARC Tracker dependencies (`mcp`, `httpx`, `pyyaml`). If `ARLO.exe` fails with `ModuleNotFoundError`, add the missing module to `hiddenimports` in `ARLO.spec` and rebuild.

### Build Output

```
dist/ARLO/
├── ARLO.exe    # Main application (shows console window)
├── Calibrate.exe           # Calibration tool (GUI only)
├── _internal/              # Python dependencies (don't modify)
├── tesseract/              # Bundled Tesseract OCR
│   ├── tesseract.exe
│   ├── *.dll
│   └── tessdata/
│       └── eng.traineddata
├── .env                    # Configuration file (edit this)
├── .env.example            # Configuration reference
├── resolutions.json        # Pre-configured resolution profiles
├── rules.example.yaml      # Copy to rules.yaml for personal keep targets
├── sample_items.csv        # Example item database
└── README.md               # This file
```
