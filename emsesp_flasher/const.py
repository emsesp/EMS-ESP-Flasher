import re

__version__ = "1.1.1"

# TODO update latest arduino-esp32 version 2.0.17?
ESP32_DEFAULT_OTA_DATA = "https://github.com/espressif/arduino-esp32/raw/2.0.5/tools/partitions/boot_app0.bin"

# TODO update latest arduino-esp32 version 2.0.17?
ESP32_DEFAULT_BOOTLOADER_FORMAT = (
    "https://github.com/espressif/arduino-esp32/raw/2.0.5/"
    "tools/sdk/$MODEL$/bin/bootloader_$FLASH_MODE$_$FLASH_FREQ$.bin"
)

ESP32_DEFAULT_PARTITIONS = (
    "https://raw.githubusercontent.com/emsesp/EMS-ESP-Flasher/main/"
    "partitions/partitions_$MODEL$_$FLASH_SIZE$.bin"
)

# https://stackoverflow.com/a/3809435/8924614
HTTP_REGEX = re.compile(
    r"https?://(www\.)?[-a-zA-Z0-9@:%._+~#=]{2,256}\.[a-z]{2,6}\b([-a-zA-Z0-9@:%_+.~#?&/=]*)"
)
