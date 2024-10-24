#!/usr/bin/env python
#
# SPDX-FileCopyrightText: 2014-2022 Fredrik Ahlberg, Angus Gratton, Espressif Systems (Shanghai) CO LTD, other contributors as noted.
#
# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import division, print_function
from typing import Dict

import argparse
import base64
import binascii
import copy
import hashlib
import inspect
import io
import itertools
import os
import re
import shlex
import string
import struct
import sys
import time
import zlib

try:
    import serial
except ImportError:
    print("Pyserial is not installed for %s. Check the README for installation instructions." % (sys.executable))
    raise

# check 'serial' is 'pyserial' and not 'serial' https://github.com/espressif/esptool/issues/269
try:
    if "serialization" in serial.__doc__ and "deserialization" in serial.__doc__:
        raise ImportError("""
esptool.py depends on pyserial, but there is a conflict with a currently installed package named 'serial'.

You may be able to work around this by 'pip uninstall serial; pip install pyserial' \
but this may break other installed Python software that depends on 'serial'.

There is no good fix for this right now, apart from configuring virtualenvs. \
See https://github.com/espressif/esptool/issues/269#issuecomment-385298196 for discussion of the underlying issue(s).""")
except TypeError:
    pass  # __doc__ returns None for pyserial

try:
    import serial.tools.list_ports as list_ports
except ImportError:
    print("The installed version (%s) of pyserial appears to be too old for esptool.py (Python interpreter %s). "
          "Check the README for installation instructions." % (sys.VERSION, sys.executable))
    raise
except Exception:
    if sys.platform == "darwin":
        # swallow the exception, this is a known issue in pyserial+macOS Big Sur preview ref https://github.com/espressif/esptool/issues/540
        list_ports = None
    else:
        raise


__version__ = "3.4.1"

MAX_UINT32 = 0xffffffff
MAX_UINT24 = 0xffffff

DEFAULT_TIMEOUT = 3                   # timeout for most flash operations
START_FLASH_TIMEOUT = 20              # timeout for starting flash (may perform erase)
CHIP_ERASE_TIMEOUT = 120              # timeout for full chip erase
MAX_TIMEOUT = CHIP_ERASE_TIMEOUT * 2  # longest any command can run
SYNC_TIMEOUT = 0.1                    # timeout for syncing with bootloader
MD5_TIMEOUT_PER_MB = 8                # timeout (per megabyte) for calculating md5sum
ERASE_REGION_TIMEOUT_PER_MB = 30      # timeout (per megabyte) for erasing a region
ERASE_WRITE_TIMEOUT_PER_MB = 40       # timeout (per megabyte) for erasing and writing data
MEM_END_ROM_TIMEOUT = 0.05            # special short timeout for ESP_MEM_END, as it may never respond
DEFAULT_SERIAL_WRITE_TIMEOUT = 10     # timeout for serial port write
DEFAULT_CONNECT_ATTEMPTS = 7          # default number of times to try connection
WRITE_BLOCK_ATTEMPTS = 3              # number of times to try writing a data block

SUPPORTED_CHIPS = ['esp8266', 'esp32', 'esp32s2', 'esp32s3', 'esp32c3', 'esp32c6', 'esp32h2', 'esp32c2']


def timeout_per_mb(seconds_per_mb, size_bytes):
    """ Scales timeouts which are size-specific """
    result = seconds_per_mb * (size_bytes / 1e6)
    if result < DEFAULT_TIMEOUT:
        return DEFAULT_TIMEOUT
    return result


def _chip_to_rom_loader(chip):
    return {
        'esp8266': ESP8266ROM,
        'esp32': ESP32ROM,
        'esp32s2': ESP32S2ROM,
        'esp32s3': ESP32S3ROM,
        'esp32c3': ESP32C3ROM,
        'esp32c6': ESP32C6ROM,
        'esp32h2': ESP32H2ROM,
        'esp32c2': ESP32C2ROM,
    }[chip]


def get_default_connected_device(serial_list, port, connect_attempts, initial_baud, chip='auto', trace=False,
                                 before='default_reset'):
    _esp = None
    for each_port in reversed(serial_list):
        print("Serial port %s" % each_port)
        try:
            if chip == 'auto':
                _esp = ESPLoader.detect_chip(each_port, initial_baud, before, trace,
                                             connect_attempts)
            else:
                chip_class = _chip_to_rom_loader(chip)
                _esp = chip_class(each_port, initial_baud, trace)
                _esp.connect(before, connect_attempts)
            break
        except (FatalError, OSError) as err:
            if port is not None:
                raise
            print("%s failed to connect: %s" % (each_port, err))
            if _esp and _esp._port:
                _esp._port.close()
            _esp = None
    return _esp


DETECTED_FLASH_SIZES = {
    0x12: "256KB",
    0x13: "512KB",
    0x14: "1MB",
    0x15: "2MB",
    0x16: "4MB",
    0x17: "8MB",
    0x18: "16MB",
    0x19: "32MB",
    0x1A: "64MB",
    0x1B: "128MB",
    0x1C: "256MB",
    0x20: "64MB",
    0x21: "128MB",
    0x22: "256MB",
    0x32: "256KB",
    0x33: "512KB",
    0x34: "1MB",
    0x35: "2MB",
    0x36: "4MB",
    0x37: "8MB",
    0x38: "16MB",
    0x39: "32MB",
    0x3A: "64MB",
}


def check_supported_function(func, check_func):
    """
    Decorator implementation that wraps a check around an ESPLoader
    bootloader function to check if it's supported.

    This is used to capture the multidimensional differences in
    functionality between the ESP8266 & ESP32 (and later chips) ROM loaders, and the
    software stub that runs on these. Not possible to do this cleanly
    via inheritance alone.
    """
    def inner(*args, **kwargs):
        obj = args[0]
        if check_func(obj):
            return func(*args, **kwargs)
        else:
            raise NotImplementedInROMError(obj, func)
    return inner


def esp8266_function_only(func):
    """ Attribute for a function only supported on ESP8266 """
    return check_supported_function(func, lambda o: o.CHIP_NAME == "ESP8266")


def stub_function_only(func):
    """ Attribute for a function only supported in the software stub loader """
    return check_supported_function(func, lambda o: o.IS_STUB)


def stub_and_esp32_function_only(func):
    """ Attribute for a function only supported by software stubs or ESP32 and later chips ROM """
    return check_supported_function(func, lambda o: o.IS_STUB or isinstance(o, ESP32ROM))


def esp32s3_or_newer_function_only(func):
    """ Attribute for a function only supported by ESP32S3 and later chips ROM """
    return check_supported_function(func, lambda o: isinstance(o, ESP32S3ROM) or isinstance(o, ESP32C3ROM))


PYTHON2 = sys.version_info[0] < 3  # True if on pre-Python 3

# Function to return nth byte of a bitstring
# Different behaviour on Python 2 vs 3
if PYTHON2:
    def byte(bitstr, index):
        return ord(bitstr[index])
else:
    def byte(bitstr, index):
        return bitstr[index]

# Provide a 'basestring' class on Python 3
try:
    basestring
except NameError:
    basestring = str


def print_overwrite(message, last_line=False):
    """ Print a message, overwriting the currently printed line.

    If last_line is False, don't append a newline at the end (expecting another subsequent call will overwrite this one.)

    After a sequence of calls with last_line=False, call once with last_line=True.

    If output is not a TTY (for example redirected a pipe), no overwriting happens and this function is the same as print().
    """
    if sys.stdout.isatty():
        print("\r%s" % message, end='\n' if last_line else '')
    else:
        print(message)


def _mask_to_shift(mask):
    """ Return the index of the least significant bit in the mask """
    shift = 0
    while mask & 0x1 == 0:
        shift += 1
        mask >>= 1
    return shift


