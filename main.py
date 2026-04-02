import asyncio
import struct
import logging
import threading
import queue
from kivy.app import App
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.relativelayout import RelativeLayout
from kivy.uix.label import Label
from kivy.graphics import Rectangle, Ellipse, Color, Line
from kivy.clock import Clock

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ------------------ Modbus RTU функции ------------------
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF

def build_request(slave_id: int, function_code: int, start_address: int, quantity: int) -> bytes:
    addr_bytes = struct.pack('>H', start_address)
    quant_bytes = struct.pack('>H', quantity)
    request = bytes([slave_id, function_code]) + addr_bytes + quant_bytes
    crc = crc16_modbus(request)
    request += struct.pack('<H', crc)
    return request

def parse_response(response: bytes, slave_id: int, function_code: int, expected_quantity: int) -> list:
    if len(response) < 7:
        raise ValueError(f"Слишком короткий ответ: {len(response)} байт")
    if response[0] != slave_id:
        raise ValueError(f"Неверный slave ID: {response[0]}, ожидался {slave_id}")
    if response[1] != function_code:
        if response[1] == function_code | 0x80:
            raise Exception(f"Modbus ошибка: код {response[1]}, доп. байт {response[2]}")
        else:
            raise ValueError(f"Неверный код функции: {response[1]}, ожидался {function_code}")
    byte_count = response[2]
    expected_byte_count = expected_quantity * 2
    if byte_count != expected_byte_count:
        raise ValueError(f"Неверное количество байт данных: {byte_count}, ожидалось {expected_byte_count}")
    data = response[3:3+byte_count]
    received_crc = struct.unpack('<H', response[-2:])[0]
    computed_crc = crc16_modbus(response[:-2])
    if received_crc != computed_crc:
        raise ValueError(f"Ошибка CRC: получено 0x{received_crc:04X}, вычислено 0x{computed_crc:04X}")
    values = []
    for i in range(0, len(data), 2):
        raw = struct.unpack('>H', data[i:i+2])[0]
        if raw & 0x8000:
            value = raw - 0x10000
        else:
            value = raw
        values.append(value)
    return values

async def read_input_registers_async(host: str, port: int, slave_id: int, start_address: int, quantity: int, timeout: float = 5.0) -> list:
    request = build_request(slave_id, 0x03, start_address, quantity)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        raise TimeoutError(f"Таймаут подключения к {host}:{port}")

    writer.write(request)
    await writer.drain()

    total_expected = 1 + 1 + 1 + 2*quantity + 2
    response = b''
    try:
        while len(response) < total_expected:
            chunk = await asyncio.wait_for(
                reader.read(total_expected - len(response)),
                timeout=timeout
            )
            if not chunk:
                raise ConnectionError("Соединение закрыто")
            response += chunk
    except asyncio.TimeoutError:
        writer.close()
        await writer.wait_closed()
        raise TimeoutError(f"Таймаут чтения от {host}:{port}")
    except Exception:
        writer.close()
        await writer.wait_closed()
        raise

    writer.close()
    await writer.wait_closed()
    return parse_response(response, slave_id, 0x03, quantity)

# ------------------ Конфигурация датчиков ------------------
SENSORS = [
    {"name": "Температура дымовых газов", "host": "192.168.1.140", "port": 502, "slave_id": 1, "address": 0xA0, "quantity": 1},
    {"name": "Температура в теплообменнике", "host": "192.168.1.140", "port": 502, "slave_id": 1, "address": 0xA1, "quantity": 1},
    {"name": "Температура полка", "host": "192.168.1.140", "port": 502, "slave_id": 1, "address": 0xA2, "quantity": 1},
    {"name": "Температура воды", "host": "192.168.1.140", "port": 502, "slave_id": 1, "address": 0xA3, "quantity": 1},
]

INTERVAL = 5.0      # интервал между полными циклами опроса (сек)
RETRY_DELAY = 1.0
MAX_RETRIES = 3

