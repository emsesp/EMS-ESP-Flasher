# EMS-ESP-Flasher

Flash tool for uploading EMS-ESP firmware.

Based on [https://github.com/Jason2866/ESP_Flasher/](https://github.com/Jason2866/ESP_Flasher/) version 3.0.0 with these modifications:

- use EMS-ESP partitions in const.py
- added option to keep settings by adding --no-erase option
- removed ESP32_SAFEBOOT_SERVER

## License

[MIT](http://opensource.org/licenses/MIT) © Marcel Stör, Otto Winter, Johann Obermeier

## Building

- `python3 -m venv venv` to create the virtual environment
- `source ./venv/bin/activate` to enter it

for installing first time:
`pip3 install -e .` to install the libraries

for running:
`python3 esp_flasher`

## Virus warning on Windows

If windows blocks the .exe file, it's a false positive. See [here](<https://github.com/pyinstaller/pyinstaller/issues/3802>) and [here](<https://github.com/Jason2866/ESP_Flasher/issues/35>) for more information.

## Building the executables

### macOS

`pyinstaller -F -w -n ESP-Flasher -i icon.icns esp_flasher/__main__.py`

### Windows

1. Start up VM
2. Install Python (3) from App Store
3. Download esp-flasher from GitHub
4. `pip install -e.` and `pip install pyinstaller`
5. Check with `python -m esp_flasher.__main__`
6. `python -m PyInstaller.__main__ -F -w -n ESP-Flasher -i icon.ico esp_flasher\__main__.py`
7. Go to `dist` folder, check ESP-Flasher.exe works.
