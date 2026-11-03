# -*- coding: utf-8 -*-
"""
  @file MHZ16_CO2.py
  @brief MH-Z16 CO2 sensor driver (UART)
  @note  Communication protocol according to MH-Z16 datasheet
"""

import serial
import time
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class MHZ16_CO2:

    READ_CMD = 0x86
    ZERO_CMD = 0x87
    SPAN_CMD = 0x88

    def __init__(self, port="/dev/ttyAMA0", baudrate=9600, timeout=1):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout

        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            stopbits=serial.STOPBITS_ONE,
            parity=serial.PARITY_NONE,
            timeout=self.timeout
        )

        time.sleep(2)  # allow serial to stabilize

    # ------------------------------
    # Internal checksum calculation
    # ------------------------------
    def _checksum(self, packet):
        checksum = sum(packet[1:8]) & 0xFF
        checksum = (0xFF - checksum + 1) & 0xFF
        return checksum

    # ------------------------------
    # Send command
    # ------------------------------
    def _send_command(self, command, data_bytes=None):
        if data_bytes is None:
            data_bytes = [0, 0, 0, 0, 0]

        packet = [0xFF, 0x01, command] + data_bytes[:5]
        packet = packet[:8]  # ensure length before checksum
        packet.append(self._checksum(packet))

        self.ser.flushInput()
        self.ser.write(bytearray(packet))
        time.sleep(0.1)

        return packet

    # ------------------------------
    # Read CO2 concentration
    # ------------------------------
    def read_co2(self):
        self._send_command(self.READ_CMD)

        response = self.ser.read(9)
        if len(response) != 9:
            logger.error("Incomplete response from sensor")
            return None

        response = list(response)

        if self._checksum(response) != response[8]:
            logger.error("Checksum mismatch")
            return None

        if response[0] != 0xFF or response[1] != 0x86:
            logger.error("Invalid response header")
            return None

        high = response[2]
        low = response[3]

        co2 = high * 256 + low
        return co2

    # ------------------------------
    # Zero calibration
    # ------------------------------
    def zero_calibration(self):
        self._send_command(self.ZERO_CMD)

    # ------------------------------
    # Span calibration
    # ------------------------------
    def span_calibration(self, span_ppm):
        high = (span_ppm >> 8) & 0xFF
        low = span_ppm & 0xFF
        self._send_command(self.SPAN_CMD, [high, low, 0, 0, 0])

    # ------------------------------
    # Close serial
    # ------------------------------
    def close(self):
        if self.ser.is_open:
            self.ser.close()