class ESPLoader(object):
    """ Base class providing access to ESP ROM & software stub bootloaders.
    Subclasses provide ESP8266 & ESP32 Family specific functionality.

    Don't instantiate this base class directly, either instantiate a subclass or
    call ESPLoader.detect_chip() which will interrogate the chip and return the
    appropriate subclass instance.

    """
    CHIP_NAME = "Espressif device"
    IS_STUB = False

    FPGA_SLOW_BOOT = False

    DEFAULT_PORT = "/dev/ttyUSB0"

    USES_RFC2217 = False

    # Commands supported by ESP8266 ROM bootloader
    ESP_FLASH_BEGIN = 0x02
    ESP_FLASH_DATA = 0x03
    ESP_FLASH_END = 0x04
    ESP_MEM_BEGIN = 0x05
    ESP_MEM_END = 0x06
    ESP_MEM_DATA = 0x07
    ESP_SYNC = 0x08
    ESP_WRITE_REG = 0x09
    ESP_READ_REG = 0x0a

    # Some comands supported by ESP32 and later chips ROM bootloader (or -8266 w/ stub)
    ESP_SPI_SET_PARAMS = 0x0B
    ESP_SPI_ATTACH = 0x0D
    ESP_READ_FLASH_SLOW = 0x0e  # ROM only, much slower than the stub flash read
    ESP_CHANGE_BAUDRATE = 0x0F
    ESP_FLASH_DEFL_BEGIN = 0x10
    ESP_FLASH_DEFL_DATA = 0x11
    ESP_FLASH_DEFL_END = 0x12
    ESP_SPI_FLASH_MD5 = 0x13

    # Commands supported by ESP32-S2 and later chips ROM bootloader only
    ESP_GET_SECURITY_INFO = 0x14

    # Some commands supported by stub only
    ESP_ERASE_FLASH = 0xD0
    ESP_ERASE_REGION = 0xD1
    ESP_READ_FLASH = 0xD2
    ESP_RUN_USER_CODE = 0xD3

    # Flash encryption encrypted data command
    ESP_FLASH_ENCRYPT_DATA = 0xD4

    # Response code(s) sent by ROM
    ROM_INVALID_RECV_MSG = 0x05   # response if an invalid message is received

    # Maximum block sized for RAM and Flash writes, respectively.
    ESP_RAM_BLOCK = 0x1800

    FLASH_WRITE_SIZE = 0x400

    # Default baudrate. The ROM auto-bauds, so we can use more or less whatever we want.
    ESP_ROM_BAUD = 115200

    # First byte of the application image
    ESP_IMAGE_MAGIC = 0xe9

    # Initial state for the checksum routine
    ESP_CHECKSUM_MAGIC = 0xef

    # Flash sector size, minimum unit of erase.
    FLASH_SECTOR_SIZE = 0x1000

    UART_DATE_REG_ADDR = 0x60000078

    CHIP_DETECT_MAGIC_REG_ADDR = 0x40001000  # This ROM address has a different value on each chip model

    UART_CLKDIV_MASK = 0xFFFFF

    # Memory addresses
    IROM_MAP_START = 0x40200000
    IROM_MAP_END = 0x40300000

    # The number of bytes in the UART response that signify command status
    STATUS_BYTES_LENGTH = 2

    # Response to ESP_SYNC might indicate that flasher stub is running instead of the ROM bootloader
    sync_stub_detected = False

    # Device PIDs
    USB_JTAG_SERIAL_PID = 0x1001

    # Chip IDs that are no longer supported by esptool
    UNSUPPORTED_CHIPS = {6: "ESP32-S3(beta 3)"}

    def __init__(self, port=DEFAULT_PORT, baud=ESP_ROM_BAUD, trace_enabled=False):
        """Base constructor for ESPLoader bootloader interaction

        Don't call this constructor, either instantiate ESP8266ROM
        or ESP32ROM, or use ESPLoader.detect_chip().

        This base class has all of the instance methods for bootloader
        functionality supported across various chips & stub
        loaders. Subclasses replace the functions they don't support
        with ones which throw NotImplementedInROMError().

        """
        self.secure_download_mode = False  # flag is set to True if esptool detects the ROM is in Secure Download Mode
        self.stub_is_disabled = False  # flag is set to True if esptool detects conditions which require the stub to be disabled

        if isinstance(port, basestring):
            self._port = serial.serial_for_url(port)
        else:
            self._port = port
        self._slip_reader = slip_reader(self._port, self.trace)
        # setting baud rate in a separate step is a workaround for
        # CH341 driver on some Linux versions (this opens at 9600 then
        # sets), shouldn't matter for other platforms/drivers. See
        # https://github.com/espressif/esptool/issues/44#issuecomment-107094446
        self._set_port_baudrate(baud)
        self._trace_enabled = trace_enabled
        # set write timeout, to prevent esptool blocked at write forever.
        try:
            self._port.write_timeout = DEFAULT_SERIAL_WRITE_TIMEOUT
        except NotImplementedError:
            # no write timeout for RFC2217 ports
            # need to set the property back to None or it will continue to fail
            self._port.write_timeout = None

    @property
    def serial_port(self):
        return self._port.port

    def _set_port_baudrate(self, baud):
        try:
            self._port.baudrate = baud
        except IOError:
            raise FatalError("Failed to set baud rate %d. The driver may not support this rate." % baud)

    @staticmethod
    def detect_chip(port=DEFAULT_PORT, baud=ESP_ROM_BAUD, connect_mode='default_reset', trace_enabled=False,
                    connect_attempts=DEFAULT_CONNECT_ATTEMPTS):
        """ Use serial access to detect the chip type.

        First, get_security_info command is sent to detect the ID of the chip
        (supported only by ESP32-C3 and later, works even in the Secure Download Mode).
        If this fails, we reconnect and fall-back to reading the magic number.
        It's mapped at a specific ROM address and has a different value on each chip model.
        This way we can use one memory read and compare it to the magic number for each chip type.

        This routine automatically performs ESPLoader.connect() (passing
        connect_mode parameter) as part of querying the chip.
        """
        inst = None
        detect_port = ESPLoader(port, baud, trace_enabled=trace_enabled)
        if detect_port.serial_port.startswith("rfc2217:"):
            detect_port.USES_RFC2217 = True
        detect_port.connect(connect_mode, connect_attempts, detecting=True)
        try:
            print('Detecting chip type...', end='')
            chip_magic_value = detect_port.read_reg(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR)

            for cls in [ESP8266ROM, ESP32ROM, ESP32S2ROM, ESP32S3ROM,
                        ESP32C3ROM, ESP32C6ROM, ESP32C2ROM, ESP32H2ROM]:
                if chip_magic_value in cls.CHIP_DETECT_MAGIC_VALUE:
                    inst = cls(detect_port._port, baud, trace_enabled=trace_enabled)
                    inst._post_connect()
                    inst.check_chip_id()
        except UnsupportedCommandError:
            raise FatalError("Unsupported Command Error received. Probably this means Secure Download Mode is enabled, "
                             "autodetection will not work. Need to manually specify the chip.")
        finally:
            if inst is not None:
                print(' %s' % inst.CHIP_NAME, end='')
                if detect_port.sync_stub_detected:
                    inst = inst.STUB_CLASS(inst)
                    inst.sync_stub_detected = True
                print('')  # end line
                return inst
        raise FatalError("Unexpected CHIP magic value 0x%08x. Failed to autodetect chip type." % (chip_magic_value))

    """ Read a SLIP packet from the serial port """

    def read(self):
        return next(self._slip_reader)

    """ Write bytes to the serial port while performing SLIP escaping """

    def write(self, packet):
        buf = b'\xc0' \
              + (packet.replace(b'\xdb', b'\xdb\xdd').replace(b'\xc0', b'\xdb\xdc')) \
              + b'\xc0'
        self.trace("Write %d bytes: %s", len(buf), HexFormatter(buf))
        self._port.write(buf)

    def trace(self, message, *format_args):
        if self._trace_enabled:
            now = time.time()
            try:

                delta = now - self._last_trace
            except AttributeError:
                delta = 0.0
            self._last_trace = now
            prefix = "TRACE +%.3f " % delta
            print(prefix + (message % format_args))

    """ Calculate checksum of a blob, as it is defined by the ROM """
    @staticmethod
    def checksum(data, state=ESP_CHECKSUM_MAGIC):
        for b in data:
            if type(b) is int:  # python 2/3 compat
                state ^= b
            else:
                state ^= ord(b)

        return state

    """ Send a request and read the response """

    def command(self, op=None, data=b"", chk=0, wait_response=True, timeout=DEFAULT_TIMEOUT):
        saved_timeout = self._port.timeout
        new_timeout = min(timeout, MAX_TIMEOUT)
        if new_timeout != saved_timeout:
            self._port.timeout = new_timeout

        try:
            if op is not None:
                self.trace("command op=0x%02x data len=%s wait_response=%d timeout=%.3f data=%s",
                           op, len(data), 1 if wait_response else 0, timeout, HexFormatter(data))
                pkt = struct.pack(b'<BBHI', 0x00, op, len(data), chk) + data
                self.write(pkt)

            if not wait_response:
                return

            # tries to get a response until that response has the
            # same operation as the request or a retries limit has
            # exceeded. This is needed for some esp8266s that
            # reply with more sync responses than expected.
            for retry in range(100):
                p = self.read()
                if len(p) < 8:
                    continue
                (resp, op_ret, len_ret, val) = struct.unpack('<BBHI', p[:8])
                if resp != 1:
                    continue
                data = p[8:]

                if op is None or op_ret == op:
                    return val, data
                if byte(data, 0) != 0 and byte(data, 1) == self.ROM_INVALID_RECV_MSG:
                    self.flush_input()  # Unsupported read_reg can result in more than one error response for some reason
                    raise UnsupportedCommandError(self, op)

        finally:
            if new_timeout != saved_timeout:
                self._port.timeout = saved_timeout

        raise FatalError("Response doesn't match request")

    def check_command(self, op_description, op=None, data=b'', chk=0, timeout=DEFAULT_TIMEOUT):
        """
        Execute a command with 'command', check the result code and throw an appropriate
        FatalError if it fails.

        Returns the "result" of a successful command.
        """
        val, data = self.command(op, data, chk, timeout=timeout)

        # things are a bit weird here, bear with us

        # the status bytes are the last 2/4 bytes in the data (depending on chip)
        if len(data) < self.STATUS_BYTES_LENGTH:
            raise FatalError("Failed to %s. Only got %d byte status response." % (op_description, len(data)))
        status_bytes = data[-self.STATUS_BYTES_LENGTH:]
        # we only care if the first one is non-zero. If it is, the second byte is a reason.
        if byte(status_bytes, 0) != 0:
            raise FatalError.WithResult('Failed to %s' % op_description, status_bytes)

        # if we had more data than just the status bytes, return it as the result
        # (this is used by the md5sum command, maybe other commands?)
        if len(data) > self.STATUS_BYTES_LENGTH:
            return data[:-self.STATUS_BYTES_LENGTH]
        else:  # otherwise, just return the 'val' field which comes from the reply header (this is used by read_reg)
            return val

    def flush_input(self):
        self._port.flushInput()
        self._slip_reader = slip_reader(self._port, self.trace)

    def sync(self):
        val, _ = self.command(self.ESP_SYNC, b'\x07\x07\x12\x20' + 32 * b'\x55',
                              timeout=SYNC_TIMEOUT)

        # ROM bootloaders send some non-zero "val" response. The flasher stub sends 0. If we receive 0 then it
        # probably indicates that the chip wasn't or couldn't be reseted properly and esptool is talking to the
        # flasher stub.
        self.sync_stub_detected = val == 0

        for _ in range(7):
            val, _ = self.command()
            self.sync_stub_detected &= val == 0

    def _setDTR(self, state):
        self._port.setDTR(state)

    def _setRTS(self, state):
        self._port.setRTS(state)
        # Work-around for adapters on Windows using the usbser.sys driver:
        # generate a dummy change to DTR so that the set-control-line-state
        # request is sent with the updated RTS state and the same DTR state
        self._port.setDTR(self._port.dtr)

    def _get_pid(self):
        if list_ports is None:
            print("\nListing all serial ports is currently not available. Can't get device PID.")
            return
        active_port = self._port.port

        # Pyserial only identifies regular ports, URL handlers are not supported
        if not active_port.lower().startswith(("com", "/dev/")):
            print("\nDevice PID identification is only supported on COM and /dev/ serial ports.")
            return
        # Return the real path if the active port is a symlink
        if active_port.startswith("/dev/") and os.path.islink(active_port):
            active_port = os.path.realpath(active_port)

        # The "cu" (call-up) device has to be used for outgoing communication on MacOS
        if sys.platform == "darwin" and "tty" in active_port:
            active_port = [active_port, active_port.replace("tty", "cu")]
        ports = list_ports.comports()
        for p in ports:
            if p.device in active_port:
                return p.pid
        print("\nFailed to get PID of a device on {}, using standard reset sequence.".format(active_port))

    def bootloader_reset(self, usb_jtag_serial=False, extra_delay=False):
        """ Issue a reset-to-bootloader, with USB-JTAG-Serial custom reset sequence option
        """
        # RTS = either CH_PD/EN or nRESET (both active low = chip in reset)
        # DTR = GPIO0 (active low = boot to flasher)
        #
        # DTR & RTS are active low signals,
        # ie True = pin @ 0V, False = pin @ VCC.
        if usb_jtag_serial:
            # Custom reset sequence, which is required when the device
            # is connecting via its USB-JTAG-Serial peripheral
            self._setRTS(False)
            self._setDTR(False)  # Idle
            time.sleep(0.1)
            self._setDTR(True)  # Set IO0
            self._setRTS(False)
            time.sleep(0.1)
            self._setRTS(True)  # Reset. Note dtr/rts calls inverted so we go through (1,1) instead of (0,0)
            self._setDTR(False)
            self._setRTS(True)  # Extra RTS set for RTS as Windows only propagates DTR on RTS setting
            time.sleep(0.1)
            self._setDTR(False)
            self._setRTS(False)
        else:
            # This fpga delay is for Espressif internal use
            fpga_delay = True if self.FPGA_SLOW_BOOT and os.environ.get(
                "ESPTOOL_ENV_FPGA", "").strip() == "1" else False
            delay = 7 if fpga_delay else 0.5 if extra_delay else 0.05  # 0.5 needed for ESP32 rev0 and rev1

            self._setDTR(False)  # IO0=HIGH
            self._setRTS(True)   # EN=LOW, chip in reset
            time.sleep(0.1)
            self._setDTR(True)   # IO0=LOW
            self._setRTS(False)  # EN=HIGH, chip out of reset
            time.sleep(delay)
            self._setDTR(False)  # IO0=HIGH, done

    def _connect_attempt(self, mode='default_reset', usb_jtag_serial=False, extra_delay=False):
        """ A single connection attempt """
        last_error = None
        boot_log_detected = False
        download_mode = False

        # If we're doing no_sync, we're likely communicating as a pass through
        # with an intermediate device to the ESP32
        if mode == "no_reset_no_sync":
            return last_error

        if mode != 'no_reset':
            if not self.USES_RFC2217:  # Might block on rfc2217 ports
                self._port.reset_input_buffer()  # Empty serial buffer to isolate boot log
            self.bootloader_reset(usb_jtag_serial, extra_delay)

            # Detect the ROM boot log and check actual boot mode (ESP32 and later only)
            waiting = self._port.inWaiting()
            read_bytes = self._port.read(waiting)
            data = re.search(b'boot:(0x[0-9a-fA-F]+)(.*waiting for download)?', read_bytes, re.DOTALL)
            if data is not None:
                boot_log_detected = True
                boot_mode = data.group(1)
                download_mode = data.group(2) is not None

        for _ in range(5):
            try:
                self.flush_input()
                self._port.flushOutput()
                self.sync()
                return None
            except FatalError as e:
                print('.', end='')
                sys.stdout.flush()
                time.sleep(0.05)
                last_error = e

        if boot_log_detected:
            last_error = FatalError(
                "Wrong boot mode detected ({})! The chip needs to be in download mode.".format(boot_mode.decode("utf-8")))
            if download_mode:
                last_error = FatalError(
                    "Download mode successfully detected, but getting no sync reply: The serial TX path seems to be down.")
        return last_error

    def get_memory_region(self, name):
        """ Returns a tuple of (start, end) for the memory map entry with the given name, or None if it doesn't exist
        """
        try:
            return [(start, end) for (start, end, n) in self.MEMORY_MAP if n == name][0]
        except IndexError:
            return None

    def connect(self, mode='default_reset', attempts=DEFAULT_CONNECT_ATTEMPTS, detecting=False, warnings=True):
        """ Try connecting repeatedly until successful, or giving up """
        if warnings and mode in ['no_reset', 'no_reset_no_sync']:
            print('WARNING: Pre-connection option "{}" was selected.'.format(mode),
                  'Connection may fail if the chip is not in bootloader or flasher stub mode.')
        print('Connecting...', end='')
        sys.stdout.flush()
        last_error = None

        usb_jtag_serial = (mode == 'usb_reset') or (self._get_pid() == self.USB_JTAG_SERIAL_PID)

        try:
            for _, extra_delay in zip(range(attempts) if attempts > 0 else itertools.count(), itertools.cycle((False, True))):
                last_error = self._connect_attempt(mode=mode, usb_jtag_serial=usb_jtag_serial, extra_delay=extra_delay)
                if last_error is None:
                    break
        finally:
            print('')  # end 'Connecting...' line

        if last_error is not None:
            raise FatalError('Failed to connect to {}: {}'
                             '\nFor troubleshooting steps visit: '
                             'https://docs.espressif.com/projects/esptool/en/latest/troubleshooting.html'.format(self.CHIP_NAME, last_error))

        if not detecting:
            try:
                # check the date code registers match what we expect to see
                chip_magic_value = self.read_reg(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR)
                if chip_magic_value not in self.CHIP_DETECT_MAGIC_VALUE:
                    actually = None
                    for cls in [ESP8266ROM, ESP32ROM, ESP32S2ROM, ESP32S3ROM,
                                ESP32C3ROM, ESP32H2ROM, ESP32C2ROM, ESP32C6ROM]:
                        if chip_magic_value in cls.CHIP_DETECT_MAGIC_VALUE:
                            actually = cls
                            break
                    if warnings and actually is None:
                        print(("WARNING: This chip doesn't appear to be a %s (chip magic value 0x%08x). "
                               "Probably it is unsupported by this version of esptool.") % (self.CHIP_NAME, chip_magic_value))
                    else:
                        raise FatalError("This chip is %s not %s. Wrong --chip argument?" %
                                         (actually.CHIP_NAME, self.CHIP_NAME))
            except UnsupportedCommandError:
                self.secure_download_mode = True
            self._post_connect()
            self.check_chip_id()

    def _post_connect(self):
        """
        Additional initialization hook, may be overridden by the chip-specific class.
        Gets called after connect, and after auto-detection.
        """
        pass

    def read_reg(self, addr, timeout=DEFAULT_TIMEOUT):
        """ Read memory address in target """
        # we don't call check_command here because read_reg() function is called
        # when detecting chip type, and the way we check for success (STATUS_BYTES_LENGTH) is different
        # for different chip types (!)
        val, data = self.command(self.ESP_READ_REG, struct.pack('<I', addr), timeout=timeout)
        if byte(data, 0) != 0:
            raise FatalError.WithResult("Failed to read register address %08x" % addr, data)
        return val

    """ Write to memory address in target """

    def write_reg(self, addr, value, mask=0xFFFFFFFF, delay_us=0, delay_after_us=0):
        command = struct.pack('<IIII', addr, value, mask, delay_us)
        if delay_after_us > 0:
            # add a dummy write to a date register as an excuse to have a delay
            command += struct.pack('<IIII', self.UART_DATE_REG_ADDR, 0, 0, delay_after_us)

        return self.check_command("write target memory", self.ESP_WRITE_REG, command)

    def update_reg(self, addr, mask, new_val):
        """ Update register at 'addr', replace the bits masked out by 'mask'
        with new_val. new_val is shifted left to match the LSB of 'mask'

        Returns just-written value of register.
        """
        shift = _mask_to_shift(mask)
        val = self.read_reg(addr)
        val &= ~mask
        val |= (new_val << shift) & mask
        self.write_reg(addr, val)

        return val

    """ Start downloading an application image to RAM """

    def mem_begin(self, size, blocks, blocksize, offset):
        if self.IS_STUB:  # check we're not going to overwrite a running stub with this data
            stub = self.STUB_CODE
            load_start = offset
            load_end = offset + size
            for (start, end) in [(stub["data_start"], stub["data_start"] + len(stub["data"])),
                                 (stub["text_start"], stub["text_start"] + len(stub["text"]))]:
                if load_start < end and load_end > start:
                    raise FatalError(("Software loader is resident at 0x%08x-0x%08x. "
                                      "Can't load binary at overlapping address range 0x%08x-0x%08x. "
                                      "Either change binary loading address, or use the --no-stub "
                                      "option to disable the software loader.") % (start, end, load_start, load_end))

        return self.check_command("enter RAM download mode", self.ESP_MEM_BEGIN,
                                  struct.pack('<IIII', size, blocks, blocksize, offset))

    """ Send a block of an image to RAM """

    def mem_block(self, data, seq):
        return self.check_command("write to target RAM", self.ESP_MEM_DATA,
                                  struct.pack('<IIII', len(data), seq, 0, 0) + data,
                                  self.checksum(data))

    """ Leave download mode and run the application """

    def mem_finish(self, entrypoint=0):
        # Sending ESP_MEM_END usually sends a correct response back, however sometimes
        # (with ROM loader) the executed code may reset the UART or change the baud rate
        # before the transmit FIFO is empty. So in these cases we set a short timeout and
        # ignore errors.
        timeout = DEFAULT_TIMEOUT if self.IS_STUB else MEM_END_ROM_TIMEOUT
        data = struct.pack('<II', int(entrypoint == 0), entrypoint)
        try:
            return self.check_command("leave RAM download mode", self.ESP_MEM_END,
                                      data=data, timeout=timeout)
        except FatalError:
            if self.IS_STUB:
                raise
            pass

    """ Start downloading to Flash (performs an erase)

    Returns number of blocks (of size self.FLASH_WRITE_SIZE) to write.
    """

    def flash_begin(self, size, offset, begin_rom_encrypted=False):
        num_blocks = (size + self.FLASH_WRITE_SIZE - 1) // self.FLASH_WRITE_SIZE
        erase_size = self.get_erase_size(offset, size)

        t = time.time()
        if self.IS_STUB:
            timeout = DEFAULT_TIMEOUT
        else:
            timeout = timeout_per_mb(ERASE_REGION_TIMEOUT_PER_MB, size)  # ROM performs the erase up front

        params = struct.pack('<IIII', erase_size, num_blocks, self.FLASH_WRITE_SIZE, offset)
        if isinstance(self, (ESP32S2ROM, ESP32S3ROM, ESP32C3ROM,
                             ESP32C6ROM, ESP32H2ROM, ESP32C2ROM)) and not self.IS_STUB:
            params += struct.pack('<I', 1 if begin_rom_encrypted else 0)
        self.check_command("enter Flash download mode", self.ESP_FLASH_BEGIN,
                           params, timeout=timeout)
        if size != 0 and not self.IS_STUB:
            print("Took %.2fs to erase flash block" % (time.time() - t))
        return num_blocks

    def flash_block(self, data, seq, timeout=DEFAULT_TIMEOUT):
        """Write block to flash, retry if fail"""
        for attempts_left in range(WRITE_BLOCK_ATTEMPTS - 1, -1, -1):
            try:
                self.check_command(
                    "write to target Flash after seq %d" % seq,
                    self.ESP_FLASH_DATA,
                    struct.pack("<IIII", len(data), seq, 0, 0) + data,
                    self.checksum(data),
                    timeout=timeout,
                )
                break
            except FatalError:
                if attempts_left:
                    self.trace(
                        "Block write failed, "
                        "retrying with {} attempts left".format(attempts_left)
                    )
                else:
                    raise

    def flash_encrypt_block(self, data, seq, timeout=DEFAULT_TIMEOUT):
        """Encrypt, write block to flash, retry if fail"""
        if isinstance(self, (ESP32S2ROM, ESP32C3ROM, ESP32S3ROM, ESP32H2ROM, ESP32C2ROM)) and not self.IS_STUB:
            # ROM support performs the encrypted writes via the normal write command,
            # triggered by flash_begin(begin_rom_encrypted=True)
            return self.flash_block(data, seq, timeout)

        for attempts_left in range(WRITE_BLOCK_ATTEMPTS - 1, -1, -1):
            try:
                self.check_command(
                    "Write encrypted to target Flash after seq %d" % seq,
                    self.ESP_FLASH_ENCRYPT_DATA,
                    struct.pack("<IIII", len(data), seq, 0, 0) + data,
                    self.checksum(data),
                    timeout=timeout,
                )
                break
            except FatalError:
                if attempts_left:
                    self.trace(
                        "Encrypted block write failed, "
                        "retrying with {} attempts left".format(attempts_left)
                    )
                else:
                    raise

    """ Leave flash mode and run/reboot """

    def flash_finish(self, reboot=False):
        pkt = struct.pack('<I', int(not reboot))
        # stub sends a reply to this command
        self.check_command("leave Flash mode", self.ESP_FLASH_END, pkt)

    """ Run application code in flash """

    def run(self, reboot=False):
        # Fake flash begin immediately followed by flash end
        self.flash_begin(0, 0)
        self.flash_finish(reboot)

    """ Read SPI flash manufacturer and device id """

    def flash_id(self):
        SPIFLASH_RDID = 0x9F
        return self.run_spiflash_command(SPIFLASH_RDID, b"", 24)

    def get_security_info(self):
        res = self.check_command('get security info', self.ESP_GET_SECURITY_INFO, b'')
        esp32s2 = True if len(res) == 12 else False
        res = struct.unpack("<IBBBBBBBB" if esp32s2 else "<IBBBBBBBBII", res)
        return {
            "flags": res[0],
            "flash_crypt_cnt": res[1],
            "key_purposes": res[2:9],
            "chip_id": None if esp32s2 else res[9],
            "api_version": None if esp32s2 else res[10],
        }

    @esp32s3_or_newer_function_only
    def get_chip_id(self):
        res = self.check_command('get security info', self.ESP_GET_SECURITY_INFO, b'')
        res = struct.unpack("<IBBBBBBBBI", res[:16])  # 4b flags, 1b flash_crypt_cnt, 7*1b key_purposes, 4b chip_id
        chip_id = res[9]  # 2/4 status bytes invariant
        return chip_id

    @classmethod
    def parse_flash_size_arg(cls, arg):
        try:
            return cls.FLASH_SIZES[arg]
        except KeyError:
            raise FatalError("Flash size '%s' is not supported by this chip type. Supported sizes: %s"
                             % (arg, ", ".join(cls.FLASH_SIZES.keys())))

    @classmethod
    def parse_flash_freq_arg(cls, arg):
        try:
            return cls.FLASH_FREQUENCY[arg]
        except KeyError:
            raise FatalError("Flash frequency '%s' is not supported by this chip type. Supported frequencies: %s"
                             % (arg, ", ".join(cls.FLASH_FREQUENCY.keys())))

    def run_stub(self, stub=None):
        if stub is None:
            stub = self.STUB_CODE

        if self.sync_stub_detected:
            print("Stub is already running. No upload is necessary.")
            return self.STUB_CLASS(self)

        # Upload
        print("Uploading stub...")
        for field in ['text', 'data']:
            if field in stub:
                offs = stub[field + "_start"]
                length = len(stub[field])
                blocks = (length + self.ESP_RAM_BLOCK - 1) // self.ESP_RAM_BLOCK
                self.mem_begin(length, blocks, self.ESP_RAM_BLOCK, offs)
                for seq in range(blocks):
                    from_offs = seq * self.ESP_RAM_BLOCK
                    to_offs = from_offs + self.ESP_RAM_BLOCK
                    self.mem_block(stub[field][from_offs:to_offs], seq)
        print("Running stub...")
        self.mem_finish(stub['entry'])

        p = self.read()
        if p != b'OHAI':
            raise FatalError("Failed to start stub. Unexpected response: %s" % p)
        print("Stub running...")
        return self.STUB_CLASS(self)

    @stub_and_esp32_function_only
    def flash_defl_begin(self, size, compsize, offset):
        """ Start downloading compressed data to Flash (performs an erase)

        Returns number of blocks (size self.FLASH_WRITE_SIZE) to write.
        """
        num_blocks = (compsize + self.FLASH_WRITE_SIZE - 1) // self.FLASH_WRITE_SIZE
        erase_blocks = (size + self.FLASH_WRITE_SIZE - 1) // self.FLASH_WRITE_SIZE

        t = time.time()
        if self.IS_STUB:
            write_size = size  # stub expects number of bytes here, manages erasing internally
            timeout = DEFAULT_TIMEOUT
        else:
            write_size = erase_blocks * self.FLASH_WRITE_SIZE  # ROM expects rounded up to erase block size
            timeout = timeout_per_mb(ERASE_REGION_TIMEOUT_PER_MB, write_size)  # ROM performs the erase up front
        print("Compressed %d bytes to %d..." % (size, compsize))
        params = struct.pack('<IIII', write_size, num_blocks, self.FLASH_WRITE_SIZE, offset)
        if isinstance(self, (ESP32S2ROM, ESP32S3ROM, ESP32C3ROM,
                             ESP32C6ROM, ESP32H2ROM, ESP32C2ROM)) and not self.IS_STUB:
            # extra param is to enter encrypted flash mode via ROM (not supported currently)
            params += struct.pack('<I', 0)
        self.check_command("enter compressed flash mode", self.ESP_FLASH_DEFL_BEGIN, params, timeout=timeout)
        if size != 0 and not self.IS_STUB:
            # (stub erases as it writes, but ROM loaders erase on begin)
            print("Took %.2fs to erase flash block" % (time.time() - t))
        return num_blocks

    @stub_and_esp32_function_only
    def flash_defl_block(self, data, seq, timeout=DEFAULT_TIMEOUT):
        """Write block to flash, send compressed, retry if fail"""
        for attempts_left in range(WRITE_BLOCK_ATTEMPTS - 1, -1, -1):
            try:
                self.check_command(
                    "write compressed data to flash after seq %d" % seq,
                    self.ESP_FLASH_DEFL_DATA,
                    struct.pack("<IIII", len(data), seq, 0, 0) + data,
                    self.checksum(data),
                    timeout=timeout,
                )
                break
            except FatalError:
                if attempts_left:
                    self.trace(
                        "Compressed block write failed, "
                        "retrying with {} attempts left".format(attempts_left)
                    )
                else:
                    raise

    """ Leave compressed flash mode and run/reboot """
    @stub_and_esp32_function_only
    def flash_defl_finish(self, reboot=False):
        if not reboot and not self.IS_STUB:
            # skip sending flash_finish to ROM loader, as this
            # exits the bootloader. Stub doesn't do this.
            return
        pkt = struct.pack('<I', int(not reboot))
        self.check_command("leave compressed flash mode", self.ESP_FLASH_DEFL_END, pkt)
        self.in_bootloader = False

    @stub_and_esp32_function_only
    def flash_md5sum(self, addr, size):
        # the MD5 command returns additional bytes in the standard
        # command reply slot
        timeout = timeout_per_mb(MD5_TIMEOUT_PER_MB, size)
        res = self.check_command('calculate md5sum', self.ESP_SPI_FLASH_MD5, struct.pack('<IIII', addr, size, 0, 0),
                                 timeout=timeout)

        if len(res) == 32:
            return res.decode("utf-8")  # already hex formatted
        elif len(res) == 16:
            return hexify(res).lower()
        else:
            raise FatalError("MD5Sum command returned unexpected result: %r" % res)

    @stub_and_esp32_function_only
    def change_baud(self, baud):
        print("Changing baud rate to %d" % baud)
        # stub takes the new baud rate and the old one
        second_arg = self._port.baudrate if self.IS_STUB else 0
        self.command(self.ESP_CHANGE_BAUDRATE, struct.pack('<II', baud, second_arg))
        print("Changed.")
        self._set_port_baudrate(baud)
        time.sleep(0.05)  # get rid of crap sent during baud rate change
        self.flush_input()

    @stub_function_only
    def erase_flash(self):
        # depending on flash chip model the erase may take this long (maybe longer!)
        self.check_command("erase flash", self.ESP_ERASE_FLASH,
                           timeout=CHIP_ERASE_TIMEOUT)

    @stub_function_only
    def erase_region(self, offset, size):
        if offset % self.FLASH_SECTOR_SIZE != 0:
            raise FatalError("Offset to erase from must be a multiple of 4096")
        if size % self.FLASH_SECTOR_SIZE != 0:
            raise FatalError("Size of data to erase must be a multiple of 4096")
        timeout = timeout_per_mb(ERASE_REGION_TIMEOUT_PER_MB, size)
        self.check_command("erase region", self.ESP_ERASE_REGION, struct.pack('<II', offset, size), timeout=timeout)

    def read_flash_slow(self, offset, length, progress_fn):
        raise NotImplementedInROMError(self, self.read_flash_slow)

    def read_flash(self, offset, length, progress_fn=None):
        if not self.IS_STUB:
            return self.read_flash_slow(offset, length, progress_fn)  # ROM-only routine

        # issue a standard bootloader command to trigger the read
        self.check_command("read flash", self.ESP_READ_FLASH,
                           struct.pack('<IIII',
                                       offset,
                                       length,
                                       self.FLASH_SECTOR_SIZE,
                                       64))
        # now we expect (length // block_size) SLIP frames with the data
        data = b''
        while len(data) < length:
            p = self.read()
            data += p
            if len(data) < length and len(p) < self.FLASH_SECTOR_SIZE:
                raise FatalError('Corrupt data, expected 0x%x bytes but received 0x%x bytes' %
                                 (self.FLASH_SECTOR_SIZE, len(p)))
            self.write(struct.pack('<I', len(data)))
            if progress_fn and (len(data) % 1024 == 0 or len(data) == length):
                progress_fn(len(data), length)
        if progress_fn:
            progress_fn(len(data), length)
        if len(data) > length:
            raise FatalError('Read more than expected')

        digest_frame = self.read()
        if len(digest_frame) != 16:
            raise FatalError('Expected digest, got: %s' % hexify(digest_frame))
        expected_digest = hexify(digest_frame).upper()
        digest = hashlib.md5(data).hexdigest().upper()
        if digest != expected_digest:
            raise FatalError('Digest mismatch: expected %s, got %s' % (expected_digest, digest))
        return data

    def flash_spi_attach(self, hspi_arg):
        """Send SPI attach command to enable the SPI flash pins

        ESP8266 ROM does this when you send flash_begin, ESP32 ROM
        has it as a SPI command.
        """
        # last 3 bytes in ESP_SPI_ATTACH argument are reserved values
        arg = struct.pack('<I', hspi_arg)
        if not self.IS_STUB:
            # ESP32 ROM loader takes additional 'is legacy' arg, which is not
            # currently supported in the stub loader or esptool.py (as it's not usually needed.)
            is_legacy = 0
            arg += struct.pack('BBBB', is_legacy, 0, 0, 0)
        self.check_command("configure SPI flash pins", ESP32ROM.ESP_SPI_ATTACH, arg)

    def flash_set_parameters(self, size):
        """Tell the ESP bootloader the parameters of the chip

        Corresponds to the "flashchip" data structure that the ROM
        has in RAM.

        'size' is in bytes.

        All other flash parameters are currently hardcoded (on ESP8266
        these are mostly ignored by ROM code, on ESP32 I'm not sure.)
        """
        fl_id = 0
        total_size = size
        block_size = 64 * 1024
        sector_size = 4 * 1024
        page_size = 256
        status_mask = 0xffff
        self.check_command("set SPI params", ESP32ROM.ESP_SPI_SET_PARAMS,
                           struct.pack('<IIIIII', fl_id, total_size, block_size, sector_size, page_size, status_mask))

    def run_spiflash_command(self, spiflash_command, data=b"", read_bits=0, addr=None, addr_len=0, dummy_len=0):
        """Run an arbitrary SPI flash command.

        This function uses the "USR_COMMAND" functionality in the ESP
        SPI hardware, rather than the precanned commands supported by
        hardware. So the value of spiflash_command is an actual command
        byte, sent over the wire.

        After writing command byte, writes 'data' to MOSI and then
        reads back 'read_bits' of reply on MISO. Result is a number.
        """

        # SPI_USR register flags
        SPI_USR_COMMAND = (1 << 31)
        SPI_USR_ADDR = (1 << 30)
        SPI_USR_DUMMY = (1 << 29)
        SPI_USR_MISO = (1 << 28)
        SPI_USR_MOSI = (1 << 27)

        # SPI registers, base address differs ESP32* vs 8266
        base = self.SPI_REG_BASE
        SPI_CMD_REG = base + 0x00
        SPI_ADDR_REG = base + 0x04
        SPI_USR_REG = base + self.SPI_USR_OFFS
        SPI_USR1_REG = base + self.SPI_USR1_OFFS
        SPI_USR2_REG = base + self.SPI_USR2_OFFS
        SPI_W0_REG = base + self.SPI_W0_OFFS

        # following two registers are ESP32 and later chips only
        if self.SPI_MOSI_DLEN_OFFS is not None:
            # ESP32 and later chips have a more sophisticated way to set up "user" commands
            def set_data_lengths(mosi_bits, miso_bits):
                SPI_MOSI_DLEN_REG = base + self.SPI_MOSI_DLEN_OFFS
                SPI_MISO_DLEN_REG = base + self.SPI_MISO_DLEN_OFFS
                if mosi_bits > 0:
                    self.write_reg(SPI_MOSI_DLEN_REG, mosi_bits - 1)
                if miso_bits > 0:
                    self.write_reg(SPI_MISO_DLEN_REG, miso_bits - 1)
                flags = 0
                if dummy_len > 0:
                    flags |= (dummy_len - 1)
                if addr_len > 0:
                    flags |= (addr_len - 1) << SPI_USR_ADDR_LEN_SHIFT
                if flags:
                    self.write_reg(SPI_USR1_REG, flags)
        else:
            def set_data_lengths(mosi_bits, miso_bits):
                SPI_DATA_LEN_REG = SPI_USR1_REG
                SPI_MOSI_BITLEN_S = 17
                SPI_MISO_BITLEN_S = 8
                mosi_mask = 0 if (mosi_bits == 0) else (mosi_bits - 1)
                miso_mask = 0 if (miso_bits == 0) else (miso_bits - 1)
                flags = (miso_mask << SPI_MISO_BITLEN_S) | (mosi_mask << SPI_MOSI_BITLEN_S)
                if dummy_len > 0:
                    flags |= (dummy_len - 1)
                if addr_len > 0:
                    flags |= (addr_len - 1) << SPI_USR_ADDR_LEN_SHIFT
                self.write_reg(SPI_DATA_LEN_REG, flags)

        # SPI peripheral "command" bitmasks for SPI_CMD_REG
        SPI_CMD_USR = (1 << 18)

        # shift values
        SPI_USR2_COMMAND_LEN_SHIFT = 28
        SPI_USR_ADDR_LEN_SHIFT = 26

        if read_bits > 32:
            raise FatalError("Reading more than 32 bits back from a SPI flash operation is unsupported")
        if len(data) > 64:
            raise FatalError("Writing more than 64 bytes of data with one SPI command is unsupported")

        data_bits = len(data) * 8
        old_spi_usr = self.read_reg(SPI_USR_REG)
        old_spi_usr2 = self.read_reg(SPI_USR2_REG)
        flags = SPI_USR_COMMAND
        if read_bits > 0:
            flags |= SPI_USR_MISO
        if data_bits > 0:
            flags |= SPI_USR_MOSI
        if addr_len > 0:
            flags |= SPI_USR_ADDR
        if dummy_len > 0:
            flags |= SPI_USR_DUMMY
        set_data_lengths(data_bits, read_bits)
        self.write_reg(SPI_USR_REG, flags)
        self.write_reg(SPI_USR2_REG,
                       (7 << SPI_USR2_COMMAND_LEN_SHIFT) | spiflash_command)
        if addr and addr_len > 0:
            self.write_reg(SPI_ADDR_REG, addr)
        if data_bits == 0:
            self.write_reg(SPI_W0_REG, 0)  # clear data register before we read it
        else:
            data = pad_to(data, 4, b'\00')  # pad to 32-bit multiple
            words = struct.unpack("I" * (len(data) // 4), data)
            next_reg = SPI_W0_REG
            for word in words:
                self.write_reg(next_reg, word)
                next_reg += 4
        self.write_reg(SPI_CMD_REG, SPI_CMD_USR)

        def wait_done():
            for _ in range(10):
                if (self.read_reg(SPI_CMD_REG) & SPI_CMD_USR) == 0:
                    return
            raise FatalError("SPI command did not complete in time")
        wait_done()

        status = self.read_reg(SPI_W0_REG)
        # restore some SPI controller registers
        self.write_reg(SPI_USR_REG, old_spi_usr)
        self.write_reg(SPI_USR2_REG, old_spi_usr2)
        return status

    def read_spiflash_sfdp(self, addr, read_bits):
        CMD_RDSFDP = 0x5A
        return self.run_spiflash_command(CMD_RDSFDP, read_bits=read_bits, addr=addr, addr_len=24, dummy_len=8)

    def read_status(self, num_bytes=2):
        """Read up to 24 bits (num_bytes) of SPI flash status register contents
        via RDSR, RDSR2, RDSR3 commands

        Not all SPI flash supports all three commands. The upper 1 or 2
        bytes may be 0xFF.
        """
        SPIFLASH_RDSR = 0x05
        SPIFLASH_RDSR2 = 0x35
        SPIFLASH_RDSR3 = 0x15

        status = 0
        shift = 0
        for cmd in [SPIFLASH_RDSR, SPIFLASH_RDSR2, SPIFLASH_RDSR3][0:num_bytes]:
            status += self.run_spiflash_command(cmd, read_bits=8) << shift
            shift += 8
        return status

    def write_status(self, new_status, num_bytes=2, set_non_volatile=False):
        """Write up to 24 bits (num_bytes) of new status register

        num_bytes can be 1, 2 or 3.

        Not all flash supports the additional commands to write the
        second and third byte of the status register. When writing 2
        bytes, esptool also sends a 16-byte WRSR command (as some
        flash types use this instead of WRSR2.)

        If the set_non_volatile flag is set, non-volatile bits will
        be set as well as volatile ones (WREN used instead of WEVSR).

        """
        SPIFLASH_WRSR = 0x01
        SPIFLASH_WRSR2 = 0x31
        SPIFLASH_WRSR3 = 0x11
        SPIFLASH_WEVSR = 0x50
        SPIFLASH_WREN = 0x06
        SPIFLASH_WRDI = 0x04

        enable_cmd = SPIFLASH_WREN if set_non_volatile else SPIFLASH_WEVSR

        # try using a 16-bit WRSR (not supported by all chips)
        # this may be redundant, but shouldn't hurt
        if num_bytes == 2:
            self.run_spiflash_command(enable_cmd)
            self.run_spiflash_command(SPIFLASH_WRSR, struct.pack("<H", new_status))

        # also try using individual commands (also not supported by all chips for num_bytes 2 & 3)
        for cmd in [SPIFLASH_WRSR, SPIFLASH_WRSR2, SPIFLASH_WRSR3][0:num_bytes]:
            self.run_spiflash_command(enable_cmd)
            self.run_spiflash_command(cmd, struct.pack("B", new_status & 0xFF))
            new_status >>= 8

        self.run_spiflash_command(SPIFLASH_WRDI)

    def get_crystal_freq(self):
        # Figure out the crystal frequency from the UART clock divider
        # Returns a normalized value in integer MHz (40 or 26 are the only supported values)
        #
        # The logic here is:
        # - We know that our baud rate and the ESP UART baud rate are roughly the same, or we couldn't communicate
        # - We can read the UART clock divider register to know how the ESP derives this from the APB bus frequency
        # - Multiplying these two together gives us the bus frequency which is either the crystal frequency (ESP32)
        #   or double the crystal frequency (ESP8266). See the self.XTAL_CLK_DIVIDER parameter for this factor.
        uart_div = self.read_reg(self.UART_CLKDIV_REG) & self.UART_CLKDIV_MASK
        est_xtal = (self._port.baudrate * uart_div) / 1e6 / self.XTAL_CLK_DIVIDER
        norm_xtal = 40 if est_xtal > 33 else 26
        if abs(norm_xtal - est_xtal) > 1:
            print("WARNING: Detected crystal freq %.2fMHz is quite different to normalized freq %dMHz. Unsupported crystal in use?" % (
                est_xtal, norm_xtal))
        return norm_xtal

    def hard_reset(self):
        print('Hard resetting via RTS pin...')
        self._setRTS(True)  # EN->LOW
        time.sleep(0.1)
        self._setRTS(False)

    def soft_reset(self, stay_in_bootloader):
        if not self.IS_STUB:
            if stay_in_bootloader:
                return  # ROM bootloader is already in bootloader!
            else:
                # 'run user code' is as close to a soft reset as we can do
                self.flash_begin(0, 0)
                self.flash_finish(False)
        else:
            if stay_in_bootloader:
                # soft resetting from the stub loader
                # will re-load the ROM bootloader
                self.flash_begin(0, 0)
                self.flash_finish(True)
            elif self.CHIP_NAME != "ESP8266":
                raise FatalError("Soft resetting is currently only supported on ESP8266")
            else:
                # running user code from stub loader requires some hacks
                # in the stub loader
                self.command(self.ESP_RUN_USER_CODE, wait_response=False)

    def check_chip_id(self):
        try:
            chip_id = self.get_chip_id()
            if chip_id != self.IMAGE_CHIP_ID:
                print("WARNING: Chip ID {} ({}) doesn't match expected Chip ID {}. esptool may not work correctly."
                      .format(chip_id, self.UNSUPPORTED_CHIPS.get(chip_id, 'Unknown'), self.IMAGE_CHIP_ID))
                # Try to flash anyways by disabling stub
                self.stub_is_disabled = True
        except NotImplementedInROMError:
            pass


class ESP8266ROM(ESPLoader):
    """ Access class for ESP8266 ROM bootloader
    """
    CHIP_NAME = "ESP8266"
    IS_STUB = False

    CHIP_DETECT_MAGIC_VALUE = [0xfff0c101]

    # OTP ROM addresses
    ESP_OTP_MAC0 = 0x3ff00050
    ESP_OTP_MAC1 = 0x3ff00054
    ESP_OTP_MAC3 = 0x3ff0005c

    SPI_REG_BASE = 0x60000200
    SPI_USR_OFFS = 0x1c
    SPI_USR1_OFFS = 0x20
    SPI_USR2_OFFS = 0x24
    SPI_MOSI_DLEN_OFFS = None
    SPI_MISO_DLEN_OFFS = None
    SPI_W0_OFFS = 0x40

    UART_CLKDIV_REG = 0x60000014

    XTAL_CLK_DIVIDER = 2

    FLASH_SIZES = {
        '512KB': 0x00,
        '256KB': 0x10,
        '1MB': 0x20,
        '2MB': 0x30,
        '4MB': 0x40,
        '2MB-c1': 0x50,
        '4MB-c1': 0x60,
        '8MB': 0x80,
        '16MB': 0x90,
    }

    FLASH_FREQUENCY = {
        '80m': 0xf,
        '40m': 0x0,
        '26m': 0x1,
        '20m': 0x2,
    }

    BOOTLOADER_FLASH_OFFSET = 0

    MEMORY_MAP = [[0x3FF00000, 0x3FF00010, "DPORT"],
                  [0x3FFE8000, 0x40000000, "DRAM"],
                  [0x40100000, 0x40108000, "IRAM"],
                  [0x40201010, 0x402E1010, "IROM"]]

    def get_efuses(self):
        # Return the 128 bits of ESP8266 efuse as a single Python integer
        result = self.read_reg(0x3ff0005c) << 96
        result |= self.read_reg(0x3ff00058) << 64
        result |= self.read_reg(0x3ff00054) << 32
        result |= self.read_reg(0x3ff00050)
        return result

    def _get_flash_size(self, efuses):
        # rX_Y = EFUSE_DATA_OUTX[Y]
        r0_4 = (efuses & (1 << 4)) != 0
        r3_25 = (efuses & (1 << 121)) != 0
        r3_26 = (efuses & (1 << 122)) != 0
        r3_27 = (efuses & (1 << 123)) != 0

        if r0_4 and not r3_25:
            if not r3_27 and not r3_26:
                return 1
            elif not r3_27 and r3_26:
                return 2
        if not r0_4 and r3_25:
            if not r3_27 and not r3_26:
                return 2
            elif not r3_27 and r3_26:
                return 4
        return -1

    def get_chip_description(self):
        efuses = self.get_efuses()
        is_8285 = (efuses & ((1 << 4) | 1 << 80)) != 0  # One or the other efuse bit is set for ESP8285
        if is_8285:
            flash_size = self._get_flash_size(efuses)
            max_temp = (efuses & (1 << 5)) != 0  # This efuse bit identifies the max flash temperature
            chip_name = {
                1: "ESP8285H08" if max_temp else "ESP8285N08",
                2: "ESP8285H16" if max_temp else "ESP8285N16"
            }.get(flash_size, "ESP8285")
            return chip_name
        return "ESP8266EX"

    def get_chip_features(self):
        features = ["WiFi"]
        if "ESP8285" in self.get_chip_description():
            features += ["Embedded Flash"]
        return features

    def flash_spi_attach(self, hspi_arg):
        if self.IS_STUB:
            super(ESP8266ROM, self).flash_spi_attach(hspi_arg)
        else:
            # ESP8266 ROM has no flash_spi_attach command in serial protocol,
            # but flash_begin will do it
            self.flash_begin(0, 0)

    def flash_set_parameters(self, size):
        # not implemented in ROM, but OK to silently skip for ROM
        if self.IS_STUB:
            super(ESP8266ROM, self).flash_set_parameters(size)

    def chip_id(self):
        """ Read Chip ID from efuse - the equivalent of the SDK system_get_chip_id() function """
        id0 = self.read_reg(self.ESP_OTP_MAC0)
        id1 = self.read_reg(self.ESP_OTP_MAC1)
        return (id0 >> 24) | ((id1 & MAX_UINT24) << 8)

    def read_mac(self):
        """ Read MAC from OTP ROM """
        mac0 = self.read_reg(self.ESP_OTP_MAC0)
        mac1 = self.read_reg(self.ESP_OTP_MAC1)
        mac3 = self.read_reg(self.ESP_OTP_MAC3)
        if (mac3 != 0):
            oui = ((mac3 >> 16) & 0xff, (mac3 >> 8) & 0xff, mac3 & 0xff)
        elif ((mac1 >> 16) & 0xff) == 0:
            oui = (0x18, 0xfe, 0x34)
        elif ((mac1 >> 16) & 0xff) == 1:
            oui = (0xac, 0xd0, 0x74)
        else:
            raise FatalError("Unknown OUI")
        return oui + ((mac1 >> 8) & 0xff, mac1 & 0xff, (mac0 >> 24) & 0xff)

    def get_erase_size(self, offset, size):
        """ Calculate an erase size given a specific size in bytes.

        Provides a workaround for the bootloader erase bug."""

        sectors_per_block = 16
        sector_size = self.FLASH_SECTOR_SIZE
        num_sectors = (size + sector_size - 1) // sector_size
        start_sector = offset // sector_size

        head_sectors = sectors_per_block - (start_sector % sectors_per_block)
        if num_sectors < head_sectors:
            head_sectors = num_sectors

        if num_sectors < 2 * head_sectors:
            return (num_sectors + 1) // 2 * sector_size
        else:
            return (num_sectors - head_sectors) * sector_size

    def override_vddsdio(self, new_voltage):
        raise NotImplementedInROMError("Overriding VDDSDIO setting only applies to ESP32")


class ESP8266StubLoader(ESP8266ROM):
    """ Access class for ESP8266 stub loader, runs on top of ROM.
    """
    FLASH_WRITE_SIZE = 0x4000  # matches MAX_WRITE_BLOCK in stub_loader.c
    IS_STUB = True

    def __init__(self, rom_loader):
        self.secure_download_mode = rom_loader.secure_download_mode
        self._port = rom_loader._port
        self._trace_enabled = rom_loader._trace_enabled
        self.flush_input()  # resets _slip_reader

    def get_erase_size(self, offset, size):
        return size  # stub doesn't have same size bug as ROM loader


ESP8266ROM.STUB_CLASS = ESP8266StubLoader


class ESP32ROM(ESPLoader):
    """Access class for ESP32 ROM bootloader

    """
    CHIP_NAME = "ESP32"
    IMAGE_CHIP_ID = 0
    IS_STUB = False

    FPGA_SLOW_BOOT = True

    CHIP_DETECT_MAGIC_VALUE = [0x00f01d83]

    IROM_MAP_START = 0x400d0000
    IROM_MAP_END = 0x40400000

    DROM_MAP_START = 0x3F400000
    DROM_MAP_END = 0x3F800000

    # ESP32 uses a 4 byte status reply
    STATUS_BYTES_LENGTH = 4

    SPI_REG_BASE = 0x3ff42000
    SPI_USR_OFFS = 0x1c
    SPI_USR1_OFFS = 0x20
    SPI_USR2_OFFS = 0x24
    SPI_MOSI_DLEN_OFFS = 0x28
    SPI_MISO_DLEN_OFFS = 0x2c
    EFUSE_RD_REG_BASE = 0x3ff5a000

    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT_REG = EFUSE_RD_REG_BASE + 0x18
    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT = (1 << 7)  # EFUSE_RD_DISABLE_DL_ENCRYPT

    DR_REG_SYSCON_BASE = 0x3ff66000
    APB_CTL_DATE_ADDR = DR_REG_SYSCON_BASE + 0x7C
    APB_CTL_DATE_V = 0x1
    APB_CTL_DATE_S = 31

    SPI_W0_OFFS = 0x80

    UART_CLKDIV_REG = 0x3ff40014

    XTAL_CLK_DIVIDER = 1

    FLASH_SIZES = {
        '1MB': 0x00,
        '2MB': 0x10,
        '4MB': 0x20,
        '8MB': 0x30,
        '16MB': 0x40,
        '32MB': 0x50,
        '64MB': 0x60,
        '128MB': 0x70
    }

    FLASH_FREQUENCY = {
        '80m': 0xf,
        '40m': 0x0,
        '26m': 0x1,
        '20m': 0x2,
    }

    BOOTLOADER_FLASH_OFFSET = 0x1000

    OVERRIDE_VDDSDIO_CHOICES = ["1.8V", "1.9V", "OFF"]

    MEMORY_MAP = [[0x00000000, 0x00010000, "PADDING"],
                  [0x3F400000, 0x3F800000, "DROM"],
                  [0x3F800000, 0x3FC00000, "EXTRAM_DATA"],
                  [0x3FF80000, 0x3FF82000, "RTC_DRAM"],
                  [0x3FF90000, 0x40000000, "BYTE_ACCESSIBLE"],
                  [0x3FFAE000, 0x40000000, "DRAM"],
                  [0x3FFE0000, 0x3FFFFFFC, "DIRAM_DRAM"],
                  [0x40000000, 0x40070000, "IROM"],
                  [0x40070000, 0x40078000, "CACHE_PRO"],
                  [0x40078000, 0x40080000, "CACHE_APP"],
                  [0x40080000, 0x400A0000, "IRAM"],
                  [0x400A0000, 0x400BFFFC, "DIRAM_IRAM"],
                  [0x400C0000, 0x400C2000, "RTC_IRAM"],
                  [0x400D0000, 0x40400000, "IROM"],
                  [0x50000000, 0x50002000, "RTC_DATA"]]

    FLASH_ENCRYPTED_WRITE_ALIGN = 32

    """ Try to read the BLOCK1 (encryption key) and check if it is valid """

    def is_flash_encryption_key_valid(self):
        """ Bit 0 of efuse_rd_disable[3:0] is mapped to BLOCK1
        this bit is at position 16 in EFUSE_BLK0_RDATA0_REG """
        word0 = self.read_efuse(0)
        rd_disable = (word0 >> 16) & 0x1

        # reading of BLOCK1 is NOT ALLOWED so we assume valid key is programmed
        if rd_disable:
            return True
        else:
            # reading of BLOCK1 is ALLOWED so we will read and verify for non-zero.
            # When ESP32 has not generated AES/encryption key in BLOCK1, the contents will be readable and 0.
            # If the flash encryption is enabled it is expected to have a valid non-zero key. We break out on
            # first occurance of non-zero value
            key_word = [0] * 7
            for i in range(len(key_word)):
                key_word[i] = self.read_efuse(14 + i)
                # key is non-zero so break & return
                if key_word[i] != 0:
                    return True
            return False

    def get_flash_crypt_config(self):
        """ For flash encryption related commands we need to make sure
        user has programmed all the relevant efuse correctly so before
        writing encrypted write_flash_encrypt esptool will verify the values
        of flash_crypt_config to be non zero if they are not read
        protected. If the values are zero a warning will be printed

        bit 3 in efuse_rd_disable[3:0] is mapped to flash_crypt_config
        this bit is at position 19 in EFUSE_BLK0_RDATA0_REG """
        word0 = self.read_efuse(0)
        rd_disable = (word0 >> 19) & 0x1

        if rd_disable == 0:
            """ we can read the flash_crypt_config efuse value
            so go & read it (EFUSE_BLK0_RDATA5_REG[31:28]) """
            word5 = self.read_efuse(5)
            word5 = (word5 >> 28) & 0xF
            return word5
        else:
            # if read of the efuse is disabled we assume it is set correctly
            return 0xF

    def get_encrypted_download_disabled(self):
        if self.read_reg(self.EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT_REG) & self.EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT:
            return True
        else:
            return False

    def get_pkg_version(self):
        word3 = self.read_efuse(3)
        pkg_version = (word3 >> 9) & 0x07
        pkg_version += ((word3 >> 2) & 0x1) << 3
        return pkg_version

    # Returns new version format based on major and minor versions
    def get_chip_full_revision(self):
        return self.get_major_chip_version() * 100 + self.get_minor_chip_version()

    # Returns old version format (ECO number). Use the new format get_chip_full_revision().
    def get_chip_revision(self):
        return self.get_major_chip_version()

    def get_minor_chip_version(self):
        return (self.read_efuse(5) >> 24) & 0x3

    def get_major_chip_version(self):
        rev_bit0 = (self.read_efuse(3) >> 15) & 0x1
        rev_bit1 = (self.read_efuse(5) >> 20) & 0x1
        apb_ctl_date = self.read_reg(self.APB_CTL_DATE_ADDR)
        rev_bit2 = (apb_ctl_date >> self.APB_CTL_DATE_S) & self.APB_CTL_DATE_V
        combine_value = (rev_bit2 << 2) | (rev_bit1 << 1) | rev_bit0

        revision = {
            0: 0,
            1: 1,
            3: 2,
            7: 3,
        }.get(combine_value, 0)
        return revision

    def get_chip_description(self):
        pkg_version = self.get_pkg_version()
        major_rev = self.get_major_chip_version()
        minor_rev = self.get_minor_chip_version()
        rev3 = major_rev == 3
        single_core = self.read_efuse(3) & (1 << 0)  # CHIP_VER DIS_APP_CPU

        chip_name = {
            0: "ESP32-S0WDQ6" if single_core else "ESP32-D0WDQ6",
            1: "ESP32-S0WDQ5" if single_core else "ESP32-D0WDQ5",
            2: "ESP32-S2WDQ5" if single_core else "ESP32-D2WDQ5",
            3: "ESP32-S0WD-OEM" if single_core else "ESP32-D0WD-OEM",
            4: "ESP32-U4WDH",
            5: "ESP32-PICO-V3" if rev3 else "ESP32-PICO-D4",
            6: "ESP32-PICO-V3-02",
            7: "ESP32-D0WDR2-V3",
        }.get(pkg_version, "unknown ESP32")

        # ESP32-D0WD-V3, ESP32-D0WDQ6-V3
        if chip_name.startswith("ESP32-D0WD") and rev3:
            chip_name += "-V3"

        return "%s (revision v%d.%d)" % (chip_name, major_rev, minor_rev)

    def get_chip_features(self):
        features = ["WiFi"]
        word3 = self.read_efuse(3)

        # names of variables in this section are lowercase
        #  versions of EFUSE names as documented in TRM and
        # ESP-IDF efuse_reg.h

        chip_ver_dis_bt = word3 & (1 << 1)
        if chip_ver_dis_bt == 0:
            features += ["BT"]

        chip_ver_dis_app_cpu = word3 & (1 << 0)
        if chip_ver_dis_app_cpu:
            features += ["Single Core"]
        else:
            features += ["Dual Core"]

        chip_cpu_freq_rated = word3 & (1 << 13)
        if chip_cpu_freq_rated:
            chip_cpu_freq_low = word3 & (1 << 12)
            if chip_cpu_freq_low:
                features += ["160MHz"]
            else:
                features += ["240MHz"]

        pkg_version = self.get_pkg_version()
        if pkg_version in [2, 4, 5, 6]:
            features += ["Embedded Flash"]

        if pkg_version == 6:
            features += ["Embedded PSRAM"]

        word4 = self.read_efuse(4)
        adc_vref = (word4 >> 8) & 0x1F
        if adc_vref:
            features += ["VRef calibration in efuse"]

        blk3_part_res = word3 >> 14 & 0x1
        if blk3_part_res:
            features += ["BLK3 partially reserved"]

        word6 = self.read_efuse(6)
        coding_scheme = word6 & 0x3
        features += ["Coding Scheme %s" % {
            0: "None",
            1: "3/4",
            2: "Repeat (UNSUPPORTED)",
            3: "Invalid"}[coding_scheme]]

        return features

    def read_efuse(self, n):
        """ Read the nth word of the ESP3x EFUSE region. """
        return self.read_reg(self.EFUSE_RD_REG_BASE + (4 * n))

    def chip_id(self):
        raise NotSupportedError(self, "chip_id")

    def read_mac(self):
        """ Read MAC from EFUSE region """
        words = [self.read_efuse(2), self.read_efuse(1)]
        bitstring = struct.pack(">II", *words)
        bitstring = bitstring[2:8]  # trim the 2 byte CRC
        try:
            return tuple(ord(b) for b in bitstring)
        except TypeError:  # Python 3, bitstring elements are already bytes
            return tuple(bitstring)

    def get_erase_size(self, offset, size):
        return size

    def override_vddsdio(self, new_voltage):
        new_voltage = new_voltage.upper()
        if new_voltage not in self.OVERRIDE_VDDSDIO_CHOICES:
            raise FatalError("The only accepted VDDSDIO overrides are '1.8V', '1.9V' and 'OFF'")
        RTC_CNTL_SDIO_CONF_REG = 0x3ff48074
        RTC_CNTL_XPD_SDIO_REG = (1 << 31)
        RTC_CNTL_DREFH_SDIO_M = (3 << 29)
        RTC_CNTL_DREFM_SDIO_M = (3 << 27)
        RTC_CNTL_DREFL_SDIO_M = (3 << 25)
        # RTC_CNTL_SDIO_TIEH = (1 << 23)  # not used here, setting TIEH=1 would set 3.3V output, not safe for esptool.py to do
        RTC_CNTL_SDIO_FORCE = (1 << 22)
        RTC_CNTL_SDIO_PD_EN = (1 << 21)

        reg_val = RTC_CNTL_SDIO_FORCE  # override efuse setting
        reg_val |= RTC_CNTL_SDIO_PD_EN
        if new_voltage != "OFF":
            reg_val |= RTC_CNTL_XPD_SDIO_REG  # enable internal LDO
        if new_voltage == "1.9V":
            reg_val |= (RTC_CNTL_DREFH_SDIO_M | RTC_CNTL_DREFM_SDIO_M | RTC_CNTL_DREFL_SDIO_M)  # boost voltage
        self.write_reg(RTC_CNTL_SDIO_CONF_REG, reg_val)
        print("VDDSDIO regulator set to %s" % new_voltage)

    def read_flash_slow(self, offset, length, progress_fn):
        BLOCK_LEN = 64  # ROM read limit per command (this limit is why it's so slow)

        data = b''
        while len(data) < length:
            block_len = min(BLOCK_LEN, length - len(data))
            r = self.check_command("read flash block", self.ESP_READ_FLASH_SLOW,
                                   struct.pack('<II', offset + len(data), block_len))
            if len(r) < block_len:
                raise FatalError("Expected %d byte block, got %d bytes. Serial errors?" % (block_len, len(r)))
            # command always returns 64 byte buffer, regardless of how many bytes were actually read from flash
            data += r[:block_len]
            if progress_fn and (len(data) % 1024 == 0 or len(data) == length):
                progress_fn(len(data), length)
        return data


class ESP32S2ROM(ESP32ROM):
    CHIP_NAME = "ESP32-S2"
    IMAGE_CHIP_ID = 2

    IROM_MAP_START = 0x40080000
    IROM_MAP_END = 0x40B80000
    DROM_MAP_START = 0x3F000000
    DROM_MAP_END = 0x3F3F0000

    CHIP_DETECT_MAGIC_VALUE = [0x000007C6]

    SPI_REG_BASE = 0x3F402000
    SPI_USR_OFFS = 0x18
    SPI_USR1_OFFS = 0x1C
    SPI_USR2_OFFS = 0x20
    SPI_MOSI_DLEN_OFFS = 0x24
    SPI_MISO_DLEN_OFFS = 0x28
    SPI_W0_OFFS = 0x58

    MAC_EFUSE_REG = 0x3F41A044  # ESP32-S2 has special block for MAC efuses

    UART_CLKDIV_REG = 0x3F400014

    SUPPORTS_ENCRYPTED_FLASH = True

    FLASH_ENCRYPTED_WRITE_ALIGN = 16

    # todo: use espefuse APIs to get this info
    EFUSE_BASE = 0x3F41A000
    EFUSE_RD_REG_BASE = EFUSE_BASE + 0x030  # BLOCK0 read base address
    EFUSE_BLOCK1_ADDR = EFUSE_BASE + 0x044
    EFUSE_BLOCK2_ADDR = EFUSE_BASE + 0x05C

    EFUSE_PURPOSE_KEY0_REG = EFUSE_BASE + 0x34
    EFUSE_PURPOSE_KEY0_SHIFT = 24
    EFUSE_PURPOSE_KEY1_REG = EFUSE_BASE + 0x34
    EFUSE_PURPOSE_KEY1_SHIFT = 28
    EFUSE_PURPOSE_KEY2_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY2_SHIFT = 0
    EFUSE_PURPOSE_KEY3_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY3_SHIFT = 4
    EFUSE_PURPOSE_KEY4_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY4_SHIFT = 8
    EFUSE_PURPOSE_KEY5_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY5_SHIFT = 12

    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT_REG = EFUSE_RD_REG_BASE
    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT = 1 << 19

    EFUSE_SPI_BOOT_CRYPT_CNT_REG = EFUSE_BASE + 0x034
    EFUSE_SPI_BOOT_CRYPT_CNT_MASK = 0x7 << 18

    EFUSE_SECURE_BOOT_EN_REG = EFUSE_BASE + 0x038
    EFUSE_SECURE_BOOT_EN_MASK = 1 << 20

    EFUSE_RD_REPEAT_DATA3_REG = EFUSE_BASE + 0x3C
    EFUSE_RD_REPEAT_DATA3_REG_FLASH_TYPE_MASK = 1 << 9

    PURPOSE_VAL_XTS_AES256_KEY_1 = 2
    PURPOSE_VAL_XTS_AES256_KEY_2 = 3
    PURPOSE_VAL_XTS_AES128_KEY = 4

    UARTDEV_BUF_NO = 0x3FFFFD14  # Variable in ROM .bss which indicates the port in use
    UARTDEV_BUF_NO_USB = 2  # Value of the above indicating that USB-OTG is in use

    USB_RAM_BLOCK = 0x800  # Max block size USB-OTG is used

    GPIO_STRAP_REG = 0x3F404038
    GPIO_STRAP_SPI_BOOT_MASK = 1 << 3  # Not download mode
    RTC_CNTL_OPTION1_REG = 0x3F408128
    RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK = 0x1  # Is download mode forced over USB?

    RTCCNTL_BASE_REG = 0x3F408000
    RTC_CNTL_WDTCONFIG0_REG = RTCCNTL_BASE_REG + 0x0094
    RTC_CNTL_WDTCONFIG1_REG = RTCCNTL_BASE_REG + 0x0098
    RTC_CNTL_WDTWPROTECT_REG = RTCCNTL_BASE_REG + 0x00AC
    RTC_CNTL_WDT_WKEY = 0x50D83AA1

    MEMORY_MAP = [
        [0x00000000, 0x00010000, "PADDING"],
        [0x3F000000, 0x3FF80000, "DROM"],
        [0x3F500000, 0x3FF80000, "EXTRAM_DATA"],
        [0x3FF9E000, 0x3FFA0000, "RTC_DRAM"],
        [0x3FF9E000, 0x40000000, "BYTE_ACCESSIBLE"],
        [0x3FF9E000, 0x40072000, "MEM_INTERNAL"],
        [0x3FFB0000, 0x40000000, "DRAM"],
        [0x40000000, 0x4001A100, "IROM_MASK"],
        [0x40020000, 0x40070000, "IRAM"],
        [0x40070000, 0x40072000, "RTC_IRAM"],
        [0x40080000, 0x40800000, "IROM"],
        [0x50000000, 0x50002000, "RTC_DATA"],
    ]

    UF2_FAMILY_ID = 0xBFDD4EEE

    # Returns old version format (ECO number). Use the new format get_chip_full_revision().
    def get_chip_revision(self):
        return self.get_major_chip_version()

    def get_pkg_version(self):
        num_word = 4
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 0) & 0x0F

    def get_minor_chip_version(self):
        hi_num_word = 3
        hi = (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * hi_num_word)) >> 20) & 0x01
        low_num_word = 4
        low = (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * low_num_word)) >> 4) & 0x07
        return (hi << 3) + low

    def get_major_chip_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 18) & 0x03

    def get_flash_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 21) & 0x0F

    def get_flash_cap(self):
        return self.get_flash_version()

    def get_psram_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 28) & 0x0F

    def get_psram_cap(self):
        return self.get_psram_version()

    def get_block2_version(self):
        # BLK_VERSION_MINOR
        num_word = 4
        return (self.read_reg(self.EFUSE_BLOCK2_ADDR + (4 * num_word)) >> 4) & 0x07

    def get_chip_description(self):
        chip_name = {
            0: "ESP32-S2",
            1: "ESP32-S2FH2",
            2: "ESP32-S2FH4",
            102: "ESP32-S2FNR2",
            100: "ESP32-S2R2",
        }.get(
            self.get_flash_cap() + self.get_psram_cap() * 100,
            "unknown ESP32-S2",
        )
        major_rev = self.get_major_chip_version()
        minor_rev = self.get_minor_chip_version()
        return f"{chip_name} (revision v{major_rev}.{minor_rev})"

    def get_chip_features(self):
        features = ["WiFi"]

        if self.secure_download_mode:
            features += ["Secure Download Mode Enabled"]

        flash_version = {
            0: "No Embedded Flash",
            1: "Embedded Flash 2MB",
            2: "Embedded Flash 4MB",
        }.get(self.get_flash_cap(), "Unknown Embedded Flash")
        features += [flash_version]

        psram_version = {
            0: "No Embedded PSRAM",
            1: "Embedded PSRAM 2MB",
            2: "Embedded PSRAM 4MB",
        }.get(self.get_psram_cap(), "Unknown Embedded PSRAM")
        features += [psram_version]

        block2_version = {
            0: "No calibration in BLK2 of efuse",
            1: "ADC and temperature sensor calibration in BLK2 of efuse V1",
            2: "ADC and temperature sensor calibration in BLK2 of efuse V2",
        }.get(self.get_block2_version(), "Unknown Calibration in BLK2")
        features += [block2_version]

        return features

    def get_crystal_freq(self):
        # ESP32-S2 XTAL is fixed to 40MHz
        return 40

    def override_vddsdio(self, new_voltage):
        raise NotImplementedInROMError(
            "VDD_SDIO overrides are not supported for ESP32-S2"
        )

    def read_mac(self, mac_type="BASE_MAC"):
        """Read MAC from EFUSE region"""
        if mac_type != "BASE_MAC":
            return None
        mac0 = self.read_reg(self.MAC_EFUSE_REG)
        mac1 = self.read_reg(self.MAC_EFUSE_REG + 4)  # only bottom 16 bits are MAC
        bitstring = struct.pack(">II", mac1, mac0)[2:]
        return tuple(bitstring)

    def flash_type(self):
        return (
            1
            if self.read_reg(self.EFUSE_RD_REPEAT_DATA3_REG)
            & self.EFUSE_RD_REPEAT_DATA3_REG_FLASH_TYPE_MASK
            else 0
        )

    def get_flash_crypt_config(self):
        return None  # doesn't exist on ESP32-S2

    def get_secure_boot_enabled(self):
        return (
            self.read_reg(self.EFUSE_SECURE_BOOT_EN_REG)
            & self.EFUSE_SECURE_BOOT_EN_MASK
        )

    def get_key_block_purpose(self, key_block):
        if key_block < 0 or key_block > 5:
            raise FatalError("Valid key block numbers must be in range 0-5")

        reg, shift = [
            (self.EFUSE_PURPOSE_KEY0_REG, self.EFUSE_PURPOSE_KEY0_SHIFT),
            (self.EFUSE_PURPOSE_KEY1_REG, self.EFUSE_PURPOSE_KEY1_SHIFT),
            (self.EFUSE_PURPOSE_KEY2_REG, self.EFUSE_PURPOSE_KEY2_SHIFT),
            (self.EFUSE_PURPOSE_KEY3_REG, self.EFUSE_PURPOSE_KEY3_SHIFT),
            (self.EFUSE_PURPOSE_KEY4_REG, self.EFUSE_PURPOSE_KEY4_SHIFT),
            (self.EFUSE_PURPOSE_KEY5_REG, self.EFUSE_PURPOSE_KEY5_SHIFT),
        ][key_block]
        return (self.read_reg(reg) >> shift) & 0xF

    def is_flash_encryption_key_valid(self):
        # Need to see either an AES-128 key or two AES-256 keys
        purposes = [self.get_key_block_purpose(b) for b in range(6)]

        if any(p == self.PURPOSE_VAL_XTS_AES128_KEY for p in purposes):
            return True

        return any(p == self.PURPOSE_VAL_XTS_AES256_KEY_1 for p in purposes) and any(
            p == self.PURPOSE_VAL_XTS_AES256_KEY_2 for p in purposes
        )

    def uses_usb(self, _cache=[]):
        if self.secure_download_mode:
            return False  # can't detect native USB in secure download mode
        if not _cache:
            buf_no = self.read_reg(self.UARTDEV_BUF_NO) & 0xff
            _cache.append(buf_no == self.UARTDEV_BUF_NO_USB)
        return _cache[0]

    def _post_connect(self):
        if self.uses_usb():
            self.ESP_RAM_BLOCK = self.USB_RAM_BLOCK

    def rtc_wdt_reset(self):
        print("Hard resetting with RTC WDT...")
        self.write_reg(self.RTC_CNTL_WDTWPROTECT_REG, self.RTC_CNTL_WDT_WKEY)  # unlock
        self.write_reg(self.RTC_CNTL_WDTCONFIG1_REG, 5000)  # set WDT timeout
        self.write_reg(
            self.RTC_CNTL_WDTCONFIG0_REG, (1 << 31) | (5 << 28) | (1 << 8) | 2
        )  # enable WDT
        self.write_reg(self.RTC_CNTL_WDTWPROTECT_REG, 0)  # lock

    def hard_reset(self):
        if self.uses_usb():
            # Check the strapping register to see if we can perform RTC WDT reset
            strap_reg = self.read_reg(self.GPIO_STRAP_REG)
            force_dl_reg = self.read_reg(self.RTC_CNTL_OPTION1_REG)
            if (
                strap_reg & self.GPIO_STRAP_SPI_BOOT_MASK == 0  # GPIO0 low
                and force_dl_reg & self.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK == 0
            ):
                self.rtc_wdt_reset()
                return

        print('Hard resetting via RTS pin...')
        self._setRTS(True)  # EN->LOW
        if self.uses_usb():
            # Give the chip some time to come out of reset, to be able to handle further DTR/RTS transitions
            time.sleep(0.2)
            self._setRTS(False)
            time.sleep(0.2)
        else:
            time.sleep(0.1)
            self._setRTS(False)


