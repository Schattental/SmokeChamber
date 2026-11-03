# -*- coding: utf-8 -*-
"""
  @file read_mhz16_co2.py
  @brief Example script to read CO2 from MH-Z16 sensor
"""

import time
from MHZ16_CO2 import MHZ16_CO2

# ------------------------------
# Configuration
# ------------------------------
UART_PORT_PI = "/dev/ttyAMA0" # default for pins on Pi, Bank 1
# DON'T USE /dev/ttyUSB0 IT CAN CHANGE TO USB1 or something else I guess
UART_PORT_USB = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0" # port for USB to TTL adapter, Bank 2
BAUDRATE = 9600

sensor1 = MHZ16_CO2(port=UART_PORT_USB, baudrate=BAUDRATE)
sensor2 = MHZ16_CO2(port=UART_PORT_PI, baudrate=BAUDRATE)


def setup():
    print("MH-Z16 CO2 Sensors initialized")
    print("Sensor requires 3 minutes preheat time")
    time.sleep(1)


def loop():
    sensor1_co2 = sensor1.read_co2()
    sensor2_co2 = sensor2.read_co2()


    if sensor1_co2 is not None:
        print(f"CO2 concentration S1 (USB): {sensor1_co2} ppm")
    else:
        print("Read error sensor1 (USB)")

    if sensor2_co2 is not None:
        print(f"CO2 concentration S2 (Pi): {sensor2_co2} ppm")
    else:
        print("Read error sensor2 (Pi)")

    time.sleep(1)


if __name__ == "__main__":
    setup()
    while True:
        loop()