# ------------------ Асинхронный опрос (последовательный) ------------------
async def sequential_sensor_task(sensors, stop_event, data_queue, interval):
    """Опрашивает все датчики по очереди, затем ждёт interval."""
    while not stop_event.is_set():
        try:
            for sensor in sensors:
                if stop_event.is_set():
                    break
                name = sensor["name"]
                host = sensor["host"]
                port = sensor["port"]
                slave_id = sensor["slave_id"]
                address = sensor["address"]
                quantity = sensor["quantity"]

                attempts = 0
                while attempts < MAX_RETRIES and not stop_event.is_set():
                    try:
                        values = await read_input_registers_async(host, port, slave_id, address, quantity)
                        temperature = values[0] / 10.0
                        await data_queue.put(('data', name, temperature))
                        break
                    except Exception as e:
                        attempts += 1
                        if attempts >= MAX_RETRIES:
                            await data_queue.put(('error', name, str(e)))
                        else:
                            logging.warning(f"{name}: попытка {attempts}/{MAX_RETRIES} не удалась: {e}")
                            try:
                                await asyncio.wait_for(stop_event.wait(), timeout=RETRY_DELAY)
                            except asyncio.TimeoutError:
                                continue
                await asyncio.sleep(0.2)   # задержка между датчиками
            await asyncio.sleep(interval)
        except Exception as e:
            logging.error(f"Критическая ошибка в sequential_sensor_task: {e}", exc_info=True)
            await asyncio.sleep(1)

async def modbus_manager(thread_queue: queue.Queue, stop_event: asyncio.Event):
    data_queue = asyncio.Queue()
    task = asyncio.create_task(sequential_sensor_task(SENSORS, stop_event, data_queue, INTERVAL))

    while not stop_event.is_set():
        try:
            typ, name, value = await asyncio.wait_for(data_queue.get(), timeout=0.5)
            thread_queue.put((typ, name, value))
        except asyncio.TimeoutError:
            pass

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    logging.info("Менеджер Modbus завершил работу")

def run_async_manager(thread_queue, stop_thread_event):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    async def watch_stop():
        while not stop_thread_event.is_set():
            await asyncio.sleep(0.2)
        stop_event.set()

    try:
        loop.run_until_complete(asyncio.gather(
            modbus_manager(thread_queue, stop_event),
            watch_stop()
        ))
    except Exception as e:
        print(f"Ошибка в асинхронном потоке: {e}")
        import traceback
        traceback.print_exc()
    finally:
        loop.close()