class ESP32S3ROM(ESP32ROM):
    CHIP_NAME = "ESP32-S3"

    IMAGE_CHIP_ID = 9

    CHIP_DETECT_MAGIC_VALUE = [0x9]

    FPGA_SLOW_BOOT = False

    IROM_MAP_START = 0x42000000
    IROM_MAP_END = 0x44000000
    DROM_MAP_START = 0x3C000000
    DROM_MAP_END = 0x3E000000

    UART_DATE_REG_ADDR = 0x60000080

    SPI_REG_BASE = 0x60002000
    SPI_USR_OFFS = 0x18
    SPI_USR1_OFFS = 0x1C
    SPI_USR2_OFFS = 0x20
    SPI_MOSI_DLEN_OFFS = 0x24
    SPI_MISO_DLEN_OFFS = 0x28
    SPI_W0_OFFS = 0x58

    SPI_ADDR_REG_MSB = False

    BOOTLOADER_FLASH_OFFSET = 0x0

    SUPPORTS_ENCRYPTED_FLASH = True

    FLASH_ENCRYPTED_WRITE_ALIGN = 16

    # todo: use espefuse APIs to get this info
    EFUSE_BASE = 0x60007000  # BLOCK0 read base address
    EFUSE_BLOCK1_ADDR = EFUSE_BASE + 0x44
    EFUSE_BLOCK2_ADDR = EFUSE_BASE + 0x5C
    MAC_EFUSE_REG = EFUSE_BASE + 0x044

    EFUSE_RD_REG_BASE = EFUSE_BASE + 0x030  # BLOCK0 read base address

    EFUSE_PURPOSE_KEY0_REG = EFUSE_BASE + 0x34
    EFUSE_PURPOSE_KEY0_SHIFT = 24
    EFUSE_PURPOSE_KEY1_REG = EFUSE_BASE + 0x34
    EFUSE_PURPOSE_KEY1_SHIFT = 28
    EFUSE_PURPOSE_KEY2_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY2_SHIFT = 0
    EFUSE_PURPOSE_KEY3_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY3_SHIFT = 4
    EFUSE_PURPOSE_KEY4_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY4_SHIFT = 8
    EFUSE_PURPOSE_KEY5_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY5_SHIFT = 12

    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT_REG = EFUSE_RD_REG_BASE
    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT = 1 << 20

    EFUSE_SPI_BOOT_CRYPT_CNT_REG = EFUSE_BASE + 0x034
    EFUSE_SPI_BOOT_CRYPT_CNT_MASK = 0x7 << 18

    EFUSE_SECURE_BOOT_EN_REG = EFUSE_BASE + 0x038
    EFUSE_SECURE_BOOT_EN_MASK = 1 << 20

    EFUSE_RD_REPEAT_DATA3_REG = EFUSE_BASE + 0x3C
    EFUSE_RD_REPEAT_DATA3_REG_FLASH_TYPE_MASK = 1 << 9

    PURPOSE_VAL_XTS_AES256_KEY_1 = 2
    PURPOSE_VAL_XTS_AES256_KEY_2 = 3
    PURPOSE_VAL_XTS_AES128_KEY = 4

    UARTDEV_BUF_NO = 0x3FCEF14C  # Variable in ROM .bss which indicates the port in use
    UARTDEV_BUF_NO_USB = 3  # The above var when USB-OTG is used
    UARTDEV_BUF_NO_USB_JTAG_SERIAL = 4  # The above var when USB-JTAG/Serial is used

    RTCCNTL_BASE_REG = 0x60008000
    RTC_CNTL_SWD_CONF_REG = RTCCNTL_BASE_REG + 0x00B4
    RTC_CNTL_SWD_AUTO_FEED_EN = 1 << 31
    RTC_CNTL_SWD_WPROTECT_REG = RTCCNTL_BASE_REG + 0x00B8
    RTC_CNTL_SWD_WKEY = 0x8F1D312A

    RTC_CNTL_WDTCONFIG0_REG = RTCCNTL_BASE_REG + 0x0098
    RTC_CNTL_WDTCONFIG1_REG = RTCCNTL_BASE_REG + 0x009C
    RTC_CNTL_WDTWPROTECT_REG = RTCCNTL_BASE_REG + 0x00B0
    RTC_CNTL_WDT_WKEY = 0x50D83AA1

    USB_RAM_BLOCK = 0x800  # Max block size USB-OTG is used

    GPIO_STRAP_REG = 0x60004038
    GPIO_STRAP_SPI_BOOT_MASK = 1 << 3  # Not download mode
    RTC_CNTL_OPTION1_REG = 0x6000812C
    RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK = 0x1  # Is download mode forced over USB?

    UART_CLKDIV_REG = 0x60000014

    MEMORY_MAP = [[0x00000000, 0x00010000, "PADDING"],
                  [0x3C000000, 0x3D000000, "DROM"],
                  [0x3D000000, 0x3E000000, "EXTRAM_DATA"],
                  [0x600FE000, 0x60100000, "RTC_DRAM"],
                  [0x3FC88000, 0x3FD00000, "BYTE_ACCESSIBLE"],
                  [0x3FC88000, 0x403E2000, "MEM_INTERNAL"],
                  [0x3FC88000, 0x3FD00000, "DRAM"],
                  [0x40000000, 0x4001A100, "IROM_MASK"],
                  [0x40370000, 0x403E0000, "IRAM"],
                  [0x600FE000, 0x60100000, "RTC_IRAM"],
                  [0x42000000, 0x42800000, "IROM"],
                  [0x50000000, 0x50002000, "RTC_DATA"]]

    # Returns old version format (ECO number). Use the new format get_chip_full_revision().
    def get_chip_revision(self):
        return self.get_minor_chip_version()

    def get_pkg_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 21) & 0x07

    def is_eco0(self, minor_raw):
        # Workaround: The major version field was allocated to other purposes
        # when block version is v1.1.
        # Luckily only chip v0.0 have this kind of block version and efuse usage.
        return (
            (minor_raw & 0x7) == 0 and self.get_blk_version_major() == 1 and self.get_blk_version_minor() == 1
        )

    def get_minor_chip_version(self):
        minor_raw = self.get_raw_minor_chip_version()
        if self.is_eco0(minor_raw):
            return 0
        return minor_raw

    def get_raw_minor_chip_version(self):
        hi_num_word = 5
        hi = (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * hi_num_word)) >> 23) & 0x01
        low_num_word = 3
        low = (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * low_num_word)) >> 18) & 0x07
        return (hi << 3) + low

    def get_blk_version_major(self):
        num_word = 4
        return (self.read_reg(self.EFUSE_BLOCK2_ADDR + (4 * num_word)) >> 0) & 0x03

    def get_blk_version_minor(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 24) & 0x07

    def get_major_chip_version(self):
        minor_raw = self.get_raw_minor_chip_version()
        if self.is_eco0(minor_raw):
            return 0
        return self.get_raw_major_chip_version()

    def get_raw_major_chip_version(self):
        num_word = 5
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 24) & 0x03

    def get_chip_description(self):
        major_rev = self.get_major_chip_version()
        minor_rev = self.get_minor_chip_version()
        return "%s (revision v%d.%d)" % (self.CHIP_NAME, major_rev, minor_rev)

    def get_chip_features(self):
        return ["WiFi", "BLE"]

    def get_crystal_freq(self):
        # ESP32S3 XTAL is fixed to 40MHz
        return 40

    def get_flash_crypt_config(self):
        return None  # doesn't exist on ESP32-S3

    def get_key_block_purpose(self, key_block):
        if key_block < 0 or key_block > 5:
            raise FatalError("Valid key block numbers must be in range 0-5")

        reg, shift = [(self.EFUSE_PURPOSE_KEY0_REG, self.EFUSE_PURPOSE_KEY0_SHIFT),
                      (self.EFUSE_PURPOSE_KEY1_REG, self.EFUSE_PURPOSE_KEY1_SHIFT),
                      (self.EFUSE_PURPOSE_KEY2_REG, self.EFUSE_PURPOSE_KEY2_SHIFT),
                      (self.EFUSE_PURPOSE_KEY3_REG, self.EFUSE_PURPOSE_KEY3_SHIFT),
                      (self.EFUSE_PURPOSE_KEY4_REG, self.EFUSE_PURPOSE_KEY4_SHIFT),
                      (self.EFUSE_PURPOSE_KEY5_REG, self.EFUSE_PURPOSE_KEY5_SHIFT)][key_block]
        return (self.read_reg(reg) >> shift) & 0xF

    def is_flash_encryption_key_valid(self):
        # Need to see either an AES-128 key or two AES-256 keys
        purposes = [self.get_key_block_purpose(b) for b in range(6)]

        if any(p == self.PURPOSE_VAL_XTS_AES128_KEY for p in purposes):
            return True

        return any(p == self.PURPOSE_VAL_XTS_AES256_KEY_1 for p in purposes) \
            and any(p == self.PURPOSE_VAL_XTS_AES256_KEY_2 for p in purposes)

    def override_vddsdio(self, new_voltage):
        raise NotImplementedInROMError("VDD_SDIO overrides are not supported for ESP32-S3")

    def read_mac(self):
        mac0 = self.read_reg(self.MAC_EFUSE_REG)
        mac1 = self.read_reg(self.MAC_EFUSE_REG + 4)  # only bottom 16 bits are MAC
        bitstring = struct.pack(">II", mac1, mac0)[2:]
        try:
            return tuple(ord(b) for b in bitstring)
        except TypeError:  # Python 3, bitstring elements are already bytes
            return tuple(bitstring)

    def uses_usb(self, _cache=[]):
        if self.secure_download_mode:
            return False  # can't detect native USB in secure download mode
        if not _cache:
            buf_no = self.read_reg(self.UARTDEV_BUF_NO) & 0xff
            _cache.append(buf_no == self.UARTDEV_BUF_NO_USB)
        return _cache[0]

    def uses_usb_jtag_serial(self, _cache=[]):
        """
        Check the UARTDEV_BUF_NO register to see if USB-JTAG/Serial is being used
        """
        if self.secure_download_mode:
            return False  # can't detect USB-JTAG/Serial in secure download mode
        if not _cache:
            buf_no = self.read_reg(self.UARTDEV_BUF_NO) & 0xff
            _cache.append(buf_no == self.UARTDEV_BUF_NO_USB_JTAG_SERIAL)
        return _cache[0]

    def _post_connect(self):
        if self.uses_usb():
            self.ESP_RAM_BLOCK = self.USB_RAM_BLOCK

    def rtc_wdt_reset(self):
        print("Hard resetting with RTC WDT...")
        self.write_reg(self.RTC_CNTL_WDTWPROTECT_REG, self.RTC_CNTL_WDT_WKEY)  # unlock
        self.write_reg(self.RTC_CNTL_WDTCONFIG1_REG, 5000)  # set WDT timeout
        self.write_reg(
            self.RTC_CNTL_WDTCONFIG0_REG, (1 << 31) | (5 << 28) | (1 << 8) | 2
        )  # enable WDT
        self.write_reg(self.RTC_CNTL_WDTWPROTECT_REG, 0)  # lock

    def hard_reset(self):
        try:
            # Clear force download boot mode to avoid the chip being stuck in download mode after reset
            # workaround for issue: https://github.com/espressif/arduino-esp32/issues/6762
            self.write_reg(
                self.RTC_CNTL_OPTION1_REG, 0, self.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK
            )
        except Exception:
            # Skip if response was not valid and proceed to reset; e.g. when monitoring while resetting
            pass
        uses_usb_otg = self.uses_usb()
        if uses_usb_otg or self.uses_usb_jtag_serial():
            # Check the strapping register to see if we can perform RTC WDT reset
            strap_reg = self.read_reg(self.GPIO_STRAP_REG)
            force_dl_reg = self.read_reg(self.RTC_CNTL_OPTION1_REG)
            if (
                strap_reg & self.GPIO_STRAP_SPI_BOOT_MASK == 0  # GPIO0 low
                and force_dl_reg & self.RTC_CNTL_FORCE_DOWNLOAD_BOOT_MASK == 0
            ):
                self.rtc_wdt_reset()
                return

        print('Hard resetting via RTS pin...')
        self._setRTS(True)  # EN->LOW
        if self.uses_usb():
            # Give the chip some time to come out of reset, to be able to handle further DTR/RTS transitions
            time.sleep(0.2)
            self._setRTS(False)
            time.sleep(0.2)
        else:
            time.sleep(0.1)
            self._setRTS(False)


