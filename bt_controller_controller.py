import asyncio
import logging
from asyncio import Future
from enum import Enum

import qasync
from PyQt6.QtCore import QObject, pyqtSignal
from bleak import BLEDevice, BleakClient, BleakScanner, AdvertisementData, BleakGATTCharacteristic, BleakError


class BtControllerCommand(Enum):
    BT_CMD_LED_ON = 0x01
    BT_CMD_LED_OFF = 0x02
    BT_CMD_SET_LED = 0x03
    BT_CMD_PILOT_LIGHT_ON = 0x04


class BtControllerResponse(Enum):
    BT_RESP_OK = 0x01
    BT_RESP_ERR_INVALID_CMD = 0x10
    BT_RESP_ERR_INVALID_LED = 0x11


class BtControllerRequest:
    class Signals(QObject):
        error = pyqtSignal(Exception)
        success = pyqtSignal()

    def __init__(self, command: BtControllerCommand, param: int = None):
        self.param = param
        self.command = command
        self.signals = BtControllerRequest.Signals()

class BtControllerController(QObject):
    DEVICE_NAME = "CCeH Dome Controller"
    DEVICE_ADDRESS = "00:0E:0B:10:45:63"
    BLE_CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

    send_command = pyqtSignal(BtControllerRequest)

    def __init__(self, parent=None):
        super(BtControllerController, self).__init__(parent)
        self._logger = logging.getLogger(self.__class__.__name__)
        self._device_address = None
        self._client: BleakClient | None = None
        self._queue: asyncio.Queue[tuple[BtControllerResponse, int | None]] = asyncio.Queue()

        self.keep_connected = True


    # async def scan_callback(self, device: BLEDevice, _advertisement_data: AdvertisementData):
    #     if device.address == "00:0E:0B:10:45:63":
    #         self._device_address = device.address
    #         self._logger.info(f"Found {self.DEVICE_NAME} with address {self._device_address}.")

    def schedule_command(self, request: BtControllerRequest):
        def done_callback(future: Future):
            try:
                if future.result() == True:
                    request.signals.success.emit()
            except Exception as e:
                request.signals.error.emit(e)

        future = asyncio.run_coroutine_threadsafe(self._send_command(request.command, request.param), asyncio.get_event_loop())
        future.add_done_callback(done_callback)

    def _response_callback(self, _sender: BleakGATTCharacteristic, data: bytearray):
        try:
            response = BtControllerResponse(data[0])
            logging.info(f"Got response: {response.name}")
            if len(data) == 2:
                self._logger.info(f"Response param: {data[1]}")
                self._queue.put_nowait((response, data[1]))
            else:
                self._queue.put_nowait((response, None))

        except ValueError:
            self._logger.error(f"Invalid response: {data[0]}")

    async def _send_command(self, command: BtControllerCommand, param: int = None, timeout: float = 5.0):
        async def send_and_wait_for_response():
            nonlocal param
            # await self._client.is_connected()

            command_bytes = bytearray([command.value])
            if param is not None:
                command_bytes.append(param)
            self._logger.info(command_bytes)

            await self._client.write_gatt_char(self.BLE_CHARACTERISTIC_UUID, command_bytes)
            while True:
                response, param = await self._queue.get()
                if response == BtControllerResponse.BT_RESP_OK and param == command.value:
                    break

            return True

        self._logger.info(f"Sending command {command.name}")
        try:
            # Attempt to send the command and wait for the response within the specified timeout
            return await asyncio.wait_for(send_and_wait_for_response(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Command {command.name} timed out after {timeout} seconds")

    def _disconnect_callback(self, _client: BleakClient):
        self._logger.info("Device disconnected.")
        for task in asyncio.all_tasks():
            task.cancel()

        asyncio.create_task(self._connect())

    async def _connect(self):
        self._logger.info("Connecting...")
        while self.keep_connected:
            try:
                self._client = BleakClient(self.DEVICE_ADDRESS, disconnected_callback=self._disconnect_callback)
                await self._client.connect()
                await self._client.start_notify(self.BLE_CHARACTERISTIC_UUID, self._response_callback)
                self._logger.info("Connected.")
                break

            except BleakError as e:
                self._logger.exception(e)
                self._logger.info("Retrying connection...")
                await asyncio.sleep(1)

    async def main(self):
        self._logger.info("Controller Controller starting")

        # scanner = BleakScanner(self.scan_callback)
        # while self._device_address is None:
        #     await scanner.start()
        #     while self._device_address is None:
        #         await asyncio.sleep(0.5)
        #     # device = await scanner.find_device_by_filter(lambda device, _adv_data: device.name == self.DEVICE_NAME)
        #     # if device:
        #     #    self._device_address = device.address
        #
        # await scanner.stop()

        await self._connect()
        # while True:
        #     for i in range(14, 59):
        #         self._logger.info(i)
        #
        #         # await self.send_command(BtControllerCommand.BT_CMD_LED_OFF)
        #         await self._send_command(BtControllerCommand.BT_CMD_SET_LED, i)
        #         # await asyncio.sleep(0.5)
        #         await self._send_command(BtControllerCommand.BT_CMD_LED_ON)
        #         await asyncio.sleep(0.5)
        #         await self._send_command(BtControllerCommand.BT_CMD_LED_OFF)


    def run(self):
        self.send_command.connect(self.schedule_command)

        loop = qasync.QEventLoop(self)
        asyncio.set_event_loop(loop)
        loop.create_task(self.main())
        loop.run_forever()
        # asyncio.run(self.main())

    def exec(self):
        self.thread().exec()