# ------------------ Kivy GUI ------------------
class BuildingPlan(RelativeLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.temperature_texts = {}
        self.pending_data = {}
        self._first_draw = False
        self.bind(size=self.on_size)

    def on_size(self, instance, size):
        if not self._first_draw and size[0] > 0 and size[1] > 0:
            self.draw_plan()
            self._first_draw = True

    def draw_plan(self):
        self.clear_widgets()
        self.canvas.clear()
        self.temperature_texts.clear()

        width, height = self.width, self.height
        if width == 0 or height == 0:
            return

        half_w = width // 2
        half_h = height // 2

        self.draw_room_tk(0, 0, half_w, half_h, "Парная", "парная")
        self.draw_room_tk(half_w, 0, width, half_h, "Помывочная", "помывочная")
        self.draw_room_tk(0, half_h, width, height, "Комната отдыха", "отдых")

        self.apply_pending_data()

    def apply_pending_data(self):
        for name, value in self.pending_data.items():
            label = self.temperature_texts.get(name)
            if label:
                label.text = f"{name}: {value:.1f} °C"
        self.pending_data.clear()

    def draw_room_tk(self, x1, y1, x2, y2, title, room_type):
        y1_k = self.height - y2
        y2_k = self.height - y1
        with self.canvas:
            Color(0.94, 0.94, 0.94, 1)
            Rectangle(pos=(x1, y1_k), size=(x2-x1, y2-y1))
            Color(0, 0, 0, 1)
            Line(rectangle=(x1, y1_k, x2-x1, y2-y1), width=2)

        title_x = (x1 + x2) // 2
        title_y = y1 + 20
        title_y_k = self.height - title_y
        self.add_widget(Label(text=title, font_size=14, bold=True,
                              size_hint=(None, None), pos=(title_x, title_y_k)))

        if room_type == "парная":
            self.draw_parnaya_tk(x1, y1, x2, y2)
        elif room_type == "помывочная":
            self.draw_pomivochnaya_tk(x1, y1, x2, y2)
        elif room_type == "отдых":
            self.draw_otdih_tk(x1, y1, x2, y2)

    def draw_parnaya_tk(self, x1, y1, x2, y2):
        width = x2 - x1
        height = y2 - y1

        pech_w = width * 0.25
        pech_h = height * 0.3
        pech_x = x1 + width * 0.1
        pech_y = y2 - pech_h - height * 0.1

        pech_x_k = pech_x
        pech_y_k = self.height - (pech_y + pech_h)
        with self.canvas:
            Color(0.6, 0.3, 0, 1)
            Rectangle(pos=(pech_x_k, pech_y_k), size=(pech_w, pech_h))
            Color(0.5, 0.5, 0.5, 1)
            truba_w = pech_w * 0.3
            truba_h = pech_h * 0.2
            truba_x_k = pech_x + pech_w * 0.35
            truba_y_k = self.height - (pech_y - truba_h) - truba_h
            Rectangle(pos=(truba_x_k, truba_y_k), size=(truba_w, truba_h))

        gas_x = pech_x + pech_w/2 + 20
        gas_y = pech_y - 25
        gas_y_k = self.height - gas_y
        gas_label = Label(text="Температура дымовых газов: -- °C", font_size=10, size_hint=(None, None), color=(1,0,0,1))
        gas_label.pos = (gas_x, gas_y_k)
        self.add_widget(gas_label)
        self.temperature_texts["Температура дымовых газов"] = gas_label

        heat_x = pech_x + pech_w + 10
        heat_y = pech_y + pech_h/2
        heat_y_k = self.height - heat_y
        heat_label = Label(text="Температура в теплообменнике: -- °C", font_size=10, size_hint=(None, None), color=(1,0,0,1))
        heat_label.pos = (heat_x, heat_y_k)
        self.add_widget(heat_label)
        self.temperature_texts["Температура в теплообменнике"] = heat_label

        text_x = x1 + width * 0.6
        start_y = y1 + height * 0.2
        polok_y = start_y + 2 * height * 0.08
        polok_y_k = self.height - polok_y
        polok_label = Label(text="Температура полка: -- °C", font_size=10, size_hint=(None, None), color=(1,0,0,1))
        polok_label.pos = (text_x, polok_y_k)
        self.add_widget(polok_label)
        self.temperature_texts["Температура полка"] = polok_label

    def draw_pomivochnaya_tk(self, x1, y1, x2, y2):
        width = x2 - x1
        height = y2 - y1

        boiler_w = width * 0.4
        boiler_h = height * 0.2
        boiler_x = x1 + width * 0.05
        boiler_y = y1 + height * 0.05 + 50
        boiler_x_k = boiler_x
        boiler_y_k = self.height - (boiler_y + boiler_h)
        with self.canvas:
            Color(0.75, 0.75, 0.75, 1)
            Rectangle(pos=(boiler_x_k, boiler_y_k), size=(boiler_w, boiler_h))
            Ellipse(pos=(boiler_x_k, boiler_y_k - boiler_h*0.1), size=(boiler_w, boiler_h*0.1))
            Ellipse(pos=(boiler_x_k, boiler_y_k + boiler_h), size=(boiler_w, boiler_h*0.1))

        water_x = boiler_x + boiler_w + 10
        water_y = boiler_y + boiler_h / 2
        water_y_k = self.height - water_y
        water_label = Label(text="Температура воды: -- °C", font_size=10, size_hint=(None, None), color=(1,0,0,1))
        water_label.pos = (water_x, water_y_k)
        self.add_widget(water_label)
        self.temperature_texts["Температура воды"] = water_label

        top_x = x1 + width/2
        top_y = y1 + 40
        top_y_k = self.height - top_y
        top_label = Label(text="Температура вверху: -- °C", font_size=10, size_hint=(None, None), color=(1,0,0,1))
        top_label.pos = (top_x, top_y_k)
        self.add_widget(top_label)

        bottom_x = x1 + width/2
        bottom_y = y2 - 30
        bottom_y_k = self.height - bottom_y
        bottom_label = Label(text="Температура внизу: -- °C", font_size=10, size_hint=(None, None), color=(1,0,0,1))
        bottom_label.pos = (bottom_x, bottom_y_k)
        self.add_widget(bottom_label)

    def draw_otdih_tk(self, x1, y1, x2, y2):
        width = x2 - x1
        height = y2 - y1

        sofa_w = width * 0.4
        sofa_h = height * 0.3
        sofa_x = x1 + width * 0.3
        sofa_y = y2 - sofa_h - height * 0.1
        sofa_x_k = sofa_x
        sofa_y_k = self.height - (sofa_y + sofa_h)
        with self.canvas:
            Color(0, 0.5, 0, 1)
            Rectangle(pos=(sofa_x_k, sofa_y_k), size=(sofa_w, sofa_h))
            Color(0.5, 0.8, 0.5, 1)
            cushion_w = sofa_w * 0.25
            cushion_h = sofa_h * 0.4
            for i in range(3):
                cx = sofa_x + i * (sofa_w / 3) + (sofa_w / 3 - cushion_w) / 2
                cy = sofa_y + sofa_h - cushion_h - 5
                cy_k = self.height - (cy + cushion_h)
                Rectangle(pos=(cx, cy_k), size=(cushion_w, cushion_h))

        text_x = x1 + width * 0.1
        start_y = y1 + height * 0.2
        start_y_k = self.height - start_y
        up_label = Label(text="Температура вверху: -- °C", font_size=10, size_hint=(None, None), color=(1,0,0,1))
        up_label.pos = (text_x, start_y_k)
        self.add_widget(up_label)

        bottom_y = y2 - height * 0.2
        bottom_y_k = self.height - bottom_y
        down_label = Label(text="Температура внизу: -- °C", font_size=10, size_hint=(None, None), color=(1,0,0,1))
        down_label.pos = (text_x, bottom_y_k)
        self.add_widget(down_label)


class SensorDataApp(App):
    def build(self):
        self.thread_queue = queue.Queue()
        self.stop_thread_event = threading.Event()

        self.modbus_thread = threading.Thread(
            target=run_async_manager,
            args=(self.thread_queue, self.stop_thread_event),
            daemon=True
        )
        self.modbus_thread.start()
        logging.info("Поток Modbus запущен")

        self.tab_panel = TabbedPanel()
        self.tab_panel.do_default_tab = False

        tab_banya = TabbedPanelItem(text="Баня")
        self.building_plan = BuildingPlan()
        tab_banya.add_widget(self.building_plan)
        self.tab_panel.add_widget(tab_banya)

        tab_podval = TabbedPanelItem(text="Подвал")
        tab_podval.add_widget(Label(text="Подвал пуст", font_size=20, size_hint=(1,1)))
        self.tab_panel.add_widget(tab_podval)

        Clock.schedule_interval(self.process_queue, 0.1)

        return self.tab_panel

    def process_queue(self, dt):
        try:
            while True:
                typ, name, value = self.thread_queue.get_nowait()
                if typ == 'data':
                    label = self.building_plan.temperature_texts.get(name)
                    if label:
                        label.text = f"{name}: {value:.1f} °C"
                    else:
                        self.building_plan.pending_data[name] = value
                else:
                    logging.error(f"Ошибка {name}: {value}")
        except queue.Empty:
            pass

    def on_stop(self):
        logging.info("Закрытие приложения...")
        self.stop_thread_event.set()
        if self.modbus_thread.is_alive():
            self.modbus_thread.join(timeout=2.0)
        logging.info("Приложение закрыто")


if __name__ == "__main__":
    SensorDataApp().run()
