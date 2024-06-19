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
`pip3 install wxpython`
`pip3 install -e .` to install the libraries

for running:
`python3 esp_flasher`

## Virus warning on Windows

If windows blocks the .exe file, it's a false positive. See [here](<https://github.com/pyinstaller/pyinstaller/issues/3802>).