class ESP32C3ROM(ESP32ROM):
    CHIP_NAME = "ESP32-C3"
    IMAGE_CHIP_ID = 5

    FPGA_SLOW_BOOT = False

    IROM_MAP_START = 0x42000000
    IROM_MAP_END = 0x42800000
    DROM_MAP_START = 0x3C000000
    DROM_MAP_END = 0x3C800000

    SPI_REG_BASE = 0x60002000
    SPI_USR_OFFS = 0x18
    SPI_USR1_OFFS = 0x1C
    SPI_USR2_OFFS = 0x20
    SPI_MOSI_DLEN_OFFS = 0x24
    SPI_MISO_DLEN_OFFS = 0x28
    SPI_W0_OFFS = 0x58

    SPI_ADDR_REG_MSB = False

    BOOTLOADER_FLASH_OFFSET = 0x0

    # Magic values for ESP32-C3 eco 1+2, eco 3, eco 6, and eco 7 respectively
    CHIP_DETECT_MAGIC_VALUE = [0x6921506F, 0x1B31506F, 0x4881606F, 0x4361606F]

    UART_DATE_REG_ADDR = 0x60000000 + 0x7C

    UART_CLKDIV_REG = 0x60000014

    EFUSE_BASE = 0x60008800
    EFUSE_BLOCK1_ADDR = EFUSE_BASE + 0x044
    MAC_EFUSE_REG = EFUSE_BASE + 0x044

    EFUSE_RD_REG_BASE = EFUSE_BASE + 0x030  # BLOCK0 read base address

    EFUSE_PURPOSE_KEY0_REG = EFUSE_BASE + 0x34
    EFUSE_PURPOSE_KEY0_SHIFT = 24
    EFUSE_PURPOSE_KEY1_REG = EFUSE_BASE + 0x34
    EFUSE_PURPOSE_KEY1_SHIFT = 28
    EFUSE_PURPOSE_KEY2_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY2_SHIFT = 0
    EFUSE_PURPOSE_KEY3_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY3_SHIFT = 4
    EFUSE_PURPOSE_KEY4_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY4_SHIFT = 8
    EFUSE_PURPOSE_KEY5_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY5_SHIFT = 12

    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT_REG = EFUSE_RD_REG_BASE
    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT = 1 << 20

    EFUSE_SPI_BOOT_CRYPT_CNT_REG = EFUSE_BASE + 0x034
    EFUSE_SPI_BOOT_CRYPT_CNT_MASK = 0x7 << 18

    EFUSE_SECURE_BOOT_EN_REG = EFUSE_BASE + 0x038
    EFUSE_SECURE_BOOT_EN_MASK = 1 << 20

    PURPOSE_VAL_XTS_AES128_KEY = 4

    GPIO_STRAP_REG = 0x3f404038

    SUPPORTS_ENCRYPTED_FLASH = True

    FLASH_ENCRYPTED_WRITE_ALIGN = 16

    UARTDEV_BUF_NO = 0x3FCDF07C  # Variable in ROM .bss which indicates the port in use
    UARTDEV_BUF_NO_USB_JTAG_SERIAL = 3  # The above var when USB-JTAG/Serial is used

    RTCCNTL_BASE_REG = 0x60008000
    RTC_CNTL_SWD_CONF_REG = RTCCNTL_BASE_REG + 0x00AC
    RTC_CNTL_SWD_AUTO_FEED_EN = 1 << 31
    RTC_CNTL_SWD_WPROTECT_REG = RTCCNTL_BASE_REG + 0x00B0
    RTC_CNTL_SWD_WKEY = 0x8F1D312A

    RTC_CNTL_WDTCONFIG0_REG = RTCCNTL_BASE_REG + 0x0090
    RTC_CNTL_WDTCONFIG1_REG = RTCCNTL_BASE_REG + 0x0094
    RTC_CNTL_WDTWPROTECT_REG = RTCCNTL_BASE_REG + 0x00A8
    RTC_CNTL_WDT_WKEY = 0x50D83AA1

    MEMORY_MAP = [
        [0x00000000, 0x00010000, "PADDING"],
        [0x3C000000, 0x3C800000, "DROM"],
        [0x3FC80000, 0x3FCE0000, "DRAM"],
        [0x3FC88000, 0x3FD00000, "BYTE_ACCESSIBLE"],
        [0x3FF00000, 0x3FF20000, "DROM_MASK"],
        [0x40000000, 0x40060000, "IROM_MASK"],
        [0x42000000, 0x42800000, "IROM"],
        [0x4037C000, 0x403E0000, "IRAM"],
        [0x50000000, 0x50002000, "RTC_IRAM"],
        [0x50000000, 0x50002000, "RTC_DRAM"],
        [0x600FE000, 0x60100000, "MEM_INTERNAL2"],
    ]

    UF2_FAMILY_ID = 0xD42BA06C

    EFUSE_MAX_KEY = 5
    KEY_PURPOSES: Dict[int, str] = {
        0: "USER/EMPTY",
        1: "RESERVED",
        4: "XTS_AES_128_KEY",
        5: "HMAC_DOWN_ALL",
        6: "HMAC_DOWN_JTAG",
        7: "HMAC_DOWN_DIGITAL_SIGNATURE",
        8: "HMAC_UP",
        9: "SECURE_BOOT_DIGEST0",
        10: "SECURE_BOOT_DIGEST1",
        11: "SECURE_BOOT_DIGEST2",
    }

    # Returns old version format (ECO number). Use the new format get_chip_full_revision().
    def get_chip_revision(self):
        return self.get_minor_chip_version()

    def get_pkg_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 21) & 0x07

    def get_minor_chip_version(self):
        hi_num_word = 5
        hi = (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * hi_num_word)) >> 23) & 0x01
        low_num_word = 3
        low = (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * low_num_word)) >> 18) & 0x07
        return (hi << 3) + low

    def get_major_chip_version(self):
        num_word = 5
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 24) & 0x03

    def get_flash_cap(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 27) & 0x07

    def get_flash_vendor(self):
        num_word = 4
        vendor_id = (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 0) & 0x07
        return {1: "XMC", 2: "GD", 3: "FM", 4: "TT", 5: "ZBIT"}.get(vendor_id, "")

    def get_chip_description(self):
        chip_name = {
            0: "ESP32-C3 (QFN32)",
            1: "ESP8685 (QFN28)",
            2: "ESP32-C3 AZ (QFN32)",
            3: "ESP8686 (QFN24)",
        }.get(self.get_pkg_version(), "unknown ESP32-C3")
        major_rev = self.get_major_chip_version()
        minor_rev = self.get_minor_chip_version()
        return f"{chip_name} (revision v{major_rev}.{minor_rev})"

    def get_chip_features(self):
        features = ["WiFi", "BLE"]

        flash = {
            0: None,
            1: "Embedded Flash 4MB",
            2: "Embedded Flash 2MB",
            3: "Embedded Flash 1MB",
            4: "Embedded Flash 8MB",
        }.get(self.get_flash_cap(), "Unknown Embedded Flash")
        if flash is not None:
            features += [flash + f" ({self.get_flash_vendor()})"]
        return features

    def get_crystal_freq(self):
        # ESP32C3 XTAL is fixed to 40MHz
        return 40

    def get_flash_voltage(self):
        pass  # not supported on ESP32-C3

    def override_vddsdio(self, new_voltage):
        raise NotImplementedInROMError(
            "VDD_SDIO overrides are not supported for ESP32-C3"
        )

    def read_mac(self, mac_type="BASE_MAC"):
        """Read MAC from EFUSE region"""
        if mac_type != "BASE_MAC":
            return None
        mac0 = self.read_reg(self.MAC_EFUSE_REG)
        mac1 = self.read_reg(self.MAC_EFUSE_REG + 4)  # only bottom 16 bits are MAC
        bitstring = struct.pack(">II", mac1, mac0)[2:]
        return tuple(bitstring)

    def get_flash_crypt_config(self):
        return None  # doesn't exist on ESP32-C3

    def get_secure_boot_enabled(self):
        return (
            self.read_reg(self.EFUSE_SECURE_BOOT_EN_REG)
            & self.EFUSE_SECURE_BOOT_EN_MASK
        )

    def get_key_block_purpose(self, key_block):
        if key_block < 0 or key_block > self.EFUSE_MAX_KEY:
            raise FatalError(
                f"Valid key block numbers must be in range 0-{self.EFUSE_MAX_KEY}"
            )

        reg, shift = [
            (self.EFUSE_PURPOSE_KEY0_REG, self.EFUSE_PURPOSE_KEY0_SHIFT),
            (self.EFUSE_PURPOSE_KEY1_REG, self.EFUSE_PURPOSE_KEY1_SHIFT),
            (self.EFUSE_PURPOSE_KEY2_REG, self.EFUSE_PURPOSE_KEY2_SHIFT),
            (self.EFUSE_PURPOSE_KEY3_REG, self.EFUSE_PURPOSE_KEY3_SHIFT),
            (self.EFUSE_PURPOSE_KEY4_REG, self.EFUSE_PURPOSE_KEY4_SHIFT),
            (self.EFUSE_PURPOSE_KEY5_REG, self.EFUSE_PURPOSE_KEY5_SHIFT),
        ][key_block]
        return (self.read_reg(reg) >> shift) & 0xF

    def is_flash_encryption_key_valid(self):
        # Need to see an AES-128 key
        purposes = [
            self.get_key_block_purpose(b) for b in range(self.EFUSE_MAX_KEY + 1)
        ]

        return any(p == self.PURPOSE_VAL_XTS_AES128_KEY for p in purposes)

    def uses_usb_jtag_serial(self, _cache=[]):
        """
        Check the UARTDEV_BUF_NO register to see if USB-JTAG/Serial is being used
        """
        if self.secure_download_mode:
            return False  # can't detect USB-JTAG/Serial in secure download mode
        if not _cache:
            buf_no = self.read_reg(self.UARTDEV_BUF_NO) & 0xff
            _cache.append(buf_no == self.UARTDEV_BUF_NO_USB_JTAG_SERIAL)
        return _cache[0]

    def disable_watchdogs(self):
        # When USB-JTAG/Serial is used, the RTC WDT and SWD watchdog are not reset
        # and can then reset the board during flashing. Disable or autofeed them.
        if self.uses_usb_jtag_serial():
            # Disable RTC WDT
            self.write_reg(self.RTC_CNTL_WDTWPROTECT_REG, self.RTC_CNTL_WDT_WKEY)
            self.write_reg(self.RTC_CNTL_WDTCONFIG0_REG, 0)
            self.write_reg(self.RTC_CNTL_WDTWPROTECT_REG, 0)

            # Automatically feed SWD
            self.write_reg(self.RTC_CNTL_SWD_WPROTECT_REG, self.RTC_CNTL_SWD_WKEY)
            self.write_reg(
                self.RTC_CNTL_SWD_CONF_REG,
                self.read_reg(self.RTC_CNTL_SWD_CONF_REG)
                | self.RTC_CNTL_SWD_AUTO_FEED_EN,
            )
            self.write_reg(self.RTC_CNTL_SWD_WPROTECT_REG, 0)

    def _post_connect(self):
        if not self.sync_stub_detected:  # Don't run if stub is reused
            self.disable_watchdogs()

    def hard_reset(self):
        if self.uses_usb_jtag_serial():
            self.rtc_wdt_reset()
        else:
            print('Hard resetting via RTS pin...')
            self._setRTS(True)  # EN->LOW
            time.sleep(0.1)
            self._setRTS(False)

    def rtc_wdt_reset(self):
        print("Hard resetting with RTC WDT...")
        self.write_reg(self.RTC_CNTL_WDTWPROTECT_REG, self.RTC_CNTL_WDT_WKEY)  # unlock
        self.write_reg(self.RTC_CNTL_WDTCONFIG1_REG, 5000)  # set WDT timeout
        self.write_reg(
            self.RTC_CNTL_WDTCONFIG0_REG, (1 << 31) | (5 << 28) | (1 << 8) | 2
        )  # enable WDT
        self.write_reg(self.RTC_CNTL_WDTWPROTECT_REG, 0)  # lock


class ESP32C6ROM(ESP32C3ROM):
    CHIP_NAME = "ESP32-C6"
    IMAGE_CHIP_ID = 13

    FPGA_SLOW_BOOT = False

    IROM_MAP_START = 0x42000000
    IROM_MAP_END = 0x42800000
    DROM_MAP_START = 0x42800000
    DROM_MAP_END = 0x43000000

    BOOTLOADER_FLASH_OFFSET = 0x0

    # Magic value for ESP32C6
    CHIP_DETECT_MAGIC_VALUE = [0x2CE0806F]

    SPI_REG_BASE = 0x60003000
    SPI_USR_OFFS = 0x18
    SPI_USR1_OFFS = 0x1C
    SPI_USR2_OFFS = 0x20
    SPI_MOSI_DLEN_OFFS = 0x24
    SPI_MISO_DLEN_OFFS = 0x28
    SPI_W0_OFFS = 0x58

    UART_DATE_REG_ADDR = 0x60000000 + 0x7C

    EFUSE_BASE = 0x600B0800
    EFUSE_BLOCK1_ADDR = EFUSE_BASE + 0x044
    MAC_EFUSE_REG = EFUSE_BASE + 0x044

    EFUSE_RD_REG_BASE = EFUSE_BASE + 0x030  # BLOCK0 read base address

    EFUSE_PURPOSE_KEY0_REG = EFUSE_BASE + 0x34
    EFUSE_PURPOSE_KEY0_SHIFT = 24
    EFUSE_PURPOSE_KEY1_REG = EFUSE_BASE + 0x34
    EFUSE_PURPOSE_KEY1_SHIFT = 28
    EFUSE_PURPOSE_KEY2_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY2_SHIFT = 0
    EFUSE_PURPOSE_KEY3_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY3_SHIFT = 4
    EFUSE_PURPOSE_KEY4_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY4_SHIFT = 8
    EFUSE_PURPOSE_KEY5_REG = EFUSE_BASE + 0x38
    EFUSE_PURPOSE_KEY5_SHIFT = 12

    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT_REG = EFUSE_RD_REG_BASE
    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT = 1 << 20

    EFUSE_SPI_BOOT_CRYPT_CNT_REG = EFUSE_BASE + 0x034
    EFUSE_SPI_BOOT_CRYPT_CNT_MASK = 0x7 << 18

    EFUSE_SECURE_BOOT_EN_REG = EFUSE_BASE + 0x038
    EFUSE_SECURE_BOOT_EN_MASK = 1 << 20

    PURPOSE_VAL_XTS_AES128_KEY = 4

    SUPPORTS_ENCRYPTED_FLASH = True

    FLASH_ENCRYPTED_WRITE_ALIGN = 16

    UARTDEV_BUF_NO = 0x4087F580  # Variable in ROM .bss which indicates the port in use
    UARTDEV_BUF_NO_USB_JTAG_SERIAL = 3  # The above var when USB-JTAG/Serial is used

    DR_REG_LP_WDT_BASE = 0x600B1C00
    RTC_CNTL_WDTCONFIG0_REG = DR_REG_LP_WDT_BASE + 0x0  # LP_WDT_RWDT_CONFIG0_REG
    RTC_CNTL_WDTWPROTECT_REG = DR_REG_LP_WDT_BASE + 0x0018  # LP_WDT_RWDT_WPROTECT_REG

    RTC_CNTL_SWD_CONF_REG = DR_REG_LP_WDT_BASE + 0x001C  # LP_WDT_SWD_CONFIG_REG
    RTC_CNTL_SWD_AUTO_FEED_EN = 1 << 18
    RTC_CNTL_SWD_WPROTECT_REG = DR_REG_LP_WDT_BASE + 0x0020  # LP_WDT_SWD_WPROTECT_REG
    RTC_CNTL_SWD_WKEY = 0x50D83AA1  # LP_WDT_SWD_WKEY, same as WDT key in this case

    FLASH_FREQUENCY = {
        "80m": 0x0,  # workaround for wrong mspi HS div value in ROM
        "40m": 0x0,
        "20m": 0x2,
    }

    MEMORY_MAP = [
        [0x00000000, 0x00010000, "PADDING"],
        [0x42800000, 0x43000000, "DROM"],
        [0x40800000, 0x40880000, "DRAM"],
        [0x40800000, 0x40880000, "BYTE_ACCESSIBLE"],
        [0x4004AC00, 0x40050000, "DROM_MASK"],
        [0x40000000, 0x4004AC00, "IROM_MASK"],
        [0x42000000, 0x42800000, "IROM"],
        [0x40800000, 0x40880000, "IRAM"],
        [0x50000000, 0x50004000, "RTC_IRAM"],
        [0x50000000, 0x50004000, "RTC_DRAM"],
        [0x600FE000, 0x60100000, "MEM_INTERNAL2"],
    ]

    UF2_FAMILY_ID = 0x540DDF62

    # Returns old version format (ECO number). Use the new format get_chip_full_revision().
    def get_chip_revision(self):
        return self.get_major_chip_version()

    def get_pkg_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 24) & 0x07

    def get_minor_chip_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 18) & 0x0F

    def get_major_chip_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 22) & 0x03

    def get_chip_description(self):
        chip_name = {
            0: "ESP32-C6 (QFN40)",
            1: "ESP32-C6FH4 (QFN32)",
        }.get(self.get_pkg_version(), "unknown ESP32-C6")
        major_rev = self.get_major_chip_version()
        minor_rev = self.get_minor_chip_version()
        return f"{chip_name} (revision v{major_rev}.{minor_rev})"

    def get_chip_features(self):
        return ["WiFi 6", "BT 5", "IEEE802.15.4"]

    def get_crystal_freq(self):
        # ESP32C6 XTAL is fixed to 40MHz
        return 40

    def override_vddsdio(self, new_voltage):
        raise NotImplementedInROMError(
            "VDD_SDIO overrides are not supported for ESP32-C6"
        )

    def read_mac(self, mac_type="BASE_MAC"):
        """Read MAC from EFUSE region"""
        mac0 = self.read_reg(self.MAC_EFUSE_REG)
        mac1 = self.read_reg(self.MAC_EFUSE_REG + 4)  # only bottom 16 bits are MAC
        base_mac = struct.pack(">II", mac1, mac0)[2:]
        ext_mac = struct.pack(">H", (mac1 >> 16) & 0xFFFF)
        eui64 = base_mac[0:3] + ext_mac + base_mac[3:6]
        # BASE MAC: 60:55:f9:f7:2c:a2
        # EUI64 MAC: 60:55:f9:ff:fe:f7:2c:a2
        # EXT_MAC: ff:fe
        macs = {
            "BASE_MAC": tuple(base_mac),
            "EUI64": tuple(eui64),
            "MAC_EXT": tuple(ext_mac),
        }
        return macs.get(mac_type, None)

    def get_flash_crypt_config(self):
        return None  # doesn't exist on ESP32-C6

    def get_secure_boot_enabled(self):
        return (
            self.read_reg(self.EFUSE_SECURE_BOOT_EN_REG)
            & self.EFUSE_SECURE_BOOT_EN_MASK
        )

    def get_key_block_purpose(self, key_block):
        if key_block < 0 or key_block > 5:
            raise FatalError("Valid key block numbers must be in range 0-5")

        reg, shift = [
            (self.EFUSE_PURPOSE_KEY0_REG, self.EFUSE_PURPOSE_KEY0_SHIFT),
            (self.EFUSE_PURPOSE_KEY1_REG, self.EFUSE_PURPOSE_KEY1_SHIFT),
            (self.EFUSE_PURPOSE_KEY2_REG, self.EFUSE_PURPOSE_KEY2_SHIFT),
            (self.EFUSE_PURPOSE_KEY3_REG, self.EFUSE_PURPOSE_KEY3_SHIFT),
            (self.EFUSE_PURPOSE_KEY4_REG, self.EFUSE_PURPOSE_KEY4_SHIFT),
            (self.EFUSE_PURPOSE_KEY5_REG, self.EFUSE_PURPOSE_KEY5_SHIFT),
        ][key_block]
        return (self.read_reg(reg) >> shift) & 0xF

    def is_flash_encryption_key_valid(self):
        # Need to see an AES-128 key
        purposes = [self.get_key_block_purpose(b) for b in range(6)]

        return any(p == self.PURPOSE_VAL_XTS_AES128_KEY for p in purposes)


