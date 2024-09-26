# EMS-ESP-Flasher

Flash tool for uploading EMS-ESP firmware.

Based on [https://github.com/Jason2866/ESP_Flasher/](https://github.com/Jason2866/ESP_Flasher/) version 3.0.3 with these modifications:

- uses EMS-ESP specific partitions
- added option to not erase flash and retain settings (--no-erase option)
- removed the safeboot and factory firmware options
- removed show logs option
- update with EMS-ESP icons
- updated to from PyQt5 to PyQt6 and made the UI a little nicer
- auto CTRL-C to log into to EMS-ESP console
- update setuptools - moved to a .toml file. See <https://setuptools.pypa.io/en/latest/userguide/>
- note: GitHub Action build.yml uses `jason2866/upload-artifact@v2.0.3` instead of `actions/upload-artifact@v4` because of the multi-artifact feature (<https://github.com/actions/download-artifact/pull/202>)

## Installation

If you plan to run Python in a virtual environment, first do this:

```sh
python -m venv venv
source ./venv/bin/activate`
```

Then setup the environment with:

```sh
pip install --upgrade build
pip install -e .
```

To build and test the a module for distribution (places in dist folder):

```sh
python -m build
```

```sh
pip install --editable .
```

To run as a module for testing locally:

```sh
python -m emsesp_flasher
```

To test the module build:

## Building the platform executables

### macOS

```sh
pyinstaller -F -w -n EMS-ESP-Flasher -i icon.icns emsesp_flasher/__main__.py
```

Will create a `dist/EMS-ESP-Flasher` file and `*.app` folder

### Windows

```sh
python -m PyInstaller -F -w -n EMS-ESP-Flasher -i icon.ico emsesp_flasher\__main__.py
```

Will create a `dist/EMS-ESP-Flasher.exe` file.

If the Windows firewall blocks the .exe file, it's a false positive. See [here](<https://github.com/pyinstaller/pyinstaller/issues/3802>).

## Creating the installers in GitHub

After a tagged release, the artifacts will be created. Tag using:

```sh
git tag -f v1.1.0 
git push --tags -f
```
