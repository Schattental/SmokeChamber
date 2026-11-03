import serial
ser = serial.Serial("/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0", 9600, timeout=1)
ser.reset_input_buffer()
ser.reset_output_buffer()
ser.write(b'hello')
print(ser.read(5))