class ESP32H2ROM(ESP32C6ROM):
    CHIP_NAME = "ESP32-H2"
    IMAGE_CHIP_ID = 16

    # Magic value for ESP32H2
    CHIP_DETECT_MAGIC_VALUE = [0xD7B73E80]

    DR_REG_LP_WDT_BASE = 0x600B1C00
    RTC_CNTL_WDTCONFIG0_REG = DR_REG_LP_WDT_BASE + 0x0  # LP_WDT_RWDT_CONFIG0_REG
    RTC_CNTL_WDTWPROTECT_REG = DR_REG_LP_WDT_BASE + 0x001C  # LP_WDT_RWDT_WPROTECT_REG

    RTC_CNTL_SWD_CONF_REG = DR_REG_LP_WDT_BASE + 0x0020  # LP_WDT_SWD_CONFIG_REG
    RTC_CNTL_SWD_AUTO_FEED_EN = 1 << 18
    RTC_CNTL_SWD_WPROTECT_REG = DR_REG_LP_WDT_BASE + 0x0024  # LP_WDT_SWD_WPROTECT_REG
    RTC_CNTL_SWD_WKEY = 0x50D83AA1  # LP_WDT_SWD_WKEY, same as WDT key in this case

    FLASH_FREQUENCY = {
        "48m": 0xF,
        "24m": 0x0,
        "16m": 0x1,
        "12m": 0x2,
    }

    UF2_FAMILY_ID = 0x332726F6

    # Returns old version format (ECO number). Use the new format get_chip_full_revision().
    def get_chip_revision(self):
        return self.get_major_chip_version()

    def get_pkg_version(self):
        num_word = 4
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 0) & 0x07

    def get_minor_chip_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 18) & 0x07

    def get_major_chip_version(self):
        num_word = 3
        return (self.read_reg(self.EFUSE_BLOCK1_ADDR + (4 * num_word)) >> 21) & 0x03

    def get_chip_description(self):
        chip_name = {
            0: "ESP32-H2",
        }.get(self.get_pkg_version(), "unknown ESP32-H2")
        major_rev = self.get_major_chip_version()
        minor_rev = self.get_minor_chip_version()
        return f"{chip_name} (revision v{major_rev}.{minor_rev})"

    def get_chip_features(self):
        return ["BT 5", "IEEE802.15.4"]

    def get_crystal_freq(self):
        # ESP32H2 XTAL is fixed to 32MHz
        return 32


class ESP32C2ROM(ESP32C3ROM):
    CHIP_NAME = "ESP32-C2"
    IMAGE_CHIP_ID = 12

    IROM_MAP_START = 0x42000000
    IROM_MAP_END = 0x42400000
    DROM_MAP_START = 0x3C000000
    DROM_MAP_END = 0x3C400000

    # Magic value for ESP32C2 ECO0 , ECO1 and ECO4 respectively
    CHIP_DETECT_MAGIC_VALUE = [0x6F51306F, 0x7C41A06F, 0x0C21E06F]

    EFUSE_BASE = 0x60008800
    EFUSE_BLOCK2_ADDR = EFUSE_BASE + 0x040
    MAC_EFUSE_REG = EFUSE_BASE + 0x040

    EFUSE_SECURE_BOOT_EN_REG = EFUSE_BASE + 0x30
    EFUSE_SECURE_BOOT_EN_MASK = 1 << 21

    EFUSE_SPI_BOOT_CRYPT_CNT_REG = EFUSE_BASE + 0x30
    EFUSE_SPI_BOOT_CRYPT_CNT_MASK = 0x7 << 18

    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT_REG = EFUSE_BASE + 0x30
    EFUSE_DIS_DOWNLOAD_MANUAL_ENCRYPT = 1 << 6

    EFUSE_XTS_KEY_LENGTH_256_REG = EFUSE_BASE + 0x30
    EFUSE_XTS_KEY_LENGTH_256 = 1 << 10

    EFUSE_BLOCK_KEY0_REG = EFUSE_BASE + 0x60

    EFUSE_RD_DIS_REG = EFUSE_BASE + 0x30
    EFUSE_RD_DIS = 3

    FLASH_FREQUENCY = {
        "60m": 0xF,
        "30m": 0x0,
        "20m": 0x1,
        "15m": 0x2,
    }

    MEMORY_MAP = [
        [0x00000000, 0x00010000, "PADDING"],
        [0x3C000000, 0x3C400000, "DROM"],
        [0x3FCA0000, 0x3FCE0000, "DRAM"],
        [0x3FC88000, 0x3FD00000, "BYTE_ACCESSIBLE"],
        [0x3FF00000, 0x3FF50000, "DROM_MASK"],
        [0x40000000, 0x40090000, "IROM_MASK"],
        [0x42000000, 0x42400000, "IROM"],
        [0x4037C000, 0x403C0000, "IRAM"],
    ]

    UF2_FAMILY_ID = 0x2B88D29C

    # Returns old version format (ECO number). Use the new format get_chip_full_revision().
    def get_chip_revision(self):
        return self.get_major_chip_version()

    def get_pkg_version(self):
        num_word = 1
        return (self.read_reg(self.EFUSE_BLOCK2_ADDR + (4 * num_word)) >> 22) & 0x07

    def get_chip_description(self):
        chip_name = {
            0: "ESP32-C2",
            1: "ESP32-C2",
        }.get(self.get_pkg_version(), "unknown ESP32-C2")
        major_rev = self.get_major_chip_version()
        minor_rev = self.get_minor_chip_version()
        return f"{chip_name} (revision v{major_rev}.{minor_rev})"

    def get_chip_features(self):
        return ["Wi-Fi", "BLE"]

    def get_minor_chip_version(self):
        num_word = 1
        return (self.read_reg(self.EFUSE_BLOCK2_ADDR + (4 * num_word)) >> 16) & 0xF

    def get_major_chip_version(self):
        num_word = 1
        return (self.read_reg(self.EFUSE_BLOCK2_ADDR + (4 * num_word)) >> 20) & 0x3

    def get_crystal_freq(self):
        # The crystal detection algorithm of ESP32/ESP8266 works for ESP32-C2 as well.
        return ESPLoader.get_crystal_freq(self)

    def change_baud(self, baud):
        rom_with_26M_XTAL = not self.IS_STUB and self.get_crystal_freq() == 26
        if rom_with_26M_XTAL:
            # The code is copied over from ESPLoader.change_baud().
            # Probably this is just a temporary solution until the next chip revision.

            # The ROM code thinks it uses a 40 MHz XTAL. Recompute the baud rate
            # in order to trick the ROM code to set the correct baud rate for
            # a 26 MHz XTAL.
            false_rom_baud = baud * 40 // 26

            print(f"Changing baud rate to {baud}")
            self.command(
                self.ESP_CHANGE_BAUDRATE, struct.pack("<II", false_rom_baud, 0)
            )
            print("Changed.")
            self._set_port_baudrate(baud)
            time.sleep(0.05)  # get rid of garbage sent during baud rate change
            self.flush_input()
        else:
            ESPLoader.change_baud(self, baud)

    def _post_connect(self):
        # ESP32C2 ECO0 is no longer supported by the flasher stub
        if not self.secure_download_mode and self.get_chip_revision() == 0:
            self.stub_is_disabled = True
            self.IS_STUB = False

    """ Try to read (encryption key) and check if it is valid """

    def is_flash_encryption_key_valid(self):
        key_len_256 = (
            self.read_reg(self.EFUSE_XTS_KEY_LENGTH_256_REG)
            & self.EFUSE_XTS_KEY_LENGTH_256
        )

        word0 = self.read_reg(self.EFUSE_RD_DIS_REG) & self.EFUSE_RD_DIS
        rd_disable = word0 == 3 if key_len_256 else word0 == 1

        # reading of BLOCK3 is NOT ALLOWED so we assume valid key is programmed
        if rd_disable:
            return True
        else:
            # reading of BLOCK3 is ALLOWED so we will read and verify for non-zero.
            # When chip has not generated AES/encryption key in BLOCK3,
            # the contents will be readable and 0.
            # If the flash encryption is enabled it is expected to have a valid
            # non-zero key. We break out on first occurance of non-zero value
            key_word = [0] * 7 if key_len_256 else [0] * 3
            for i in range(len(key_word)):
                key_word[i] = self.read_reg(self.EFUSE_BLOCK_KEY0_REG + i * 4)
                # key is non-zero so break & return
                if key_word[i] != 0:
                    return True
            return False


class ESP32StubLoader(ESP32ROM):
    """ Access class for ESP32 stub loader, runs on top of ROM.
    """
    FLASH_WRITE_SIZE = 0x4000  # matches MAX_WRITE_BLOCK in stub_loader.c
    STATUS_BYTES_LENGTH = 2  # same as ESP8266, different to ESP32 ROM
    IS_STUB = True

    def __init__(self, rom_loader):
        self.secure_download_mode = rom_loader.secure_download_mode
        self._port = rom_loader._port
        self._trace_enabled = rom_loader._trace_enabled
        self.flush_input()  # resets _slip_reader


ESP32ROM.STUB_CLASS = ESP32StubLoader


class ESP32S2StubLoader(ESP32S2ROM):
    """ Access class for ESP32-S2 stub loader, runs on top of ROM.

    (Basically the same as ESP32StubLoader, but different base class.
    Can possibly be made into a mixin.)
    """
    FLASH_WRITE_SIZE = 0x4000  # matches MAX_WRITE_BLOCK in stub_loader.c
    STATUS_BYTES_LENGTH = 2  # same as ESP8266, different to ESP32 ROM
    IS_STUB = True

    def __init__(self, rom_loader):
        self.secure_download_mode = rom_loader.secure_download_mode
        self._port = rom_loader._port
        self._trace_enabled = rom_loader._trace_enabled
        self.flush_input()  # resets _slip_reader

        if rom_loader.uses_usb():
            self.ESP_RAM_BLOCK = self.USB_RAM_BLOCK
            self.FLASH_WRITE_SIZE = self.USB_RAM_BLOCK


ESP32S2ROM.STUB_CLASS = ESP32S2StubLoader


class ESP32S3StubLoader(ESP32S3ROM):
    """ Access class for ESP32S3 stub loader, runs on top of ROM.

    (Basically the same as ESP32StubLoader, but different base class.
    Can possibly be made into a mixin.)
    """
    FLASH_WRITE_SIZE = 0x4000  # matches MAX_WRITE_BLOCK in stub_loader.c
    STATUS_BYTES_LENGTH = 2  # same as ESP8266, different to ESP32 ROM
    IS_STUB = True

    def __init__(self, rom_loader):
        self.secure_download_mode = rom_loader.secure_download_mode
        self._port = rom_loader._port
        self._trace_enabled = rom_loader._trace_enabled
        self.flush_input()  # resets _slip_reader

        if rom_loader.uses_usb():
            self.ESP_RAM_BLOCK = self.USB_RAM_BLOCK
            self.FLASH_WRITE_SIZE = self.USB_RAM_BLOCK


ESP32S3ROM.STUB_CLASS = ESP32S3StubLoader


class ESP32C3StubLoader(ESP32C3ROM):
    """ Access class for ESP32C3 stub loader, runs on top of ROM.

    (Basically the same as ESP32StubLoader, but different base class.
    Can possibly be made into a mixin.)
    """
    FLASH_WRITE_SIZE = 0x4000  # matches MAX_WRITE_BLOCK in stub_loader.c
    STATUS_BYTES_LENGTH = 2  # same as ESP8266, different to ESP32 ROM
    IS_STUB = True

    def __init__(self, rom_loader):
        self.secure_download_mode = rom_loader.secure_download_mode
        self._port = rom_loader._port
        self._trace_enabled = rom_loader._trace_enabled
        self.flush_input()  # resets _slip_reader


ESP32C3ROM.STUB_CLASS = ESP32C3StubLoader


class ESP32C6StubLoader(ESP32C6ROM):
    """Access class for ESP32C6 stub loader, runs on top of ROM.

    (Basically the same as ESP32StubLoader, but different base class.
    Can possibly be made into a mixin.)
    """

    FLASH_WRITE_SIZE = 0x4000  # matches MAX_WRITE_BLOCK in stub_loader.c
    STATUS_BYTES_LENGTH = 2  # same as ESP8266, different to ESP32 ROM
    IS_STUB = True

    def __init__(self, rom_loader):
        self.secure_download_mode = rom_loader.secure_download_mode
        self._port = rom_loader._port
        self._trace_enabled = rom_loader._trace_enabled
        self.flush_input()  # resets _slip_reader


ESP32C6ROM.STUB_CLASS = ESP32C6StubLoader


class ESP32H2StubLoader(ESP32H2ROM):
    """Access class for ESP32H2 stub loader, runs on top of ROM.

    (Basically the same as ESP32StubLoader, but different base class.
    Can possibly be made into a mixin.)
    """

    FLASH_WRITE_SIZE = 0x4000  # matches MAX_WRITE_BLOCK in stub_loader.c
    STATUS_BYTES_LENGTH = 2  # same as ESP8266, different to ESP32 ROM
    IS_STUB = True

    def __init__(self, rom_loader):
        self.secure_download_mode = rom_loader.secure_download_mode
        self._port = rom_loader._port
        self._trace_enabled = rom_loader._trace_enabled
        self.flush_input()  # resets _slip_reader


ESP32H2ROM.STUB_CLASS = ESP32H2StubLoader


class ESP32C2StubLoader(ESP32C2ROM):
    """Access class for ESP32C2 stub loader, runs on top of ROM.

    (Basically the same as ESP32StubLoader, but different base class.
    Can possibly be made into a mixin.)
    """

    FLASH_WRITE_SIZE = 0x4000  # matches MAX_WRITE_BLOCK in stub_loader.c
    STATUS_BYTES_LENGTH = 2  # same as ESP8266, different to ESP32 ROM
    IS_STUB = True

    def __init__(self, rom_loader):
        self.secure_download_mode = rom_loader.secure_download_mode
        self._port = rom_loader._port
        self._trace_enabled = rom_loader._trace_enabled
        self.flush_input()  # resets _slip_reader


ESP32C2ROM.STUB_CLASS = ESP32C2StubLoader


class ESPBOOTLOADER(object):
    """ These are constants related to software ESP8266 bootloader, working with 'v2' image files """

    # First byte of the "v2" application image
    IMAGE_V2_MAGIC = 0xea

    # First 'segment' value in a "v2" application image, appears to be a constant version value?
    IMAGE_V2_SEGMENT = 4


def LoadFirmwareImage(chip, filename):
    """ Load a firmware image. Can be for any supported SoC.

        ESP8266 images will be examined to determine if they are original ROM firmware images (ESP8266ROMFirmwareImage)
        or "v2" OTA bootloader images.

        Returns a BaseFirmwareImage subclass, either ESP8266ROMFirmwareImage (v1) or ESP8266V2FirmwareImage (v2).
    """
    chip = re.sub(r"[-()]", "", chip.lower())
    with open(filename, 'rb') as f:
        if chip == 'esp32':
            return ESP32FirmwareImage(f)
        elif chip == "esp32s2":
            return ESP32S2FirmwareImage(f)
        elif chip == "esp32s3":
            return ESP32S3FirmwareImage(f)
        elif chip == 'esp32c3':
            return ESP32C3FirmwareImage(f)
        elif chip == 'esp32c6':
            return ESP32C6FirmwareImage(f)
        elif chip == 'esp32h2':
            return ESP32H2FirmwareImage(f)
        elif chip == 'esp32c2':
            return ESP32C2FirmwareImage(f)
        else:  # Otherwise, ESP8266 so look at magic to determine the image type
            magic = ord(f.read(1))
            f.seek(0)
            if magic == ESPLoader.ESP_IMAGE_MAGIC:
                return ESP8266ROMFirmwareImage(f)
            elif magic == ESPBOOTLOADER.IMAGE_V2_MAGIC:
                return ESP8266V2FirmwareImage(f)
            else:
                raise FatalError("Invalid image magic number: %d" % magic)


class ImageSegment(object):
    """ Wrapper class for a segment in an ESP image
    (very similar to a section in an ELFImage also) """

    def __init__(self, addr, data, file_offs=None):
        self.addr = addr
        self.data = data
        self.file_offs = file_offs
        self.include_in_checksum = True
        if self.addr != 0:
            self.pad_to_alignment(4)  # pad all "real" ImageSegments 4 byte aligned length

    def copy_with_new_addr(self, new_addr):
        """ Return a new ImageSegment with same data, but mapped at
        a new address. """
        return ImageSegment(new_addr, self.data, 0)

    def split_image(self, split_len):
        """ Return a new ImageSegment which splits "split_len" bytes
        from the beginning of the data. Remaining bytes are kept in
        this segment object (and the start address is adjusted to match.) """
        result = copy.copy(self)
        result.data = self.data[:split_len]
        self.data = self.data[split_len:]
        self.addr += split_len
        self.file_offs = None
        result.file_offs = None
        return result

    def __repr__(self):
        r = "len 0x%05x load 0x%08x" % (len(self.data), self.addr)
        if self.file_offs is not None:
            r += " file_offs 0x%08x" % (self.file_offs)
        return r

    def get_memory_type(self, image):
        """
        Return a list describing the memory type(s) that is covered by this
        segment's start address.
        """
        return [map_range[2] for map_range in image.ROM_LOADER.MEMORY_MAP if map_range[0] <= self.addr < map_range[1]]

    def pad_to_alignment(self, alignment):
        self.data = pad_to(self.data, alignment, b'\x00')


class ELFSection(ImageSegment):
    """ Wrapper class for a section in an ELF image, has a section
    name as well as the common properties of an ImageSegment. """

    def __init__(self, name, addr, data):
        super(ELFSection, self).__init__(addr, data)
        self.name = name.decode("utf-8")

    def __repr__(self):
        return "%s %s" % (self.name, super(ELFSection, self).__repr__())


class BaseFirmwareImage(object):
    SEG_HEADER_LEN = 8
    SHA256_DIGEST_LEN = 32

    """ Base class with common firmware image functions """

    def __init__(self):
        self.segments = []
        self.entrypoint = 0
        self.elf_sha256 = None
        self.elf_sha256_offset = 0
        self.pad_to_size = 0

    def load_common_header(self, load_file, expected_magic):
        (magic, segments, self.flash_mode, self.flash_size_freq,
         self.entrypoint) = struct.unpack('<BBBBI', load_file.read(8))

        if magic != expected_magic:
            raise FatalError('Invalid firmware image magic=0x%x' % (magic))
        return segments

    def verify(self):
        if len(self.segments) > 16:
            raise FatalError(
                'Invalid segment count %d (max 16). Usually this indicates a linker script problem.' % len(self.segments))

    def load_segment(self, f, is_irom_segment=False):
        """ Load the next segment from the image file """
        file_offs = f.tell()
        (offset, size) = struct.unpack('<II', f.read(8))
        self.warn_if_unusual_segment(offset, size, is_irom_segment)
        segment_data = f.read(size)
        if len(segment_data) < size:
            raise FatalError('End of file reading segment 0x%x, length %d (actual length %d)' %
                             (offset, size, len(segment_data)))
        segment = ImageSegment(offset, segment_data, file_offs)
        self.segments.append(segment)
        return segment

    def warn_if_unusual_segment(self, offset, size, is_irom_segment):
        if not is_irom_segment:
            if offset > 0x40200000 or offset < 0x3ffe0000 or size > 65536:
                print('WARNING: Suspicious segment 0x%x, length %d' % (offset, size))

    def maybe_patch_segment_data(self, f, segment_data):
        """If SHA256 digest of the ELF file needs to be inserted into this segment, do so. Returns segment data."""
        segment_len = len(segment_data)
        file_pos = f.tell()  # file_pos is position in the .bin file
        if self.elf_sha256_offset >= file_pos and self.elf_sha256_offset < file_pos + segment_len:
            # SHA256 digest needs to be patched into this binary segment,
            # calculate offset of the digest inside the binary segment.
            patch_offset = self.elf_sha256_offset - file_pos
            # Sanity checks
            if patch_offset < self.SEG_HEADER_LEN or patch_offset + self.SHA256_DIGEST_LEN > segment_len:
                raise FatalError('Cannot place SHA256 digest on segment boundary'
                                 '(elf_sha256_offset=%d, file_pos=%d, segment_size=%d)' %
                                 (self.elf_sha256_offset, file_pos, segment_len))
            # offset relative to the data part
            patch_offset -= self.SEG_HEADER_LEN
            if segment_data[patch_offset:patch_offset + self.SHA256_DIGEST_LEN] != b'\x00' * self.SHA256_DIGEST_LEN:
                raise FatalError('Contents of segment at SHA256 digest offset 0x%x are not all zero. Refusing to overwrite.' %
                                 self.elf_sha256_offset)
            assert len(self.elf_sha256) == self.SHA256_DIGEST_LEN
            segment_data = segment_data[0:patch_offset] + self.elf_sha256 + \
                segment_data[patch_offset + self.SHA256_DIGEST_LEN:]
        return segment_data

    def save_segment(self, f, segment, checksum=None):
        """ Save the next segment to the image file, return next checksum value if provided """
        segment_data = self.maybe_patch_segment_data(f, segment.data)
        f.write(struct.pack('<II', segment.addr, len(segment_data)))
        f.write(segment_data)
        if checksum is not None:
            return ESPLoader.checksum(segment_data, checksum)

    def save_flash_segment(self, f, segment, checksum=None):
        """
        Save the next segment to the image file, return next checksum value if provided
        """
        if self.ROM_LOADER.CHIP_NAME == "ESP32":
            # Work around a bug in ESP-IDF 2nd stage bootloader, that it didn't map the
            # last MMU page, if an IROM/DROM segment was < 0x24 bytes
            # over the page boundary.
            segment_end_pos = f.tell() + len(segment.data) + self.SEG_HEADER_LEN
            segment_len_remainder = segment_end_pos % self.IROM_ALIGN
            if segment_len_remainder < 0x24:
                segment.data += b"\x00" * (0x24 - segment_len_remainder)
        return self.save_segment(f, segment, checksum)

    def read_checksum(self, f):
        """ Return ESPLoader checksum from end of just-read image """
        # Skip the padding. The checksum is stored in the last byte so that the
        # file is a multiple of 16 bytes.
        align_file_position(f, 16)
        return ord(f.read(1))

    def calculate_checksum(self):
        """ Calculate checksum of loaded image, based on segments in
        segment array.
        """
        checksum = ESPLoader.ESP_CHECKSUM_MAGIC
        for seg in self.segments:
            if seg.include_in_checksum:
                checksum = ESPLoader.checksum(seg.data, checksum)
        return checksum

    def append_checksum(self, f, checksum):
        """ Append ESPLoader checksum to the just-written image """
        align_file_position(f, 16)
        f.write(struct.pack(b'B', checksum))

    def write_common_header(self, f, segments):
        f.write(struct.pack('<BBBBI', ESPLoader.ESP_IMAGE_MAGIC, len(segments),
                            self.flash_mode, self.flash_size_freq, self.entrypoint))

    def is_irom_addr(self, addr):
        """ Returns True if an address starts in the irom region.
        Valid for ESP8266 only.
        """
        return ESP8266ROM.IROM_MAP_START <= addr < ESP8266ROM.IROM_MAP_END

    def get_irom_segment(self):
        irom_segments = [s for s in self.segments if self.is_irom_addr(s.addr)]
        if len(irom_segments) > 0:
            if len(irom_segments) != 1:
                raise FatalError('Found %d segments that could be irom0. Bad ELF file?' % len(irom_segments))
            return irom_segments[0]
        return None

    def get_non_irom_segments(self):
        irom_segment = self.get_irom_segment()
        return [s for s in self.segments if s != irom_segment]

    def merge_adjacent_segments(self):
        if not self.segments:
            return  # nothing to merge

        segments = []
        # The easiest way to merge the sections is the browse them backward.
        for i in range(len(self.segments) - 1, 0, -1):
            # elem is the previous section, the one `next_elem` may need to be
            # merged in
            elem = self.segments[i - 1]
            next_elem = self.segments[i]
            if all((elem.get_memory_type(self) == next_elem.get_memory_type(self),
                    elem.include_in_checksum == next_elem.include_in_checksum,
                    next_elem.addr == elem.addr + len(elem.data))):
                # Merge any segment that ends where the next one starts, without spanning memory types
                #
                # (don't 'pad' any gaps here as they may be excluded from the image due to 'noinit'
                # or other reasons.)
                elem.data += next_elem.data
            else:
                # The section next_elem cannot be merged into the previous one,
                # which means it needs to be part of the final segments.
                # As we are browsing the list backward, the elements need to be
                # inserted at the beginning of the final list.
                segments.insert(0, next_elem)

        # The first segment will always be here as it cannot be merged into any
        # "previous" section.
        segments.insert(0, self.segments[0])

        # note: we could sort segments here as well, but the ordering of segments is sometimes
        # important for other reasons (like embedded ELF SHA-256), so we assume that the linker
        # script will have produced any adjacent sections in linear order in the ELF, anyhow.
        self.segments = segments

    def set_mmu_page_size(self, size):
        """ If supported, this should be overridden by the chip-specific class. Gets called in elf2image. """
        print('WARNING: Changing MMU page size is not supported on {}! Defaulting to 64KB.'.format(self.ROM_LOADER.CHIP_NAME))


class ESP8266ROMFirmwareImage(BaseFirmwareImage):
    """ 'Version 1' firmware image, segments loaded directly by the ROM bootloader. """

    ROM_LOADER = ESP8266ROM

    def __init__(self, load_file=None):
        super(ESP8266ROMFirmwareImage, self).__init__()
        self.flash_mode = 0
        self.flash_size_freq = 0
        self.version = 1

        if load_file is not None:
            segments = self.load_common_header(load_file, ESPLoader.ESP_IMAGE_MAGIC)

            for _ in range(segments):
                self.load_segment(load_file)
            self.checksum = self.read_checksum(load_file)

            self.verify()

    def default_output_name(self, input_file):
        """ Derive a default output name from the ELF name. """
        return input_file + '-'

    def save(self, basename):
        """ Save a set of V1 images for flashing. Parameter is a base filename. """
        # IROM data goes in its own plain binary file
        irom_segment = self.get_irom_segment()
        if irom_segment is not None:
            with open("%s0x%05x.bin" % (basename, irom_segment.addr - ESP8266ROM.IROM_MAP_START), "wb") as f:
                f.write(irom_segment.data)

        # everything but IROM goes at 0x00000 in an image file
        normal_segments = self.get_non_irom_segments()
        with open("%s0x00000.bin" % basename, 'wb') as f:
            self.write_common_header(f, normal_segments)
            checksum = ESPLoader.ESP_CHECKSUM_MAGIC
            for segment in normal_segments:
                checksum = self.save_segment(f, segment, checksum)
            self.append_checksum(f, checksum)


ESP8266ROM.BOOTLOADER_IMAGE = ESP8266ROMFirmwareImage


