import re
import sys
import threading
import os
import platform
import distro

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QComboBox,
                             QFileDialog, QTextEdit, QGroupBox, QGridLayout, QCheckBox)
from PyQt6.QtGui import QColor, QTextCursor, QPalette, QColor
from PyQt6.QtCore import pyqtSignal, QObject

from emsesp_flasher.own_esptool import get_port_list
from emsesp_flasher.const import __version__

COLOR_RE = re.compile(r'\x1b\[(.*?)[@-~]|\].*?(\x07|\x1b\\)')
COLORS = {
    'black': QColor('black'),
    'red': QColor('red'),
    'green': QColor('green'),
    'yellow': QColor('yellow'),
    'blue': QColor('blue'),
    'magenta': QColor('magenta'),
    'cyan': QColor('cyan'),
    'white': QColor('white'),
}
FORE_COLORS = {**COLORS, None: QColor('white')}
BACK_COLORS = {**COLORS, None: QColor('black')}


class RedirectText(QObject):
    text_written = pyqtSignal(str)

    def __init__(self, text_edit):
        super().__init__()
        self._out = text_edit
        self._line = ''
        self._bold = False
        self._italic = False
        self._underline = False
        self._foreground = None
        self._background = None
        self._secret = False
        self.text_written.connect(self._append_text)

    def write(self, string):
        self.text_written.emit(string)

    def flush(self):
        pass

    def _append_text(self, text):
        cursor = self._out.textCursor()
        self._out.moveCursor(QTextCursor.MoveOperation.End)
        self._out.insertPlainText(text)
        self._out.setTextCursor(cursor)


class FlashingThread(threading.Thread):
    def __init__(self, firmware, port, no_erase=True, show_logs=False):
        threading.Thread.__init__(self)
        self.daemon = True
        self._firmware = firmware
        self._port = port
        self._no_erase = no_erase
        self._show_logs = show_logs

    def run(self):
        try:
            from emsesp_flasher.__main__ import run_emsesp_flasher

            argv = ['emsesp_flasher', '--port', self._port, self._firmware]

            if self._show_logs:
                argv.append('--show-logs')

            if self._no_erase:
                argv.append('--no-erase')

            run_emsesp_flasher(argv)

        except Exception as e:
            print("Unexpected error: {}".format(e))
            raise


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self._firmware = None
        self._port = None
        self._no_erase = True

        self.init_ui()
        sys.stdout = RedirectText(self.console)  # Redirect stdout to console

    def init_ui(self):
        self.setWindowTitle(f"EMS-ESP Flasher {__version__}")
        self.setGeometry(50, 50, 800, 700)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        vbox = QVBoxLayout()

        port_group_box = QGroupBox()
        port_layout = QGridLayout()
        port_label = QLabel("Select Port:")
        self.port_combobox = QComboBox()
        self.reload_ports()
        self.port_combobox.currentIndexChanged.connect(self.select_port)
        reload_button = QPushButton("Refresh")
        reload_button.clicked.connect(self.reload_ports)
        port_layout.addWidget(port_label, 0, 0)
        port_layout.addWidget(self.port_combobox, 0, 1)
        port_layout.addWidget(reload_button, 0, 2)
        port_group_box.setLayout(port_layout)

        firmware_group_box = QGroupBox()
        firmware_layout = QGridLayout()
        firmware_label = QLabel("Select Firmware:")
        self.firmware_button = QPushButton("Browse...")
        self.firmware_button.clicked.connect(self.pick_file)
        firmware_layout.addWidget(firmware_label, 0, 0)
        firmware_layout.addWidget(self.firmware_button, 0, 1)
        firmware_group_box.setLayout(firmware_layout)

        actions_group_box = QGroupBox()
        actions_layout = QHBoxLayout()
        self.flash_button = QPushButton("Flash EMS-ESP Firmware")
        self.flash_button.clicked.connect(self.flash_esp)
        self.no_erase_checkbox = QCheckBox("Keep settings")
        self.no_erase_checkbox.setChecked(True)
        self.no_erase_checkbox.stateChanged.connect(self.no_erase_flash)
        actions_layout.addWidget(self.no_erase_checkbox)
        actions_layout.addWidget(self.flash_button)
        actions_group_box.setLayout(actions_layout)

        console_group_box = QGroupBox("Console Output")
        console_group_box.setStyleSheet(
            # "border: 2px solid gray; "
            "border-radius: 5px; "
            "margin-top: 2ex; "
            "font-size: 14px;"
            "color: lightblue;"
        )
        console_layout = QVBoxLayout()
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        console_layout.addWidget(self.console)
        console_group_box.setLayout(console_layout)

        vbox.addWidget(port_group_box)
        vbox.addWidget(firmware_group_box)
        vbox.addWidget(actions_group_box)
        vbox.addWidget(console_group_box)

        central_widget.setLayout(vbox)

    def reload_ports(self):
        self.port_combobox.clear()
        ports = get_port_list()
        if ports:
            self.port_combobox.addItems(ports)
            self._port = ports[0]
        else:
            self.port_combobox.addItem("")

    def select_port(self, index):
        self._port = self.port_combobox.itemText(index)

    def pick_file(self):
        # options = QFileDialog.options()
        file_name, _ = QFileDialog.getOpenFileName(
            self, "Select Firmware File", "", "Binary Files (*.bin)")
        if file_name:
            self._firmware = file_name
            self.firmware_button.setText(file_name)

    def flash_esp(self):
        self.console.clear()
        if self._firmware and self._port:
            worker = FlashingThread(self._firmware, self._port, self._no_erase)
            worker.start()

    def view_logs(self):
        self.console.clear()
        if self._port:
            worker = FlashingThread('dummy', self._port, show_logs=True)
            worker.start()

    def no_erase_flash(self):
        self._no_erase = self.no_erase_checkbox.isChecked()


def main():

    os_name = platform.system()
    if os_name == 'Darwin':
        os.environ['QT_QPA_PLATFORM'] = 'cocoa'
    elif os_name == 'Linux':
        distro_name = distro.id().lower()
        if 'ubuntu' in distro_name or 'debian' in distro_name:
            os.environ['QT_QPA_PLATFORM'] = 'wayland'
        else:
            os.environ['QT_QPA_PLATFORM'] = 'xcb'
    elif os_name == 'Windows':
        os.environ['QT_QPA_PLATFORM'] = 'windows'
    else:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'

    app = QApplication(sys.argv)

    app.setStyle("Fusion")
    app.setStyleSheet("QGroupBox { border: 1px solid lightblue; }")

    main_window = MainWindow()
    main_window.show()

    # uncomment below section for quick testing
    # test auto-loading
    # worker = FlashingThread("test/EMS-ESP-3_7_0-dev_39-ESP32S3-16MB+.bin", "COM6")  # S3
    # worker = FlashingThread("test/EMS-ESP-3_7_0-dev_39-ESP32-16MB+.bin", "COM6")  # E32 V2
    # worker = FlashingThread('dummy', "COM6", False, True)  # test just show console
    # worker.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
