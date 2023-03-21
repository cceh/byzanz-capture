import asyncio
import logging
from asyncio import Future
from enum import Enum, auto

from PyQt6.QtCore import QObject, pyqtSignal, QEventLoop
from PyQt6.QtWidgets import QApplication
from bleak import BleakClient, BleakGATTCharacteristic, BleakError


class BtControllerCommand(Enum):
    LED_ON = 0x01
    LED_OFF = 0x02
    SET_LED = 0x03
    PILOT_LIGHT_ON = 0x04


class BtControllerResponse(Enum):
    OK = 0x01
    ERR_INVALID_CMD = 0x10
    ERR_INVALID_LED = 0x11


class BtControllerState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    DISCONNECTING = auto()


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

    state_changed = pyqtSignal(BtControllerState)

    def __init__(self, parent=None):
        super(BtControllerController, self).__init__(parent)
        self._logger = logging.getLogger(self.__class__.__name__)
        self._device_address = None
        self._client = BleakClient(self.DEVICE_ADDRESS, disconnected_callback=self._disconnect_callback)
        self._queue: asyncio.Queue[tuple[BtControllerResponse, int | None]] = asyncio.Queue()
        self._state: BtControllerState = BtControllerState.DISCONNECTED

        self.keep_connected = True
        asyncio.create_task(self._connect())

    def send_command(self, request: BtControllerRequest):
        def done_callback(future: Future):
            try:
                if future.result() == True:
                    request.signals.success.emit()
            except Exception as e:
                request.signals.error.emit(e)

        future = asyncio.run_coroutine_threadsafe(self._send_command(request.command, request.param), asyncio.get_running_loop())
        future.add_done_callback(done_callback)

    def bt_disconnect(self):
        self.keep_connected = False
        self._logger.info("Disconnecting...")
        self._set_state(BtControllerState.DISCONNECTING)
        asyncio.run_coroutine_threadsafe(self._client.disconnect(), asyncio.get_running_loop())

        while self._client.is_connected:
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    @property
    def state(self) -> BtControllerState:
        return self._state

    def _set_state(self, new_state: BtControllerState):
        self._state = new_state
        self.state_changed.emit(new_state)

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
                if response == BtControllerResponse.OK and param == command.value:
                    break

            return True

        self._logger.info(f"Sending command {command.name}")
        try:
            # Attempt to send the command and wait for the response within the specified timeout
            return await asyncio.wait_for(send_and_wait_for_response(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Command {command.name} timed out after {timeout} seconds")

    def _disconnect_callback(self, _client: BleakClient):
        self._set_state(BtControllerState.DISCONNECTED)
        self._logger.info("Device disconnected.")
        for task in asyncio.all_tasks():
            task.cancel()

        asyncio.create_task(self._connect())

    async def _connect(self):
        while self.keep_connected:
            try:
                self._logger.info("Connecting...")
                self._set_state(BtControllerState.CONNECTING)
                await self._client.connect()
                await self._client.start_notify(self.BLE_CHARACTERISTIC_UUID, self._response_callback)
                self._logger.info("Connected.")
                self._set_state(BtControllerState.CONNECTED)
                break

            except BleakError as e:
                self._logger.exception(e)
                self._logger.info("Retrying connection...")
                await asyncio.sleep(1)