class ESP8266V2FirmwareImage(BaseFirmwareImage):
    """ 'Version 2' firmware image, segments loaded by software bootloader stub
        (ie Espressif bootloader or rboot)
    """

    ROM_LOADER = ESP8266ROM

    def __init__(self, load_file=None):
        super(ESP8266V2FirmwareImage, self).__init__()
        self.version = 2
        if load_file is not None:
            segments = self.load_common_header(load_file, ESPBOOTLOADER.IMAGE_V2_MAGIC)
            if segments != ESPBOOTLOADER.IMAGE_V2_SEGMENT:
                # segment count is not really segment count here, but we expect to see '4'
                print('Warning: V2 header has unexpected "segment" count %d (usually 4)' % segments)

            # irom segment comes before the second header
            #
            # the file is saved in the image with a zero load address
            # in the header, so we need to calculate a load address
            irom_segment = self.load_segment(load_file, True)
            irom_segment.addr = 0  # for actual mapped addr, add ESP8266ROM.IROM_MAP_START + flashing_addr + 8
            irom_segment.include_in_checksum = False

            first_flash_mode = self.flash_mode
            first_flash_size_freq = self.flash_size_freq
            first_entrypoint = self.entrypoint
            # load the second header

            segments = self.load_common_header(load_file, ESPLoader.ESP_IMAGE_MAGIC)

            if first_flash_mode != self.flash_mode:
                print('WARNING: Flash mode value in first header (0x%02x) disagrees with second (0x%02x). Using second value.'
                      % (first_flash_mode, self.flash_mode))
            if first_flash_size_freq != self.flash_size_freq:
                print('WARNING: Flash size/freq value in first header (0x%02x) disagrees with second (0x%02x). Using second value.'
                      % (first_flash_size_freq, self.flash_size_freq))
            if first_entrypoint != self.entrypoint:
                print('WARNING: Entrypoint address in first header (0x%08x) disagrees with second header (0x%08x). Using second value.'
                      % (first_entrypoint, self.entrypoint))

            # load all the usual segments
            for _ in range(segments):
                self.load_segment(load_file)
            self.checksum = self.read_checksum(load_file)

            self.verify()

    def default_output_name(self, input_file):
        """ Derive a default output name from the ELF name. """
        irom_segment = self.get_irom_segment()
        if irom_segment is not None:
            irom_offs = irom_segment.addr - ESP8266ROM.IROM_MAP_START
        else:
            irom_offs = 0
        return "%s-0x%05x.bin" % (os.path.splitext(input_file)[0],
                                  irom_offs & ~(ESPLoader.FLASH_SECTOR_SIZE - 1))

    def save(self, filename):
        with open(filename, 'wb') as f:
            # Save first header for irom0 segment
            f.write(struct.pack(b'<BBBBI', ESPBOOTLOADER.IMAGE_V2_MAGIC, ESPBOOTLOADER.IMAGE_V2_SEGMENT,
                                self.flash_mode, self.flash_size_freq, self.entrypoint))

            irom_segment = self.get_irom_segment()
            if irom_segment is not None:
                # save irom0 segment, make sure it has load addr 0 in the file
                irom_segment = irom_segment.copy_with_new_addr(0)
                irom_segment.pad_to_alignment(16)  # irom_segment must end on a 16 byte boundary
                self.save_segment(f, irom_segment)

            # second header, matches V1 header and contains loadable segments
            normal_segments = self.get_non_irom_segments()
            self.write_common_header(f, normal_segments)
            checksum = ESPLoader.ESP_CHECKSUM_MAGIC
            for segment in normal_segments:
                checksum = self.save_segment(f, segment, checksum)
            self.append_checksum(f, checksum)

        # calculate a crc32 of entire file and append
        # (algorithm used by recent 8266 SDK bootloaders)
        with open(filename, 'rb') as f:
            crc = esp8266_crc32(f.read())
        with open(filename, 'ab') as f:
            f.write(struct.pack(b'<I', crc))


def esp8266_crc32(data):
    """
    CRC32 algorithm used by 8266 SDK bootloader (and gen_appbin.py).
    """
    crc = binascii.crc32(data, 0) & 0xFFFFFFFF
    if crc & 0x80000000:
        return crc ^ 0xFFFFFFFF
    else:
        return crc + 1


class ESP32FirmwareImage(BaseFirmwareImage):
    """ ESP32 firmware image is very similar to V1 ESP8266 image,
    except with an additional 16 byte reserved header at top of image,
    and because of new flash mapping capabilities the flash-mapped regions
    can be placed in the normal image (just @ 64kB padded offsets).
    """

    ROM_LOADER = ESP32ROM

    # ROM bootloader will read the wp_pin field if SPI flash
    # pins are remapped via flash. IDF actually enables QIO only
    # from software bootloader, so this can be ignored. But needs
    # to be set to this value so ROM bootloader will skip it.
    WP_PIN_DISABLED = 0xEE

    EXTENDED_HEADER_STRUCT_FMT = "<BBBBHBHH" + ("B" * 4) + "B"

    IROM_ALIGN = 65536

    def __init__(self, load_file=None):
        super(ESP32FirmwareImage, self).__init__()
        self.secure_pad = None
        self.flash_mode = 0
        self.flash_size_freq = 0
        self.version = 1
        self.wp_pin = self.WP_PIN_DISABLED
        # SPI pin drive levels
        self.clk_drv = 0
        self.q_drv = 0
        self.d_drv = 0
        self.cs_drv = 0
        self.hd_drv = 0
        self.wp_drv = 0
        self.min_rev = 0
        self.min_rev_full = 0
        self.max_rev_full = 0

        self.append_digest = True

        if load_file is not None:
            start = load_file.tell()

            segments = self.load_common_header(load_file, ESPLoader.ESP_IMAGE_MAGIC)
            self.load_extended_header(load_file)

            for _ in range(segments):
                self.load_segment(load_file)
            self.checksum = self.read_checksum(load_file)

            if self.append_digest:
                end = load_file.tell()
                self.stored_digest = load_file.read(32)
                load_file.seek(start)
                calc_digest = hashlib.sha256()
                calc_digest.update(load_file.read(end - start))
                self.calc_digest = calc_digest.digest()  # TODO: decide what to do here?

            self.verify()

    def is_flash_addr(self, addr):
        return (self.ROM_LOADER.IROM_MAP_START <= addr < self.ROM_LOADER.IROM_MAP_END) \
            or (self.ROM_LOADER.DROM_MAP_START <= addr < self.ROM_LOADER.DROM_MAP_END)

    def default_output_name(self, input_file):
        """ Derive a default output name from the ELF name. """
        return "%s.bin" % (os.path.splitext(input_file)[0])

    def warn_if_unusual_segment(self, offset, size, is_irom_segment):
        pass  # TODO: add warnings for ESP32 segment offset/size combinations that are wrong

    def save(self, filename):
        total_segments = 0
        with io.BytesIO() as f:  # write file to memory first
            self.write_common_header(f, self.segments)

            # first 4 bytes of header are read by ROM bootloader for SPI
            # config, but currently unused
            self.save_extended_header(f)

            checksum = ESPLoader.ESP_CHECKSUM_MAGIC

            # split segments into flash-mapped vs ram-loaded, and take copies so we can mutate them
            flash_segments = [copy.deepcopy(s) for s in sorted(
                self.segments, key=lambda s: s.addr) if self.is_flash_addr(s.addr)]
            ram_segments = [copy.deepcopy(s) for s in sorted(
                self.segments, key=lambda s: s.addr) if not self.is_flash_addr(s.addr)]

            # check for multiple ELF sections that are mapped in the same flash mapping region.
            # this is usually a sign of a broken linker script, but if you have a legitimate
            # use case then let us know
            if len(flash_segments) > 0:
                last_addr = flash_segments[0].addr
                for segment in flash_segments[1:]:
                    if segment.addr // self.IROM_ALIGN == last_addr // self.IROM_ALIGN:
                        raise FatalError(("Segment loaded at 0x%08x lands in same 64KB flash mapping as segment loaded at 0x%08x. "
                                          "Can't generate binary. Suggest changing linker script or ELF to merge sections.") %
                                         (segment.addr, last_addr))
                    last_addr = segment.addr

            def get_alignment_data_needed(segment):
                # Actual alignment (in data bytes) required for a segment header: positioned so that
                # after we write the next 8 byte header, file_offs % IROM_ALIGN == segment.addr % IROM_ALIGN
                #
                # (this is because the segment's vaddr may not be IROM_ALIGNed, more likely is aligned
                # IROM_ALIGN+0x18 to account for the binary file header
                align_past = (segment.addr % self.IROM_ALIGN) - self.SEG_HEADER_LEN
                pad_len = (self.IROM_ALIGN - (f.tell() % self.IROM_ALIGN)) + align_past
                if pad_len == 0 or pad_len == self.IROM_ALIGN:
                    return 0  # already aligned

                # subtract SEG_HEADER_LEN a second time, as the padding block has a header as well
                pad_len -= self.SEG_HEADER_LEN
                if pad_len < 0:
                    pad_len += self.IROM_ALIGN
                return pad_len

            # try to fit each flash segment on a 64kB aligned boundary
            # by padding with parts of the non-flash segments...
            while len(flash_segments) > 0:
                segment = flash_segments[0]
                pad_len = get_alignment_data_needed(segment)
                if pad_len > 0:  # need to pad
                    if len(ram_segments) > 0 and pad_len > self.SEG_HEADER_LEN:
                        pad_segment = ram_segments[0].split_image(pad_len)
                        if len(ram_segments[0].data) == 0:
                            ram_segments.pop(0)
                    else:
                        pad_segment = ImageSegment(0, b'\x00' * pad_len, f.tell())
                    checksum = self.save_segment(f, pad_segment, checksum)
                    total_segments += 1
                else:
                    # write the flash segment
                    assert (f.tell() + 8) % self.IROM_ALIGN == segment.addr % self.IROM_ALIGN
                    checksum = self.save_flash_segment(f, segment, checksum)
                    flash_segments.pop(0)
                    total_segments += 1

            # flash segments all written, so write any remaining RAM segments
            for segment in ram_segments:
                checksum = self.save_segment(f, segment, checksum)
                total_segments += 1

            if self.secure_pad:
                # pad the image so that after signing it will end on a a 64KB boundary.
                # This ensures all mapped flash content will be verified.
                if not self.append_digest:
                    raise FatalError("secure_pad only applies if a SHA-256 digest is also appended to the image")
                align_past = (f.tell() + self.SEG_HEADER_LEN) % self.IROM_ALIGN
                # 16 byte aligned checksum (force the alignment to simplify calculations)
                checksum_space = 16
                if self.secure_pad == '1':
                    # after checksum: SHA-256 digest + (to be added by signing process) version, signature + 12 trailing bytes due to alignment
                    space_after_checksum = 32 + 4 + 64 + 12
                elif self.secure_pad == '2':  # Secure Boot V2
                    # after checksum: SHA-256 digest + signature sector, but we place signature sector after the 64KB boundary
                    space_after_checksum = 32
                pad_len = (self.IROM_ALIGN - align_past - checksum_space - space_after_checksum) % self.IROM_ALIGN
                pad_segment = ImageSegment(0, b'\x00' * pad_len, f.tell())

                checksum = self.save_segment(f, pad_segment, checksum)
                total_segments += 1

            # done writing segments
            self.append_checksum(f, checksum)
            image_length = f.tell()

            if self.secure_pad:
                assert ((image_length + space_after_checksum) % self.IROM_ALIGN) == 0

            # kinda hacky: go back to the initial header and write the new segment count
            # that includes padding segments. This header is not checksummed
            f.seek(1)
            try:
                f.write(chr(total_segments))
            except TypeError:  # Python 3
                f.write(bytes([total_segments]))

            if self.append_digest:
                # calculate the SHA256 of the whole file and append it
                f.seek(0)
                digest = hashlib.sha256()
                digest.update(f.read(image_length))
                f.write(digest.digest())

            if self.pad_to_size:
                image_length = f.tell()
                if image_length % self.pad_to_size != 0:
                    pad_by = self.pad_to_size - (image_length % self.pad_to_size)
                    f.write(b"\xff" * pad_by)

            with open(filename, 'wb') as real_file:
                real_file.write(f.getvalue())

    def load_extended_header(self, load_file):
        def split_byte(n):
            return (n & 0x0F, (n >> 4) & 0x0F)

        fields = list(struct.unpack(self.EXTENDED_HEADER_STRUCT_FMT, load_file.read(16)))

        self.wp_pin = fields[0]

        # SPI pin drive stengths are two per byte
        self.clk_drv, self.q_drv = split_byte(fields[1])
        self.d_drv, self.cs_drv = split_byte(fields[2])
        self.hd_drv, self.wp_drv = split_byte(fields[3])

        chip_id = fields[4]
        if chip_id != self.ROM_LOADER.IMAGE_CHIP_ID:
            print(("Unexpected chip id in image. Expected %d but value was %d. "
                   "Is this image for a different chip model?") % (self.ROM_LOADER.IMAGE_CHIP_ID, chip_id))

        self.min_rev = fields[5]
        self.min_rev_full = fields[6]
        self.max_rev_full = fields[7]

        # reserved fields in the middle should all be zero
        if any(f for f in fields[8:-1] if f != 0):
            print("Warning: some reserved header fields have non-zero values. This image may be from a newer esptool.py?")

        append_digest = fields[-1]  # last byte is append_digest
        if append_digest in [0, 1]:
            self.append_digest = (append_digest == 1)
        else:
            raise RuntimeError("Invalid value for append_digest field (0x%02x). Should be 0 or 1.", append_digest)

    def save_extended_header(self, save_file):
        def join_byte(ln, hn):
            return (ln & 0x0F) + ((hn & 0x0F) << 4)

        append_digest = 1 if self.append_digest else 0

        fields = [self.wp_pin,
                  join_byte(self.clk_drv, self.q_drv),
                  join_byte(self.d_drv, self.cs_drv),
                  join_byte(self.hd_drv, self.wp_drv),
                  self.ROM_LOADER.IMAGE_CHIP_ID,
                  self.min_rev,
                  self.min_rev_full,
                  self.max_rev_full]
        fields += [0] * 4  # padding
        fields += [append_digest]

        packed = struct.pack(self.EXTENDED_HEADER_STRUCT_FMT, *fields)
        save_file.write(packed)


class ESP8266V3FirmwareImage(ESP32FirmwareImage):
    """ ESP8266 V3 firmware image is very similar to ESP32 image
    """

    EXTENDED_HEADER_STRUCT_FMT = "B" * 16

    def is_flash_addr(self, addr):
        return (addr > ESP8266ROM.IROM_MAP_START)

    def save(self, filename):
        total_segments = 0
        with io.BytesIO() as f:  # write file to memory first
            self.write_common_header(f, self.segments)

            checksum = ESPLoader.ESP_CHECKSUM_MAGIC

            # split segments into flash-mapped vs ram-loaded, and take copies so we can mutate them
            flash_segments = [copy.deepcopy(s) for s in sorted(
                self.segments, key=lambda s: s.addr) if self.is_flash_addr(s.addr) and len(s.data)]
            ram_segments = [copy.deepcopy(s) for s in sorted(self.segments, key=lambda s: s.addr)
                            if not self.is_flash_addr(s.addr) and len(s.data)]

            # check for multiple ELF sections that are mapped in the same flash mapping region.
            # this is usually a sign of a broken linker script, but if you have a legitimate
            # use case then let us know
            if len(flash_segments) > 0:
                last_addr = flash_segments[0].addr
                for segment in flash_segments[1:]:
                    if segment.addr // self.IROM_ALIGN == last_addr // self.IROM_ALIGN:
                        raise FatalError(("Segment loaded at 0x%08x lands in same 64KB flash mapping as segment loaded at 0x%08x. "
                                          "Can't generate binary. Suggest changing linker script or ELF to merge sections.") %
                                         (segment.addr, last_addr))
                    last_addr = segment.addr

            # try to fit each flash segment on a 64kB aligned boundary
            # by padding with parts of the non-flash segments...
            while len(flash_segments) > 0:
                segment = flash_segments[0]
                # remove 8 bytes empty data for insert segment header
                if segment.name == '.flash.rodata':
                    segment.data = segment.data[8:]
                # write the flash segment
                checksum = self.save_segment(f, segment, checksum)
                flash_segments.pop(0)
                total_segments += 1

            # flash segments all written, so write any remaining RAM segments
            for segment in ram_segments:
                checksum = self.save_segment(f, segment, checksum)
                total_segments += 1

            # done writing segments
            self.append_checksum(f, checksum)
            image_length = f.tell()

            # kinda hacky: go back to the initial header and write the new segment count
            # that includes padding segments. This header is not checksummed
            f.seek(1)
            try:
                f.write(chr(total_segments))
            except TypeError:  # Python 3
                f.write(bytes([total_segments]))

            if self.append_digest:
                # calculate the SHA256 of the whole file and append it
                f.seek(0)
                digest = hashlib.sha256()
                digest.update(f.read(image_length))
                f.write(digest.digest())

            with open(filename, 'wb') as real_file:
                real_file.write(f.getvalue())

    def load_extended_header(self, load_file):
        def split_byte(n):
            return (n & 0x0F, (n >> 4) & 0x0F)

        fields = list(struct.unpack(self.EXTENDED_HEADER_STRUCT_FMT, load_file.read(16)))

        self.wp_pin = fields[0]

        # SPI pin drive stengths are two per byte
        self.clk_drv, self.q_drv = split_byte(fields[1])
        self.d_drv, self.cs_drv = split_byte(fields[2])
        self.hd_drv, self.wp_drv = split_byte(fields[3])

        if fields[15] in [0, 1]:
            self.append_digest = (fields[15] == 1)
        else:
            raise RuntimeError("Invalid value for append_digest field (0x%02x). Should be 0 or 1.", fields[15])

        # remaining fields in the middle should all be zero
        if any(f for f in fields[4:15] if f != 0):
            print("Warning: some reserved header fields have non-zero values. This image may be from a newer esptool.py?")


ESP32ROM.BOOTLOADER_IMAGE = ESP32FirmwareImage


class ESP32S2FirmwareImage(ESP32FirmwareImage):
    """ ESP32S2 Firmware Image almost exactly the same as ESP32FirmwareImage """
    ROM_LOADER = ESP32S2ROM


ESP32S2ROM.BOOTLOADER_IMAGE = ESP32S2FirmwareImage


class ESP32S3FirmwareImage(ESP32FirmwareImage):
    """ ESP32S3 Firmware Image almost exactly the same as ESP32FirmwareImage """
    ROM_LOADER = ESP32S3ROM


ESP32S3ROM.BOOTLOADER_IMAGE = ESP32S3FirmwareImage


class ESP32C2FirmwareImage(ESP32FirmwareImage):
    """ESP32C2 Firmware Image almost exactly the same as ESP32FirmwareImage"""

    ROM_LOADER = ESP32C2ROM

    def set_mmu_page_size(self, size):
        if size not in [16384, 32768, 65536]:
            raise FatalError(
                "{} bytes is not a valid ESP32-C2 page size, "
                "select from 64KB, 32KB, 16KB.".format(size)
            )
        self.IROM_ALIGN = size


ESP32C2ROM.BOOTLOADER_IMAGE = ESP32C2FirmwareImage


class ESP32C3FirmwareImage(ESP32FirmwareImage):
    """ ESP32C3 Firmware Image almost exactly the same as ESP32FirmwareImage """
    ROM_LOADER = ESP32C3ROM


ESP32C3ROM.BOOTLOADER_IMAGE = ESP32C3FirmwareImage


class ESP32C6FirmwareImage(ESP32FirmwareImage):
    """ESP32C6 Firmware Image almost exactly the same as ESP32FirmwareImage"""

    ROM_LOADER = ESP32C6ROM

    def set_mmu_page_size(self, size):
        if size not in [8192, 16384, 32768, 65536]:
            raise FatalError(
                "{} bytes is not a valid ESP32-C6 page size, "
                "select from 64KB, 32KB, 16KB, 8KB.".format(size)
            )
        self.IROM_ALIGN = size


ESP32C6ROM.BOOTLOADER_IMAGE = ESP32C6FirmwareImage


class ESP32H2FirmwareImage(ESP32C6FirmwareImage):
    """ESP32H2 Firmware Image almost exactly the same as ESP32FirmwareImage"""

    ROM_LOADER = ESP32H2ROM


ESP32H2ROM.BOOTLOADER_IMAGE = ESP32H2FirmwareImage


class ESP32C2FirmwareImage(ESP32FirmwareImage):
    """ ESP32C2 Firmware Image almost exactly the same as ESP32FirmwareImage """
    ROM_LOADER = ESP32C2ROM

    def set_mmu_page_size(self, size):
        if size not in [16384, 32768, 65536]:
            raise FatalError("{} is not a valid page size.".format(size))
        self.IROM_ALIGN = size


ESP32C2ROM.BOOTLOADER_IMAGE = ESP32C2FirmwareImage


class ELFFile(object):
    SEC_TYPE_PROGBITS = 0x01
    SEC_TYPE_STRTAB = 0x03
    SEC_TYPE_INITARRAY = 0x0e
    SEC_TYPE_FINIARRAY = 0x0f

    PROG_SEC_TYPES = (SEC_TYPE_PROGBITS, SEC_TYPE_INITARRAY, SEC_TYPE_FINIARRAY)

    LEN_SEC_HEADER = 0x28

    SEG_TYPE_LOAD = 0x01
    LEN_SEG_HEADER = 0x20

    def __init__(self, name):
        # Load sections from the ELF file
        self.name = name
        with open(self.name, 'rb') as f:
            self._read_elf_file(f)

    def get_section(self, section_name):
        for s in self.sections:
            if s.name == section_name:
                return s
        raise ValueError("No section %s in ELF file" % section_name)

    def _read_elf_file(self, f):
        # read the ELF file header
        LEN_FILE_HEADER = 0x34
        try:
            (ident, _type, machine, _version,
             self.entrypoint, _phoff, shoff, _flags,
             _ehsize, _phentsize, _phnum, shentsize,
             shnum, shstrndx) = struct.unpack("<16sHHLLLLLHHHHHH", f.read(LEN_FILE_HEADER))
        except struct.error as e:
            raise FatalError("Failed to read a valid ELF header from %s: %s" % (self.name, e))

        if byte(ident, 0) != 0x7f or ident[1:4] != b'ELF':
            raise FatalError("%s has invalid ELF magic header" % self.name)
        if machine not in [0x5e, 0xf3]:
            raise FatalError("%s does not appear to be an Xtensa or an RISCV ELF file. e_machine=%04x" %
                             (self.name, machine))
        if shentsize != self.LEN_SEC_HEADER:
            raise FatalError("%s has unexpected section header entry size 0x%x (not 0x%x)" %
                             (self.name, shentsize, self.LEN_SEC_HEADER))
        if shnum == 0:
            raise FatalError("%s has 0 section headers" % (self.name))
        self._read_sections(f, shoff, shnum, shstrndx)
        self._read_segments(f, _phoff, _phnum, shstrndx)

    def _read_sections(self, f, section_header_offs, section_header_count, shstrndx):
        f.seek(section_header_offs)
        len_bytes = section_header_count * self.LEN_SEC_HEADER
        section_header = f.read(len_bytes)
        if len(section_header) == 0:
            raise FatalError("No section header found at offset %04x in ELF file." % section_header_offs)
        if len(section_header) != (len_bytes):
            raise FatalError("Only read 0x%x bytes from section header (expected 0x%x.) Truncated ELF file?" %
                             (len(section_header), len_bytes))

        # walk through the section header and extract all sections
        section_header_offsets = range(0, len(section_header), self.LEN_SEC_HEADER)

        def read_section_header(offs):
            name_offs, sec_type, _flags, lma, sec_offs, size = struct.unpack_from("<LLLLLL", section_header[offs:])
            return (name_offs, sec_type, lma, size, sec_offs)
        all_sections = [read_section_header(offs) for offs in section_header_offsets]
        prog_sections = [s for s in all_sections if s[1] in ELFFile.PROG_SEC_TYPES]

        # search for the string table section
        if not (shstrndx * self.LEN_SEC_HEADER) in section_header_offsets:
            raise FatalError("ELF file has no STRTAB section at shstrndx %d" % shstrndx)
        _, sec_type, _, sec_size, sec_offs = read_section_header(shstrndx * self.LEN_SEC_HEADER)
        if sec_type != ELFFile.SEC_TYPE_STRTAB:
            print('WARNING: ELF file has incorrect STRTAB section type 0x%02x' % sec_type)
        f.seek(sec_offs)
        string_table = f.read(sec_size)

        # build the real list of ELFSections by reading the actual section names from the
        # string table section, and actual data for each section from the ELF file itself
        def lookup_string(offs):
            raw = string_table[offs:]
            return raw[:raw.index(b'\x00')]

        def read_data(offs, size):
            f.seek(offs)
            return f.read(size)

        prog_sections = [ELFSection(lookup_string(n_offs), lma, read_data(offs, size)) for (n_offs, _type, lma, size, offs) in prog_sections
                         if lma != 0 and size > 0]
        self.sections = prog_sections

    def _read_segments(self, f, segment_header_offs, segment_header_count, shstrndx):
        f.seek(segment_header_offs)
        len_bytes = segment_header_count * self.LEN_SEG_HEADER
        segment_header = f.read(len_bytes)
        if len(segment_header) == 0:
            raise FatalError("No segment header found at offset %04x in ELF file." % segment_header_offs)
        if len(segment_header) != (len_bytes):
            raise FatalError("Only read 0x%x bytes from segment header (expected 0x%x.) Truncated ELF file?" %
                             (len(segment_header), len_bytes))

        # walk through the segment header and extract all segments
        segment_header_offsets = range(0, len(segment_header), self.LEN_SEG_HEADER)

        def read_segment_header(offs):
            seg_type, seg_offs, _vaddr, lma, size, _memsize, _flags, _align = struct.unpack_from(
                "<LLLLLLLL", segment_header[offs:])
            return (seg_type, lma, size, seg_offs)
        all_segments = [read_segment_header(offs) for offs in segment_header_offsets]
        prog_segments = [s for s in all_segments if s[0] == ELFFile.SEG_TYPE_LOAD]

        def read_data(offs, size):
            f.seek(offs)
            return f.read(size)

        prog_segments = [ELFSection(b'PHDR', lma, read_data(offs, size)) for (_type, lma, size, offs) in prog_segments
                         if lma != 0 and size > 0]
        self.segments = prog_segments

    def sha256(self):
        # return SHA256 hash of the input ELF file
        sha256 = hashlib.sha256()
        with open(self.name, 'rb') as f:
            sha256.update(f.read())
        return sha256.digest()


def slip_reader(port, trace_function):
    """Generator to read SLIP packets from a serial port.
    Yields one full SLIP packet at a time, raises exception on timeout or invalid data.

    Designed to avoid too many calls to serial.read(1), which can bog
    down on slow systems.
    """
    partial_packet = None
    in_escape = False
    successful_slip = False
    while True:
        waiting = port.inWaiting()
        read_bytes = port.read(1 if waiting == 0 else waiting)
        if read_bytes == b'':
            if partial_packet is None:  # fail due to no data
                msg = "Serial data stream stopped: Possible serial noise or corruption." if successful_slip else "No serial data received."
            else:  # fail during packet transfer
                msg = "Packet content transfer stopped (received {} bytes)".format(len(partial_packet))
            trace_function(msg)
            raise FatalError(msg)
        trace_function("Read %d bytes: %s", len(read_bytes), HexFormatter(read_bytes))
        for b in read_bytes:
            if type(b) is int:
                b = bytes([b])  # python 2/3 compat

            if partial_packet is None:  # waiting for packet header
                if b == b'\xc0':
                    partial_packet = b""
                else:
                    trace_function("Read invalid data: %s", HexFormatter(read_bytes))
                    trace_function("Remaining data in serial buffer: %s", HexFormatter(port.read(port.inWaiting())))
                    raise FatalError('Invalid head of packet (0x%s): Possible serial noise or corruption.' % hexify(b))
            elif in_escape:  # part-way through escape sequence
                in_escape = False
                if b == b'\xdc':
                    partial_packet += b'\xc0'
                elif b == b'\xdd':
                    partial_packet += b'\xdb'
                else:
                    trace_function("Read invalid data: %s", HexFormatter(read_bytes))
                    trace_function("Remaining data in serial buffer: %s", HexFormatter(port.read(port.inWaiting())))
                    raise FatalError('Invalid SLIP escape (0xdb, 0x%s)' % (hexify(b)))
            elif b == b'\xdb':  # start of escape sequence
                in_escape = True
            elif b == b'\xc0':  # end of packet
                trace_function("Received full packet: %s", HexFormatter(partial_packet))
                yield partial_packet
                partial_packet = None
                successful_slip = True
            else:  # normal byte in packet
                partial_packet += b


def arg_auto_int(x):
    return int(x, 0)


def format_chip_name(c):
    """ Normalize chip name from user input """
    c = c.lower().replace('-', '')
    if c == 'esp8684':  # TODO: Delete alias, ESPTOOL-389
        print('WARNING: Chip name ESP8684 is deprecated in favor of ESP32-C2 and will be removed in a future release. Using ESP32-C2 instead.')
        return 'esp32c2'
    return c


def div_roundup(a, b):
    """ Return a/b rounded up to nearest integer,
    equivalent result to int(math.ceil(float(int(a)) / float(int(b))), only
    without possible floating point accuracy errors.
    """
    return (int(a) + int(b) - 1) // int(b)


def align_file_position(f, size):
    """ Align the position in the file to the next block of specified size """
    align = (size - 1) - (f.tell() % size)
    f.seek(align, 1)


def flash_size_bytes(size):
    """ Given a flash size of the type passed in args.flash_size
    (ie 512KB or 1MB) then return the size in bytes.
    """
    if "MB" in size:
        return int(size[:size.index("MB")]) * 1024 * 1024
    elif "KB" in size:
        return int(size[:size.index("KB")]) * 1024
    else:
        raise FatalError("Unknown size %s" % size)


def hexify(s, uppercase=True):
    format_str = '%02X' if uppercase else '%02x'
    if not PYTHON2:
        return ''.join(format_str % c for c in s)
    else:
        return ''.join(format_str % ord(c) for c in s)


class HexFormatter(object):
    """
    Wrapper class which takes binary data in its constructor
    and returns a hex string as it's __str__ method.

    This is intended for "lazy formatting" of trace() output
    in hex format. Avoids overhead (significant on slow computers)
    of generating long hex strings even if tracing is disabled.

    Note that this doesn't save any overhead if passed as an
    argument to "%", only when passed to trace()

    If auto_split is set (default), any long line (> 16 bytes) will be
    printed as separately indented lines, with ASCII decoding at the end
    of each line.
    """

    def __init__(self, binary_string, auto_split=True):
        self._s = binary_string
        self._auto_split = auto_split

    def __str__(self):
        if self._auto_split and len(self._s) > 16:
            result = ""
            s = self._s
            while len(s) > 0:
                line = s[:16]
                ascii_line = "".join(c if (c == ' ' or (c in string.printable and c not in string.whitespace))
                                     else '.' for c in line.decode('ascii', 'replace'))
                s = s[16:]
                result += "\n    %-16s %-16s | %s" % (hexify(line[:8], False), hexify(line[8:], False), ascii_line)
            return result
        else:
            return hexify(self._s, False)


def pad_to(data, alignment, pad_character=b'\xFF'):
    """ Pad to the next alignment boundary """
    pad_mod = len(data) % alignment
    if pad_mod != 0:
        data += pad_character * (alignment - pad_mod)
    return data


class FatalError(RuntimeError):
    """
    Wrapper class for runtime errors that aren't caused by internal bugs, but by
    ESP ROM responses or input content.
    """

    def __init__(self, message):
        RuntimeError.__init__(self, message)

    @staticmethod
    def WithResult(message, result):
        """
        Return a fatal error object that appends the hex values of
        'result' and its meaning as a string formatted argument.
        """

        err_defs = {
            0x101: 'Out of memory',
            0x102: 'Invalid argument',
            0x103: 'Invalid state',
            0x104: 'Invalid size',
            0x105: 'Requested resource not found',
            0x106: 'Operation or feature not supported',
            0x107: 'Operation timed out',
            0x108: 'Received response was invalid',
            0x109: 'CRC or checksum was invalid',
            0x10A: 'Version was invalid',
            0x10B: 'MAC address was invalid',
            # Flasher stub error codes
            0xC000: 'Bad data length',
            0xC100: 'Bad data checksum',
            0xC200: 'Bad blocksize',
            0xC300: 'Invalid command',
            0xC400: 'Failed SPI operation',
            0xC500: 'Failed SPI unlock',
            0xC600: 'Not in flash mode',
            0xC700: 'Inflate error',
            0xC800: 'Not enough data',
            0xC900: 'Too much data',
            0xFF00: 'Command not implemented',
        }

        err_code = struct.unpack(">H", result[:2])
        message += " (result was {}: {})".format(hexify(result), err_defs.get(err_code[0], 'Unknown result'))
        return FatalError(message)


class NotImplementedInROMError(FatalError):
    """
    Wrapper class for the error thrown when a particular ESP bootloader function
    is not implemented in the ROM bootloader.
    """

    def __init__(self, bootloader, func):
        FatalError.__init__(self, "%s ROM does not support function %s." % (bootloader.CHIP_NAME, func.__name__))


class NotSupportedError(FatalError):
    def __init__(self, esp, function_name):
        FatalError.__init__(self, "Function %s is not supported for %s." % (function_name, esp.CHIP_NAME))

# "Operation" commands, executable at command line. One function each
#
# Each function takes either two args (<ESPLoader instance>, <args>) or a single <args>
# argument.


class UnsupportedCommandError(RuntimeError):
    """
    Wrapper class for when ROM loader returns an invalid command response.

    Usually this indicates the loader is running in Secure Download Mode.
    """

    def __init__(self, esp, op):
        if esp.secure_download_mode:
            msg = "This command (0x%x) is not supported in Secure Download Mode" % op
        else:
            msg = "Invalid (unsupported) command 0x%x" % op
        RuntimeError.__init__(self, msg)


def load_ram(esp, args):
    image = LoadFirmwareImage(esp.CHIP_NAME, args.filename)

    print('RAM boot...')
    for seg in image.segments:
        size = len(seg.data)
        print('Downloading %d bytes at %08x...' % (size, seg.addr), end=' ')
        sys.stdout.flush()
        esp.mem_begin(size, div_roundup(size, esp.ESP_RAM_BLOCK), esp.ESP_RAM_BLOCK, seg.addr)

        seq = 0
        while len(seg.data) > 0:
            esp.mem_block(seg.data[0:esp.ESP_RAM_BLOCK], seq)
            seg.data = seg.data[esp.ESP_RAM_BLOCK:]
            seq += 1
        print('done!')

    print('All segments done, executing at %08x' % image.entrypoint)
    esp.mem_finish(image.entrypoint)


def read_mem(esp, args):
    print('0x%08x = 0x%08x' % (args.address, esp.read_reg(args.address)))


def write_mem(esp, args):
    esp.write_reg(args.address, args.value, args.mask, 0)
    print('Wrote %08x, mask %08x to %08x' % (args.value, args.mask, args.address))


def dump_mem(esp, args):
    with open(args.filename, 'wb') as f:
        for i in range(args.size // 4):
            d = esp.read_reg(args.address + (i * 4))
            f.write(struct.pack(b'<I', d))
            if f.tell() % 1024 == 0:
                print_overwrite('%d bytes read... (%d %%)' % (f.tell(),
                                                              f.tell() * 100 // args.size))
            sys.stdout.flush()
        print_overwrite("Read %d bytes" % f.tell(), last_line=True)
    print('Done!')


def detect_flash_size(esp, args):
    if args.flash_size == 'detect':
        if esp.secure_download_mode:
            raise FatalError(
                "Detecting flash size is not supported in secure download mode. Need to manually specify flash size.")
        flash_id = esp.flash_id()
        size_id = flash_id >> 16
        args.flash_size = DETECTED_FLASH_SIZES.get(size_id)
        if args.flash_size is None:
            print('Warning: Could not auto-detect Flash size (FlashID=0x%x, SizeID=0x%x), defaulting to 4MB' % (flash_id, size_id))
            args.flash_size = '4MB'
        else:
            print('Auto-detected Flash size:', args.flash_size)


def _update_image_flash_params(esp, address, args, image):
    """ Modify the flash mode & size bytes if this looks like an executable bootloader image  """
    if len(image) < 8:
        return image  # not long enough to be a bootloader image

    # unpack the (potential) image header
    magic, _, flash_mode, flash_size_freq = struct.unpack("BBBB", image[:4])
    if address != esp.BOOTLOADER_FLASH_OFFSET:
        return image  # not flashing bootloader offset, so don't modify this

    if (args.flash_mode, args.flash_freq, args.flash_size) == ('keep',) * 3:
        return image  # all settings are 'keep', not modifying anything

    # easy check if this is an image: does it start with a magic byte?
    if magic != esp.ESP_IMAGE_MAGIC:
        print("Warning: Image file at 0x%x doesn't look like an image file, so not changing any flash settings." % address)
        return image

    # make sure this really is an image, and not just data that
    # starts with esp.ESP_IMAGE_MAGIC (mostly a problem for encrypted
    # images that happen to start with a magic byte
    try:
        test_image = esp.BOOTLOADER_IMAGE(io.BytesIO(image))
        test_image.verify()
    except Exception:
        print("Warning: Image file at 0x%x is not a valid %s image, so not changing any flash settings." %
              (address, esp.CHIP_NAME))
        return image

    if args.flash_mode != 'keep':
        flash_mode = {'qio': 0, 'qout': 1, 'dio': 2, 'dout': 3}[args.flash_mode]

    flash_freq = flash_size_freq & 0x0F
    if args.flash_freq != 'keep':
        flash_freq = esp.parse_flash_freq_arg(args.flash_freq)

    flash_size = flash_size_freq & 0xF0
    if args.flash_size != 'keep':
        flash_size = esp.parse_flash_size_arg(args.flash_size)

    flash_params = struct.pack(b'BB', flash_mode, flash_size + flash_freq)
    if flash_params != image[2:4]:
        print('Flash params set to 0x%04x' % struct.unpack(">H", flash_params))
        image = image[0:2] + flash_params + image[4:]
    return image


def write_flash(esp, args):
    # set args.compress based on default behaviour:
    # -> if either --compress or --no-compress is set, honour that
    # -> otherwise, set --compress unless --no-stub is set
    if args.compress is None and not args.no_compress:
        args.compress = not args.no_stub

    # In case we have encrypted files to write, we first do few sanity checks before actual flash
    if args.encrypt or args.encrypt_files is not None:
        do_write = True

        if not esp.secure_download_mode:
            if esp.get_encrypted_download_disabled():
                raise FatalError("This chip has encrypt functionality in UART download mode disabled. "
                                 "This is the Flash Encryption configuration for Production mode instead of Development mode.")

            crypt_cfg_efuse = esp.get_flash_crypt_config()

            if crypt_cfg_efuse is not None and crypt_cfg_efuse != 0xF:
                print('Unexpected FLASH_CRYPT_CONFIG value: 0x%x' % (crypt_cfg_efuse))
                do_write = False

            enc_key_valid = esp.is_flash_encryption_key_valid()

            if not enc_key_valid:
                print('Flash encryption key is not programmed')
                do_write = False

        # Determine which files list contain the ones to encrypt
        files_to_encrypt = args.addr_filename if args.encrypt else args.encrypt_files

        for address, argfile in files_to_encrypt:
            if address % esp.FLASH_ENCRYPTED_WRITE_ALIGN:
                print("File %s address 0x%x is not %d byte aligned, can't flash encrypted" %
                      (argfile.name, address, esp.FLASH_ENCRYPTED_WRITE_ALIGN))
                do_write = False

        if not do_write and not args.ignore_flash_encryption_efuse_setting:
            raise FatalError(
                "Can't perform encrypted flash write, consult Flash Encryption documentation for more information")

    # verify file sizes fit in flash
    if args.flash_size != 'keep':  # TODO: check this even with 'keep'
        flash_end = flash_size_bytes(args.flash_size)
        for address, argfile in args.addr_filename:
            argfile.seek(0, os.SEEK_END)
            if address + argfile.tell() > flash_end:
                raise FatalError(("File %s (length %d) at offset %d will not fit in %d bytes of flash. "
                                  "Use --flash_size argument, or change flashing address.")
                                 % (argfile.name, argfile.tell(), address, flash_end))
            argfile.seek(0)

    if args.erase_all:
        erase_flash(esp, args)
    else:
        for address, argfile in args.addr_filename:
            argfile.seek(0, os.SEEK_END)
            write_end = address + argfile.tell()
            argfile.seek(0)
            bytes_over = address % esp.FLASH_SECTOR_SIZE
            if bytes_over != 0:
                print("WARNING: Flash address {:#010x} is not aligned to a {:#x} byte flash sector. "
                      "{:#x} bytes before this address will be erased."
                      .format(address, esp.FLASH_SECTOR_SIZE, bytes_over))
            # Print the address range of to-be-erased flash memory region
            print("Flash will be erased from {:#010x} to {:#010x}..."
                  .format(address - bytes_over, div_roundup(write_end, esp.FLASH_SECTOR_SIZE) * esp.FLASH_SECTOR_SIZE - 1))

    """ Create a list describing all the files we have to flash. Each entry holds an "encrypt" flag
    marking whether the file needs encryption or not. This list needs to be sorted.

    First, append to each entry of our addr_filename list the flag args.encrypt
    For example, if addr_filename is [(0x1000, "partition.bin"), (0x8000, "bootloader")],
    all_files will be [(0x1000, "partition.bin", args.encrypt), (0x8000, "bootloader", args.encrypt)],
    where, of course, args.encrypt is either True or False
    """
    all_files = [(offs, filename, args.encrypt) for (offs, filename) in args.addr_filename]

    """Now do the same with encrypt_files list, if defined.
    In this case, the flag is True
    """
    if args.encrypt_files is not None:
        encrypted_files_flag = [(offs, filename, True) for (offs, filename) in args.encrypt_files]

        # Concatenate both lists and sort them.
        # As both list are already sorted, we could simply do a merge instead,
        # but for the sake of simplicity and because the lists are very small,
        # let's use sorted.
        all_files = sorted(all_files + encrypted_files_flag, key=lambda x: x[0])

    for address, argfile, encrypted in all_files:
        compress = args.compress

        # Check whether we can compress the current file before flashing
        if compress and encrypted:
            print('\nWARNING: - compress and encrypt options are mutually exclusive ')
            print('Will flash %s uncompressed' % argfile.name)
            compress = False

        if args.no_stub:
            print('Erasing flash...')
        image = pad_to(argfile.read(), esp.FLASH_ENCRYPTED_WRITE_ALIGN if encrypted else 4)
        if len(image) == 0:
            print('WARNING: File %s is empty' % argfile.name)
            continue
        image = _update_image_flash_params(esp, address, args, image)
        calcmd5 = hashlib.md5(image).hexdigest()
        uncsize = len(image)
        if compress:
            uncimage = image
            image = zlib.compress(uncimage, 9)
            # Decompress the compressed binary a block at a time, to dynamically calculate the
            # timeout based on the real write size
            decompress = zlib.decompressobj()
            blocks = esp.flash_defl_begin(uncsize, len(image), address)
        else:
            blocks = esp.flash_begin(uncsize, address, begin_rom_encrypted=encrypted)
        argfile.seek(0)  # in case we need it again
        seq = 0
        bytes_sent = 0  # bytes sent on wire
        bytes_written = 0  # bytes written to flash
        t = time.time()

        timeout = DEFAULT_TIMEOUT

        while len(image) > 0:
            print_overwrite('Writing at 0x%08x... (%d %%)' % (address + bytes_written, 100 * (seq + 1) // blocks))
            sys.stdout.flush()
            block = image[0:esp.FLASH_WRITE_SIZE]
            if compress:
                # feeding each compressed block into the decompressor lets us see block-by-block how much will be written
                block_uncompressed = len(decompress.decompress(block))
                bytes_written += block_uncompressed
                block_timeout = max(DEFAULT_TIMEOUT, timeout_per_mb(ERASE_WRITE_TIMEOUT_PER_MB, block_uncompressed))
                if not esp.IS_STUB:
                    timeout = block_timeout  # ROM code writes block to flash before ACKing
                esp.flash_defl_block(block, seq, timeout=timeout)
                if esp.IS_STUB:
                    timeout = block_timeout  # Stub ACKs when block is received, then writes to flash while receiving the block after it
            else:
                # Pad the last block
                block = block + b'\xff' * (esp.FLASH_WRITE_SIZE - len(block))
                if encrypted:
                    esp.flash_encrypt_block(block, seq)
                else:
                    esp.flash_block(block, seq)
                bytes_written += len(block)
            bytes_sent += len(block)
            image = image[esp.FLASH_WRITE_SIZE:]
            seq += 1

        if esp.IS_STUB:
            # Stub only writes each block to flash after 'ack'ing the receive, so do a final dummy operation which will
            # not be 'ack'ed until the last block has actually been written out to flash
            esp.read_reg(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR, timeout=timeout)

        t = time.time() - t
        speed_msg = ""
        if compress:
            if t > 0.0:
                speed_msg = " (effective %.1f kbit/s)" % (uncsize / t * 8 / 1000)
            print_overwrite('Wrote %d bytes (%d compressed) at 0x%08x in %.1f seconds%s...' % (uncsize,
                                                                                               bytes_sent,
                                                                                               address, t, speed_msg), last_line=True)
        else:
            if t > 0.0:
                speed_msg = " (%.1f kbit/s)" % (bytes_written / t * 8 / 1000)
            print_overwrite('Wrote %d bytes at 0x%08x in %.1f seconds%s...' %
                            (bytes_written, address, t, speed_msg), last_line=True)

        if not encrypted and not esp.secure_download_mode:
            try:
                res = esp.flash_md5sum(address, uncsize)
                if res != calcmd5:
                    print('File  md5: %s' % calcmd5)
                    print('Flash md5: %s' % res)
                    print('MD5 of 0xFF is %s' % (hashlib.md5(b'\xFF' * uncsize).hexdigest()))
                    raise FatalError("MD5 of file does not match data in flash!")
                else:
                    print('Hash of data verified.')
            except NotImplementedInROMError:
                pass

    print('\nLeaving...')

    if esp.IS_STUB:
        # skip sending flash_finish to ROM loader here,
        # as it causes the loader to exit and run user code
        esp.flash_begin(0, 0)

        # Get the "encrypted" flag for the last file flashed
        # Note: all_files list contains triplets like:
        # (address: Integer, filename: String, encrypted: Boolean)
        last_file_encrypted = all_files[-1][2]

        # Check whether the last file flashed was compressed or not
        if args.compress and not last_file_encrypted:
            esp.flash_defl_finish(False)
        else:
            esp.flash_finish(False)

    if args.verify:
        print('Verifying just-written flash...')
        print('(This option is deprecated, flash contents are now always read back after flashing.)')
        # If some encrypted files have been flashed print a warning saying that we won't check them
        if args.encrypt or args.encrypt_files is not None:
            print('WARNING: - cannot verify encrypted files, they will be ignored')
        # Call verify_flash function only if there at least one non-encrypted file flashed
        if not args.encrypt:
            verify_flash(esp, args)


def image_info(args):
    if args.chip == "auto":
        print("WARNING: --chip not specified, defaulting to ESP8266.")
    image = LoadFirmwareImage(args.chip, args.filename)
    print('Image version: %d' % image.version)
    if args.chip != 'auto' and args.chip != 'esp8266':
        print(
            "Minimal chip revision:",
            "v{}.{},".format(image.min_rev_full // 100, image.min_rev_full % 100),
            "(legacy min_rev = {})".format(image.min_rev)
        )
        print(
            "Maximal chip revision:",
            "v{}.{}".format(image.max_rev_full // 100, image.max_rev_full % 100),
        )
    print('Entry point: %08x' % image.entrypoint if image.entrypoint != 0 else 'Entry point not set')
    print('%d segments' % len(image.segments))
    print()
    idx = 0
    for seg in image.segments:
        idx += 1
        segs = seg.get_memory_type(image)
        seg_name = ",".join(segs)
        print('Segment %d: %r [%s]' % (idx, seg, seg_name))
    calc_checksum = image.calculate_checksum()
    print('Checksum: %02x (%s)' % (image.checksum,
                                   'valid' if image.checksum == calc_checksum else 'invalid - calculated %02x' % calc_checksum))
    try:
        digest_msg = 'Not appended'
        if image.append_digest:
            is_valid = image.stored_digest == image.calc_digest
            digest_msg = "%s (%s)" % (hexify(image.calc_digest).lower(),
                                      "valid" if is_valid else "invalid")
            print('Validation Hash: %s' % digest_msg)
    except AttributeError:
        pass  # ESP8266 image has no append_digest field


def make_image(args):
    image = ESP8266ROMFirmwareImage()
    if len(args.segfile) == 0:
        raise FatalError('No segments specified')
    if len(args.segfile) != len(args.segaddr):
        raise FatalError('Number of specified files does not match number of specified addresses')
    for (seg, addr) in zip(args.segfile, args.segaddr):
        with open(seg, 'rb') as f:
            data = f.read()
            image.segments.append(ImageSegment(addr, data))
    image.entrypoint = args.entrypoint
    image.save(args.output)


def elf2image(args):
    e = ELFFile(args.input)
    if args.chip == 'auto':  # Default to ESP8266 for backwards compatibility
        args.chip = 'esp8266'

    print("Creating {} image...".format(args.chip))

    if args.chip == 'esp32':
        image = ESP32FirmwareImage()
        if args.secure_pad:
            image.secure_pad = '1'
        elif args.secure_pad_v2:
            image.secure_pad = '2'
    elif args.chip == 'esp32s2':
        image = ESP32S2FirmwareImage()
        if args.secure_pad_v2:
            image.secure_pad = '2'
    elif args.chip == 'esp32s3':
        image = ESP32S3FirmwareImage()
        if args.secure_pad_v2:
            image.secure_pad = '2'
    elif args.chip == 'esp32c3':
        image = ESP32C3FirmwareImage()
        if args.secure_pad_v2:
            image.secure_pad = '2'
    elif args.chip == 'esp32c6':
        image = ESP32C6FirmwareImage()
        if args.secure_pad_v2:
            image.secure_pad = '2'
    elif args.chip == 'esp32h2':
        image = ESP32H2FirmwareImage()
        if args.secure_pad_v2:
            image.secure_pad = '2'
    elif args.chip == 'esp32c2':
        image = ESP32C2FirmwareImage()
        if args.secure_pad_v2:
            image.secure_pad = '2'
    elif args.version == '1':  # ESP8266
        image = ESP8266ROMFirmwareImage()
    elif args.version == '2':
        image = ESP8266V2FirmwareImage()
    else:
        image = ESP8266V3FirmwareImage()
    image.entrypoint = e.entrypoint
    image.flash_mode = {'qio': 0, 'qout': 1, 'dio': 2, 'dout': 3}[args.flash_mode]

    if args.chip != 'esp8266':
        image.min_rev = args.min_rev
        image.min_rev_full = args.min_rev_full
        image.max_rev_full = args.max_rev_full

    if args.flash_mmu_page_size:
        image.set_mmu_page_size(flash_size_bytes(args.flash_mmu_page_size))

    # ELFSection is a subclass of ImageSegment, so can use interchangeably
    image.segments = e.segments if args.use_segments else e.sections

    if args.pad_to_size:
        image.pad_to_size = flash_size_bytes(args.pad_to_size)

    image.flash_size_freq = image.ROM_LOADER.parse_flash_size_arg(args.flash_size)
    image.flash_size_freq += image.ROM_LOADER.parse_flash_freq_arg(args.flash_freq)

    if args.elf_sha256_offset:
        image.elf_sha256 = e.sha256()
        image.elf_sha256_offset = args.elf_sha256_offset

    before = len(image.segments)
    image.merge_adjacent_segments()
    if len(image.segments) != before:
        delta = before - len(image.segments)
        print("Merged %d ELF section%s" % (delta, "s" if delta > 1 else ""))

    image.verify()

    if args.output is None:
        args.output = image.default_output_name(args.input)
    image.save(args.output)

    print("Successfully created {} image.".format(args.chip))


def read_mac(esp, args):
    mac = esp.read_mac()

    def print_mac(label, mac):
        print('%s: %s' % (label, ':'.join(map(lambda x: '%02x' % x, mac))))
    print_mac("MAC", mac)


def chip_id(esp, args):
    try:
        chipid = esp.chip_id()
        print('Chip ID: 0x%08x' % chipid)
    except NotSupportedError:
        print('Warning: %s has no Chip ID. Reading MAC instead.' % esp.CHIP_NAME)
        read_mac(esp, args)


def erase_flash(esp, args):
    print('Erasing flash (this may take a while)...')
    t = time.time()
    esp.erase_flash()
    print('Chip erase completed successfully in %.1fs' % (time.time() - t))


def erase_region(esp, args):
    print('Erasing region (may be slow depending on size)...')
    t = time.time()
    esp.erase_region(args.address, args.size)
    print('Erase completed successfully in %.1f seconds.' % (time.time() - t))


def run(esp, args):
    esp.run()


def flash_id(esp, args):
    flash_id = esp.flash_id()
    print('Manufacturer: %02x' % (flash_id & 0xff))
    flid_lowbyte = (flash_id >> 16) & 0xFF
    print('Device: %02x%02x' % ((flash_id >> 8) & 0xff, flid_lowbyte))
    print('Detected flash size: %s' % (DETECTED_FLASH_SIZES.get(flid_lowbyte, "Unknown")))


def read_flash(esp, args):
    if args.no_progress:
        flash_progress = None
    else:
        def flash_progress(progress, length):
            msg = '%d (%d %%)' % (progress, progress * 100.0 / length)
            padding = '\b' * len(msg)
            if progress == length:
                padding = '\n'
            sys.stdout.write(msg + padding)
            sys.stdout.flush()
    t = time.time()
    data = esp.read_flash(args.address, args.size, flash_progress)
    t = time.time() - t
    print_overwrite('Read %d bytes at 0x%x in %.1f seconds (%.1f kbit/s)...'
                    % (len(data), args.address, t, len(data) / t * 8 / 1000), last_line=True)
    with open(args.filename, 'wb') as f:
        f.write(data)


def verify_flash(esp, args):
    differences = False

    for address, argfile in args.addr_filename:
        image = pad_to(argfile.read(), 4)
        argfile.seek(0)  # rewind in case we need it again

        image = _update_image_flash_params(esp, address, args, image)

        image_size = len(image)
        print('Verifying 0x%x (%d) bytes @ 0x%08x in flash against %s...' %
              (image_size, image_size, address, argfile.name))
        # Try digest first, only read if there are differences.
        digest = esp.flash_md5sum(address, image_size)
        expected_digest = hashlib.md5(image).hexdigest()
        if digest == expected_digest:
            print('-- verify OK (digest matched)')
            continue
        else:
            differences = True
            if getattr(args, 'diff', 'no') != 'yes':
                print('-- verify FAILED (digest mismatch)')
                continue

        flash = esp.read_flash(address, image_size)
        assert flash != image
        diff = [i for i in range(image_size) if flash[i] != image[i]]
        print('-- verify FAILED: %d differences, first @ 0x%08x' % (len(diff), address + diff[0]))
        for d in diff:
            flash_byte = flash[d]
            image_byte = image[d]
            if PYTHON2:
                flash_byte = ord(flash_byte)
                image_byte = ord(image_byte)
            print('   %08x %02x %02x' % (address + d, flash_byte, image_byte))
    if differences:
        raise FatalError("Verify failed.")


def read_flash_status(esp, args):
    print('Status value: 0x%04x' % esp.read_status(args.bytes))


def write_flash_status(esp, args):
    fmt = "0x%%0%dx" % (args.bytes * 2)
    args.value = args.value & ((1 << (args.bytes * 8)) - 1)
    print(('Initial flash status: ' + fmt) % esp.read_status(args.bytes))
    print(('Setting flash status: ' + fmt) % args.value)
    esp.write_status(args.value, args.bytes, args.non_volatile)
    print(('After flash status:   ' + fmt) % esp.read_status(args.bytes))


def get_security_info(esp, args):
    si = esp.get_security_info()
    # TODO: better display and tests
    print('Flags: {:#010x} ({})'.format(si["flags"], bin(si["flags"])))
    print('Flash_Crypt_Cnt: {:#x}'.format(si["flash_crypt_cnt"]))
    print('Key_Purposes: {}'.format(si["key_purposes"]))
    if si["chip_id"] is not None and si["api_version"] is not None:
        print('Chip_ID: {}'.format(si["chip_id"]))
        print('Api_Version: {}'.format(si["api_version"]))


def merge_bin(args):
    try:
        chip_class = _chip_to_rom_loader(args.chip)
    except KeyError:
        msg = "Please specify the chip argument" if args.chip == "auto" else "Invalid chip choice: '{}'".format(
            args.chip)
        msg = msg + " (choose from {})".format(', '.join(SUPPORTED_CHIPS))
        raise FatalError(msg)

    # sort the files by offset. The AddrFilenamePairAction has already checked for overlap
    input_files = sorted(args.addr_filename, key=lambda x: x[0])
    if not input_files:
        raise FatalError("No input files specified")
    first_addr = input_files[0][0]
    if first_addr < args.target_offset:
        raise FatalError("Output file target offset is 0x%x. Input file offset 0x%x is before this." %
                         (args.target_offset, first_addr))

    if args.format != 'raw':
        raise FatalError("This version of esptool only supports the 'raw' output format")

    with open(args.output, 'wb') as of:
        def pad_to(flash_offs):
            # account for output file offset if there is any
            of.write(b'\xFF' * (flash_offs - args.target_offset - of.tell()))
        for addr, argfile in input_files:
            pad_to(addr)
            image = argfile.read()
            image = _update_image_flash_params(chip_class, addr, args, image)
            of.write(image)
        if args.fill_flash_size:
            pad_to(flash_size_bytes(args.fill_flash_size))
        print("Wrote 0x%x bytes to file %s, ready to flash to offset 0x%x" %
              (of.tell(), args.output, args.target_offset))


def version(args):
    print(__version__)

#
# End of operations functions
#


def main(argv=None, esp=None):
    """
    Main function for esptool

    argv - Optional override for default arguments parsing (that uses sys.argv), can be a list of custom arguments
    as strings. Arguments and their values need to be added as individual items to the list e.g. "-b 115200" thus
    becomes ['-b', '115200'].

    esp - Optional override of the connected device previously returned by get_default_connected_device()
    """

    external_esp = esp is not None

    parser = argparse.ArgumentParser(
        description='esptool.py v%s - Espressif chips ROM Bootloader Utility' % __version__, prog='esptool')

    parser.add_argument('--chip', '-c',
                        help='Target chip type',
                        type=format_chip_name,  # support ESP32-S2, etc.
                        choices=['auto'] + SUPPORTED_CHIPS,
                        default=os.environ.get('ESPTOOL_CHIP', 'auto'))

    parser.add_argument(
        '--port', '-p',
        help='Serial port device',
        default=os.environ.get('ESPTOOL_PORT', None))

    parser.add_argument(
        '--baud', '-b',
        help='Serial port baud rate used when flashing/reading',
        type=arg_auto_int,
        default=os.environ.get('ESPTOOL_BAUD', ESPLoader.ESP_ROM_BAUD))

    parser.add_argument(
        '--before',
        help='What to do before connecting to the chip',
        choices=['default_reset', 'usb_reset', 'no_reset', 'no_reset_no_sync'],
        default=os.environ.get('ESPTOOL_BEFORE', 'default_reset'))

    parser.add_argument(
        '--after', '-a',
        help='What to do after esptool.py is finished',
        choices=['hard_reset', 'soft_reset', 'no_reset', 'no_reset_stub'],
        default=os.environ.get('ESPTOOL_AFTER', 'hard_reset'))

    parser.add_argument(
        '--no-stub',
        help="Disable launching the flasher stub, only talk to ROM bootloader. Some features will not be available.",
        action='store_true')

    parser.add_argument(
        '--trace', '-t',
        help="Enable trace-level output of esptool.py interactions.",
        action='store_true')

    parser.add_argument(
        '--override-vddsdio',
        help="Override ESP32 VDDSDIO internal voltage regulator (use with care)",
        choices=ESP32ROM.OVERRIDE_VDDSDIO_CHOICES,
        nargs='?')

    parser.add_argument(
        '--connect-attempts',
        help=('Number of attempts to connect, negative or 0 for infinite. '
              'Default: %d.' % DEFAULT_CONNECT_ATTEMPTS),
        type=int,
        default=os.environ.get('ESPTOOL_CONNECT_ATTEMPTS', DEFAULT_CONNECT_ATTEMPTS))

    subparsers = parser.add_subparsers(
        dest='operation',
        help='Run esptool {command} -h for additional help')

    def add_spi_connection_arg(parent):
        parent.add_argument('--spi-connection', '-sc', help='ESP32-only argument. Override default SPI Flash connection. '
                            'Value can be SPI, HSPI or a comma-separated list of 5 I/O numbers to use for SPI flash (CLK,Q,D,HD,CS).',
                            action=SpiConnectionAction)

    parser_load_ram = subparsers.add_parser(
        'load_ram',
        help='Download an image to RAM and execute')
    parser_load_ram.add_argument('filename', help='Firmware image')

    parser_dump_mem = subparsers.add_parser(
        'dump_mem',
        help='Dump arbitrary memory to disk')
    parser_dump_mem.add_argument('address', help='Base address', type=arg_auto_int)
    parser_dump_mem.add_argument('size', help='Size of region to dump', type=arg_auto_int)
    parser_dump_mem.add_argument('filename', help='Name of binary dump')

    parser_read_mem = subparsers.add_parser(
        'read_mem',
        help='Read arbitrary memory location')
    parser_read_mem.add_argument('address', help='Address to read', type=arg_auto_int)

    parser_write_mem = subparsers.add_parser(
        'write_mem',
        help='Read-modify-write to arbitrary memory location')
    parser_write_mem.add_argument('address', help='Address to write', type=arg_auto_int)
    parser_write_mem.add_argument('value', help='Value', type=arg_auto_int)
    parser_write_mem.add_argument('mask', help='Mask of bits to write',
                                  type=arg_auto_int, nargs='?', default='0xFFFFFFFF')

    def add_spi_flash_subparsers(parent, allow_keep, auto_detect):
        """ Add common parser arguments for SPI flash properties """
        extra_keep_args = ['keep'] if allow_keep else []

        if auto_detect and allow_keep:
            extra_fs_message = ", detect, or keep"
        elif auto_detect:
            extra_fs_message = ", or detect"
        elif allow_keep:
            extra_fs_message = ", or keep"
        else:
            extra_fs_message = ""

        parent.add_argument('--flash_freq', '-ff', help='SPI Flash frequency',
                            choices=extra_keep_args + ['80m', '60m', '48m', '40m',
                                                       '30m', '26m', '24m', '20m', '16m', '15m', '12m'],
                            default=os.environ.get('ESPTOOL_FF', 'keep' if allow_keep else '40m'))
        parent.add_argument('--flash_mode', '-fm', help='SPI Flash mode',
                            choices=extra_keep_args + ['qio', 'qout', 'dio', 'dout'],
                            default=os.environ.get('ESPTOOL_FM', 'keep' if allow_keep else 'qio'))
        parent.add_argument('--flash_size', '-fs', help='SPI Flash size in MegaBytes (1MB, 2MB, 4MB, 8MB, 16MB, 32MB, 64MB, 128MB)'
                            ' plus ESP8266-only (256KB, 512KB, 2MB-c1, 4MB-c1)' + extra_fs_message,
                            action=FlashSizeAction, auto_detect=auto_detect,
                            default=os.environ.get('ESPTOOL_FS', 'keep' if allow_keep else '1MB'))
        add_spi_connection_arg(parent)

    parser_write_flash = subparsers.add_parser(
        'write_flash',
        help='Write a binary blob to flash')

    parser_write_flash.add_argument('addr_filename', metavar='<address> <filename>', help='Address followed by binary filename, separated by space',
                                    action=AddrFilenamePairAction)
    parser_write_flash.add_argument('--erase-all', '-e',
                                    help='Erase all regions of flash (not just write areas) before programming',
                                    action="store_true")

    add_spi_flash_subparsers(parser_write_flash, allow_keep=True, auto_detect=True)
    parser_write_flash.add_argument('--no-progress', '-p', help='Suppress progress output', action="store_true")
    parser_write_flash.add_argument('--verify', help='Verify just-written data on flash '
                                    '(mostly superfluous, data is read back during flashing)', action='store_true')
    parser_write_flash.add_argument('--encrypt', help='Apply flash encryption when writing data (required correct efuse settings)',
                                    action='store_true')
    # In order to not break backward compatibility, our list of encrypted files to flash is a new parameter
    parser_write_flash.add_argument('--encrypt-files', metavar='<address> <filename>',
                                    help='Files to be encrypted on the flash. Address followed by binary filename, separated by space.',
                                    action=AddrFilenamePairAction)
    parser_write_flash.add_argument('--ignore-flash-encryption-efuse-setting', help='Ignore flash encryption efuse settings ',
                                    action='store_true')

    compress_args = parser_write_flash.add_mutually_exclusive_group(required=False)
    compress_args.add_argument('--compress', '-z', help='Compress data in transfer (default unless --no-stub is specified)',
                               action="store_true", default=None)
    compress_args.add_argument('--no-compress', '-u', help='Disable data compression during transfer (default if --no-stub is specified)',
                               action="store_true")

    subparsers.add_parser(
        'run',
        help='Run application code in flash')

    parser_image_info = subparsers.add_parser(
        'image_info',
        help='Dump headers from an application image')
    parser_image_info.add_argument('filename', help='Image file to parse')

    parser_make_image = subparsers.add_parser(
        'make_image',
        help='Create an application image from binary files')
    parser_make_image.add_argument('output', help='Output image file')
    parser_make_image.add_argument('--segfile', '-f', action='append', help='Segment input file')
    parser_make_image.add_argument('--segaddr', '-a', action='append', help='Segment base address', type=arg_auto_int)
    parser_make_image.add_argument('--entrypoint', '-e', help='Address of entry point', type=arg_auto_int, default=0)

    parser_elf2image = subparsers.add_parser(
        'elf2image',
        help='Create an application image from ELF file')
    parser_elf2image.add_argument('input', help='Input ELF file')
    parser_elf2image.add_argument(
        '--output', '-o', help='Output filename prefix (for version 1 image), or filename (for version 2 single image)', type=str)
    parser_elf2image.add_argument('--version', '-e', help='Output image version', choices=['1', '2', '3'], default='1')
    parser_elf2image.add_argument(
        # kept for compatibility
        # Minimum chip revision (deprecated, consider using --min-rev-full)
        "--min-rev",
        "-r",
        # In v3 we do not do help=argparse.SUPPRESS because
        # it should remain visible.
        help="Minimal chip revision (ECO version format)",
        type=int,
        choices=range(256),
        metavar="{0, ... 255}",
        default=0,
    )
    parser_elf2image.add_argument(
        "--min-rev-full",
        help="Minimal chip revision (in format: major * 100 + minor)",
        type=int,
        choices=range(65536),
        metavar="{0, ... 65535}",
        default=0,
    )
    parser_elf2image.add_argument(
        "--max-rev-full",
        help="Maximal chip revision (in format: major * 100 + minor)",
        type=int,
        choices=range(65536),
        metavar="{0, ... 65535}",
        default=65535,
    )
    parser_elf2image.add_argument('--secure-pad', action='store_true',
                                  help='Pad image so once signed it will end on a 64KB boundary. For Secure Boot v1 images only.')
    parser_elf2image.add_argument('--secure-pad-v2', action='store_true',
                                  help='Pad image to 64KB, so once signed its signature sector will start at the next 64K block. '
                                  'For Secure Boot v2 images only.')
    parser_elf2image.add_argument('--elf-sha256-offset', help='If set, insert SHA256 hash (32 bytes) of the input ELF file at specified offset in the binary.',
                                  type=arg_auto_int, default=None)
    parser_elf2image.add_argument('--use_segments', help='If set, ELF segments will be used instead of ELF sections to genereate the image.',
                                  action='store_true')
    parser_elf2image.add_argument('--flash-mmu-page-size', help="Change flash MMU page size.",
                                  choices=['64KB', '32KB', '16KB'])
    parser_elf2image.add_argument(
        "--pad-to-size",
        help="The block size with which the final binary image after padding must be aligned to. Value 0xFF is used for padding, similar to erase_flash",
        default=None,
    )
    add_spi_flash_subparsers(parser_elf2image, allow_keep=False, auto_detect=False)

    subparsers.add_parser(
        'read_mac',
        help='Read MAC address from OTP ROM')

    subparsers.add_parser(
        'chip_id',
        help='Read Chip ID from OTP ROM')

    parser_flash_id = subparsers.add_parser(
        'flash_id',
        help='Read SPI flash manufacturer and device ID')
    add_spi_connection_arg(parser_flash_id)

    parser_read_status = subparsers.add_parser(
        'read_flash_status',
        help='Read SPI flash status register')

    add_spi_connection_arg(parser_read_status)
    parser_read_status.add_argument('--bytes', help='Number of bytes to read (1-3)',
                                    type=int, choices=[1, 2, 3], default=2)

    parser_write_status = subparsers.add_parser(
        'write_flash_status',
        help='Write SPI flash status register')

    add_spi_connection_arg(parser_write_status)
    parser_write_status.add_argument(
        '--non-volatile', help='Write non-volatile bits (use with caution)', action='store_true')
    parser_write_status.add_argument('--bytes', help='Number of status bytes to write (1-3)',
                                     type=int, choices=[1, 2, 3], default=2)
    parser_write_status.add_argument('value', help='New value', type=arg_auto_int)

    parser_read_flash = subparsers.add_parser(
        'read_flash',
        help='Read SPI flash content')
    add_spi_connection_arg(parser_read_flash)
    parser_read_flash.add_argument('address', help='Start address', type=arg_auto_int)
    parser_read_flash.add_argument('size', help='Size of region to dump', type=arg_auto_int)
    parser_read_flash.add_argument('filename', help='Name of binary dump')
    parser_read_flash.add_argument('--no-progress', '-p', help='Suppress progress output', action="store_true")

    parser_verify_flash = subparsers.add_parser(
        'verify_flash',
        help='Verify a binary blob against flash')
    parser_verify_flash.add_argument('addr_filename', help='Address and binary file to verify there, separated by space',
                                     action=AddrFilenamePairAction)
    parser_verify_flash.add_argument('--diff', '-d', help='Show differences',
                                     choices=['no', 'yes'], default='no')
    add_spi_flash_subparsers(parser_verify_flash, allow_keep=True, auto_detect=True)

    parser_erase_flash = subparsers.add_parser(
        'erase_flash',
        help='Perform Chip Erase on SPI flash')
    add_spi_connection_arg(parser_erase_flash)

    parser_erase_region = subparsers.add_parser(
        'erase_region',
        help='Erase a region of the flash')
    add_spi_connection_arg(parser_erase_region)
    parser_erase_region.add_argument('address', help='Start address (must be multiple of 4096)', type=arg_auto_int)
    parser_erase_region.add_argument(
        'size', help='Size of region to erase (must be multiple of 4096)', type=arg_auto_int)

    parser_merge_bin = subparsers.add_parser(
        'merge_bin',
        help='Merge multiple raw binary files into a single file for later flashing')

    parser_merge_bin.add_argument('--output', '-o', help='Output filename', type=str, required=True)
    parser_merge_bin.add_argument('--format', '-f', help='Format of the output file',
                                  choices='raw', default='raw')  # for future expansion
    add_spi_flash_subparsers(parser_merge_bin, allow_keep=True, auto_detect=False)

    parser_merge_bin.add_argument('--target-offset', '-t', help='Target offset where the output file will be flashed',
                                  type=arg_auto_int, default=0)
    parser_merge_bin.add_argument('--fill-flash-size', help='If set, the final binary file will be padded with FF '
                                  'bytes up to this flash size.', action=FlashSizeAction)
    parser_merge_bin.add_argument('addr_filename', metavar='<address> <filename>',
                                  help='Address followed by binary filename, separated by space',
                                  action=AddrFilenamePairAction)

    subparsers.add_parser('get_security_info', help='Get some security-related data')

    subparsers.add_parser('version', help='Print esptool version')

    # internal sanity check - every operation matches a module function of the same name
    for operation in subparsers.choices.keys():
        assert operation in globals(), "%s should be a module function" % operation

    argv = expand_file_arguments(argv or sys.argv[1:])

    args = parser.parse_args(argv)
    print('esptool.py v%s' % __version__)

    # operation function can take 1 arg (args), 2 args (esp, arg)
    # or be a member function of the ESPLoader class.

    if args.operation is None:
        parser.print_help()
        sys.exit(1)

    # Forbid the usage of both --encrypt, which means encrypt all the given files,
    # and --encrypt-files, which represents the list of files to encrypt.
    # The reason is that allowing both at the same time increases the chances of
    # having contradictory lists (e.g. one file not available in one of list).
    if args.operation == "write_flash" and args.encrypt and args.encrypt_files is not None:
        raise FatalError("Options --encrypt and --encrypt-files must not be specified at the same time.")

    operation_func = globals()[args.operation]

    if PYTHON2:
        # This function is depreciated in Python3
        operation_args = inspect.getargspec(operation_func).args
    else:
        operation_args = inspect.getfullargspec(operation_func).args

    if operation_args[0] == 'esp':  # operation function takes an ESPLoader connection object
        if args.before != "no_reset_no_sync":
            initial_baud = min(ESPLoader.ESP_ROM_BAUD, args.baud)  # don't sync faster than the default baud rate
        else:
            initial_baud = args.baud

        if args.port is None:
            ser_list = get_port_list()
            print("Found %d serial ports" % len(ser_list))
        else:
            ser_list = [args.port]
        esp = esp or get_default_connected_device(ser_list, port=args.port, connect_attempts=args.connect_attempts,
                                                  initial_baud=initial_baud, chip=args.chip, trace=args.trace,
                                                  before=args.before)

        if esp is None:
            raise FatalError(
                "Could not connect to an Espressif device on any of the %d available serial ports." % len(ser_list))

        if esp.secure_download_mode:
            print("Chip is %s in Secure Download Mode" % esp.CHIP_NAME)
        else:
            print("Chip is %s" % (esp.get_chip_description()))
            print("Features: %s" % ", ".join(esp.get_chip_features()))
            print("Crystal is %dMHz" % esp.get_crystal_freq())
            read_mac(esp, args)

        if not args.no_stub:
            if esp.secure_download_mode:
                print("WARNING: Stub loader is not supported in Secure Download Mode, setting --no-stub")
                args.no_stub = True
            elif not esp.IS_STUB and esp.stub_is_disabled:
                print("WARNING: Stub loader has been disabled for compatibility, setting --no-stub")
                args.no_stub = True
            else:
                esp = esp.run_stub()

        if args.override_vddsdio:
            esp.override_vddsdio(args.override_vddsdio)

        if args.baud > initial_baud:
            try:
                esp.change_baud(args.baud)
            except NotImplementedInROMError:
                print("WARNING: ROM doesn't support changing baud rate. Keeping initial baud rate %d" % initial_baud)

        # override common SPI flash parameter stuff if configured to do so
        if hasattr(args, "spi_connection") and args.spi_connection is not None:
            if esp.CHIP_NAME != "ESP32":
                raise FatalError("Chip %s does not support --spi-connection option." % esp.CHIP_NAME)
            print("Configuring SPI flash mode...")
            esp.flash_spi_attach(args.spi_connection)
        elif args.no_stub:
            print("Enabling default SPI flash mode...")
            # ROM loader doesn't enable flash unless we explicitly do it
            esp.flash_spi_attach(0)

        # XMC chip startup sequence
        XMC_VENDOR_ID = 0x20

        def is_xmc_chip_strict():
            id = esp.flash_id()
            rdid = ((id & 0xff) << 16) | ((id >> 16) & 0xff) | (id & 0xff00)

            vendor_id = ((rdid >> 16) & 0xFF)
            mfid = ((rdid >> 8) & 0xFF)
            cpid = (rdid & 0xFF)

            if vendor_id != XMC_VENDOR_ID:
                return False

            matched = False
            if mfid == 0x40:
                if cpid >= 0x13 and cpid <= 0x20:
                    matched = True
            elif mfid == 0x41:
                if cpid >= 0x17 and cpid <= 0x20:
                    matched = True
            elif mfid == 0x50:
                if cpid >= 0x15 and cpid <= 0x16:
                    matched = True
            return matched

        def flash_xmc_startup():
            # If the RDID value is a valid XMC one, may skip the flow
            fast_check = True
            if fast_check and is_xmc_chip_strict():
                return  # Successful XMC flash chip boot-up detected by RDID, skipping.

            sfdp_mfid_addr = 0x10
            mf_id = esp.read_spiflash_sfdp(sfdp_mfid_addr, 8)
            if mf_id != XMC_VENDOR_ID:  # Non-XMC chip detected by SFDP Read, skipping.
                return

            print("WARNING: XMC flash chip boot-up failure detected! Running XMC25QHxxC startup flow")
            esp.run_spiflash_command(0xB9)  # Enter DPD
            esp.run_spiflash_command(0x79)  # Enter UDPD
            esp.run_spiflash_command(0xFF)  # Exit UDPD
            time.sleep(0.002)               # Delay tXUDPD
            esp.run_spiflash_command(0xAB)  # Release Power-Down
            time.sleep(0.00002)
            # Check for success
            if not is_xmc_chip_strict():
                print("WARNING: XMC flash boot-up fix failed.")
            print("XMC flash chip boot-up fix successful!")

        # Check flash chip connection
        if not esp.secure_download_mode:
            try:
                flash_id = esp.flash_id()
                if flash_id in (0xffffff, 0x000000):
                    print('WARNING: Failed to communicate with the flash chip, read/write operations will fail. '
                          'Try checking the chip connections or removing any other hardware connected to IOs.')
            except Exception as e:
                esp.trace('Unable to verify flash chip connection ({}).'.format(e))

        # Check if XMC SPI flash chip booted-up successfully, fix if not
        if not esp.secure_download_mode:
            try:
                flash_xmc_startup()
            except Exception as e:
                esp.trace('Unable to perform XMC flash chip startup sequence ({}).'.format(e))

        if hasattr(args, "flash_size"):
            print("Configuring flash size...")
            detect_flash_size(esp, args)
            if args.flash_size != 'keep':  # TODO: should set this even with 'keep'
                esp.flash_set_parameters(flash_size_bytes(args.flash_size))
                # Check if stub supports chosen flash size
                if esp.IS_STUB and args.flash_size in ('32MB', '64MB', '128MB'):
                    print("WARNING: Flasher stub doesn't fully support flash size larger than 16MB, in case of failure use --no-stub.")

        if esp.IS_STUB and hasattr(args, "address") and hasattr(args, "size"):
            if args.address + args.size > 0x1000000:
                print("WARNING: Flasher stub doesn't fully support flash size larger than 16MB, in case of failure use --no-stub.")

        try:
            operation_func(esp, args)
        finally:
            try:  # Clean up AddrFilenamePairAction files
                for address, argfile in args.addr_filename:
                    argfile.close()
            except AttributeError:
                pass

        # Handle post-operation behaviour (reset or other)
        if operation_func == load_ram:
            # the ESP is now running the loaded image, so let it run
            print('Exiting immediately.')
        elif args.after == 'hard_reset':
            esp.hard_reset()
        elif args.after == 'soft_reset':
            print('Soft resetting...')
            # flash_finish will trigger a soft reset
            esp.soft_reset(False)
        elif args.after == 'no_reset_stub':
            print('Staying in flasher stub.')
        else:  # args.after == 'no_reset'
            print('Staying in bootloader.')
            if esp.IS_STUB:
                esp.soft_reset(True)  # exit stub back to ROM loader

        if not external_esp:
            esp._port.close()

    else:
        operation_func(args)


def get_port_list():
    if list_ports is None:
        raise FatalError(
            "Listing all serial ports is currently not available. "
            "Please try to specify the port when running esptool.py or update "
            "the pyserial package to the latest version"
        )
    port_list = sorted(ports.device for ports in list_ports.comports())
    if sys.platform == "darwin":
        port_list = [
            port
            for port in port_list
            if not port.endswith(("Bluetooth-Incoming-Port", "wlan-debug"))
        ]
    return port_list


def expand_file_arguments(argv):
    """ Any argument starting with "@" gets replaced with all values read from a text file.
    Text file arguments can be split by newline or by space.
    Values are added "as-is", as if they were specified in this order on the command line.
    """
    new_args = []
    expanded = False
    for arg in argv:
        if arg.startswith("@"):
            expanded = True
            with open(arg[1:], "r") as f:
                for line in f.readlines():
                    new_args += shlex.split(line)
        else:
            new_args.append(arg)
    if expanded:
        print("esptool.py %s" % (" ".join(new_args[1:])))
        return new_args
    return argv


class FlashSizeAction(argparse.Action):
    """ Custom flash size parser class to support backwards compatibility with megabit size arguments.

    (At next major relase, remove deprecated sizes and this can become a 'normal' choices= argument again.)
    """

    def __init__(self, option_strings, dest, nargs=1, auto_detect=False, **kwargs):
        super(FlashSizeAction, self).__init__(option_strings, dest, nargs, **kwargs)
        self._auto_detect = auto_detect

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            value = {
                '2m': '256KB',
                '4m': '512KB',
                '8m': '1MB',
                '16m': '2MB',
                '32m': '4MB',
                '16m-c1': '2MB-c1',
                '32m-c1': '4MB-c1',
            }[values[0]]
            print("WARNING: Flash size arguments in megabits like '%s' are deprecated." % (values[0]))
            print("Please use the equivalent size '%s'." % (value))
            print("Megabit arguments may be removed in a future release.")
        except KeyError:
            value = values[0]

        known_sizes = dict(ESP8266ROM.FLASH_SIZES)
        known_sizes.update(ESP32ROM.FLASH_SIZES)
        if self._auto_detect:
            known_sizes['detect'] = 'detect'
            known_sizes['keep'] = 'keep'
        if value not in known_sizes:
            raise argparse.ArgumentError(self, '%s is not a known flash size. Known sizes: %s' %
                                         (value, ", ".join(known_sizes.keys())))
        setattr(namespace, self.dest, value)


class SpiConnectionAction(argparse.Action):
    """ Custom action to parse 'spi connection' override. Values are SPI, HSPI, or a sequence of 5 pin numbers separated by commas.
    """

    def __call__(self, parser, namespace, value, option_string=None):
        if value.upper() == "SPI":
            value = 0
        elif value.upper() == "HSPI":
            value = 1
        elif "," in value:
            values = value.split(",")
            if len(values) != 5:
                raise argparse.ArgumentError(
                    self, '%s is not a valid list of comma-separate pin numbers. Must be 5 numbers - CLK,Q,D,HD,CS.' % value)
            try:
                values = tuple(int(v, 0) for v in values)
            except ValueError:
                raise argparse.ArgumentError(
                    self, '%s is not a valid argument. All pins must be numeric values' % values)
            if any([v for v in values if v > 33 or v < 0]):
                raise argparse.ArgumentError(self, 'Pin numbers must be in the range 0-33.')
            # encode the pin numbers as a 32-bit integer with packed 6-bit values, the same way ESP32 ROM takes them
            # TODO: make this less ESP32 ROM specific somehow...
            clk, q, d, hd, cs = values
            value = (hd << 24) | (cs << 18) | (d << 12) | (q << 6) | clk
        else:
            raise argparse.ArgumentError(self, '%s is not a valid spi-connection value. '
                                         'Values are SPI, HSPI, or a sequence of 5 pin numbers CLK,Q,D,HD,CS).' % value)
        setattr(namespace, self.dest, value)


class AddrFilenamePairAction(argparse.Action):
    """ Custom parser class for the address/filename pairs passed as arguments """

    def __init__(self, option_strings, dest, nargs='+', **kwargs):
        super(AddrFilenamePairAction, self).__init__(option_strings, dest, nargs, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        # validate pair arguments
        pairs = []
        for i in range(0, len(values), 2):
            try:
                address = int(values[i], 0)
            except ValueError:
                raise argparse.ArgumentError(self, 'Address "%s" must be a number' % values[i])
            try:
                argfile = open(values[i + 1], 'rb')
            except IOError as e:
                raise argparse.ArgumentError(self, e)
            except IndexError:
                raise argparse.ArgumentError(self, 'Must be pairs of an address and the binary filename to write there')
            pairs.append((address, argfile))

        # Sort the addresses and check for overlapping
        end = 0
        for address, argfile in sorted(pairs, key=lambda x: x[0]):
            argfile.seek(0, 2)  # seek to end
            size = argfile.tell()
            argfile.seek(0)
            sector_start = address & ~(ESPLoader.FLASH_SECTOR_SIZE - 1)
            sector_end = ((address + size + ESPLoader.FLASH_SECTOR_SIZE - 1) & ~(ESPLoader.FLASH_SECTOR_SIZE - 1)) - 1
            if sector_start < end:
                message = 'Detected overlap at address: 0x%x for file: %s' % (address, argfile.name)
                raise argparse.ArgumentError(self, message)
            end = sector_end
        setattr(namespace, self.dest, pairs)


# Binary stub code (see flasher_stub dir for source & details)
ESP8266ROM.STUB_CODE = eval(zlib.decompress(base64.b64decode(b"""
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\
""")))
ESP32ROM.STUB_CODE = eval(zlib.decompress(base64.b64decode(b"""
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\
""")))
ESP32S2ROM.STUB_CODE = eval(zlib.decompress(base64.b64decode(b"""
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\
""")))
ESP32S3ROM.STUB_CODE = eval(zlib.decompress(base64.b64decode(b"""
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\
""")))
ESP32C3ROM.STUB_CODE = eval(zlib.decompress(base64.b64decode(b"""
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\
""")))
ESP32C6ROM.STUB_CODE = eval(zlib.decompress(base64.b64decode(b"""
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\
PFn+C4psGGw=\
""")))
ESP32H2ROM.STUB_CODE = eval(zlib.decompress(base64.b64decode(b"""
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\
+hfcvBvW\
""")))
ESP32C2ROM.STUB_CODE = eval(zlib.decompress(base64.b64decode(b"""
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\
""")))


def _main():
    try:
        main()
    except FatalError as e:
        print('\nA fatal error occurred: %s' % e)
        sys.exit(2)


if __name__ == '__main__':
    _main()
