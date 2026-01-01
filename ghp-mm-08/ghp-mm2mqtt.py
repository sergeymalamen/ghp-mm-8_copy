#!/usr/bin/env python3
"""
Переписанный ghp-mm2mqtt.py
- Использует MQTT v3.1.1 (устраняет DeprecationWarning)
- Авто-переподключение к MQTT
- Повторная попытка открытия последовательного порта (если не удалось)
- Явные логи (print + logger)
- Без рекурсивного разбора пакетов (итеративный цикл)
- writemsg как bytes
"""

import sys
import os
import struct
import json
import logging
import time
import serial
import socket
import paho.mqtt.client as mqtt

from ghp_config import *  # ожидается, что тут определены MQTT_TOPIC_PREFIX, SERIAL_PORT и т.п.

# ----- Логирование -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
_logger = logging.getLogger("ghp-mm2mqtt")

print("🚀 Скрипт стартует...")
_logger.info("Запуск ghp-mm2mqtt")

# ----- Сетевые таймауты -----
socket.setdefaulttimeout(5)

# ----- Modbus CRC16 -----
def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if (crc & 0x0001) != 0:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc

def verify_modbus_crc(data: bytes) -> bool:
    if len(data) < 4:
        return False
    received_crc = struct.unpack('<H', data[-2:])[0]
    calculated_crc = modbus_crc16(data[:-2])
    _logger.debug(f"verify_modbus_crc: received={received_crc} calculated={calculated_crc}")
    return received_crc == calculated_crc

# ----- MQTT Publish -----
def publish(slave: int, op: int, addr: int, data):
    try:
        data_json = json.dumps(data)
    except Exception:
        # Если data — tuple of ints (от struct.unpack), преобразуем явно
        try:
            data_json = json.dumps(list(data))
        except Exception as e:
            _logger.error(f"Ошибка сериализации данных для публикации: {e}")
            return

    retain = 2100 <= addr < 2200
    mqtt_topic = f"{MQTT_TOPIC_PREFIX}/{op}/{slave}/{addr}"
    _logger.info(f"PUB {mqtt_topic}: {data_json} (retain={retain})")
    try:
        mqtt_client.publish(mqtt_topic, data_json, retain=retain)
    except Exception as e:
        _logger.error(f"Ошибка publish: {e}")

# ----- Разбор Modbus пакетов (итеративно) -----
def decode_modbus_buffer(buffer: bytearray):
    """
    Обрабатывает buffer по пакетам Modbus. Возвращает оставшийся неразобранный buffer (bytearray).
    """
    idx = 0
    buflen = len(buffer)
    # Ищем стартовый байт 0xF0 (240)
    while True:
        buflen = len(buffer)
        if buflen < 8:
            break

        index = buffer.find(0xF0)  # ищем байт 240 (0xF0)
        if index < 0:
            # нет стартового байта, обрезаем весь буфер кроме последних 7 байт (на случай обрезанных пакетов)
            if buflen > 7:
                buffer = buffer[-7:]
            break
        if index > 0:
            # отбрасываем всё до стартового байта
            buffer = buffer[index:]
            buflen = len(buffer)
            if buflen < 8:
                break

        # Теперь buffer[0] == 0xF0
        func = buffer[1]
        if func == 3:
            # possible read response or write response — минимальный размер 8
            if buflen >= 8 and verify_modbus_crc(buffer[0:8]):
                # readAddr находится в байтах 2:4 (big endian signed short)
                try:
                    readAddr = struct.unpack('>h', buffer[2:4])[0]
                except Exception as e:
                    _logger.error(f"Ошибка распаковки readAddr: {e}")
                    # отбрасываем стартовый байт и продолжаем
                    buffer = buffer[1:]
                    continue
                # Это короткий пакет (8 bytes) — возможно подтверждение
                # Отбрасываем первые 8 байт
                buffer = buffer[8:]
                continue
            else:
                # Возможно это пакет с данными: длина = buffer[2] + 5
                if buflen >= 3:
                    psize = buffer[2] + 5
                    if buflen >= psize and verify_modbus_crc(buffer[0:psize]):
                        # распаковываем данные: numshorts = (psize-5)/2
                        numshorts = int((psize - 5) / 2)
                        try:
                            payload = struct.unpack(f'>{numshorts}h', buffer[3:psize - 2])
                            publish(buffer[0], 3, readAddr if 'readAddr' in locals() else 0, payload)
                        except Exception as e:
                            _logger.error(f"Ошибка распаковки payload func=3: {e}")
                        buffer = buffer[psize:]
                        continue
                    else:
                        # не хватает байт или CRC не совпадает — сдвигаемся
                        buffer = buffer[1:]
                        continue
                else:
                    break

        elif func == 16:
            # Write multiple registers
            if buflen >= 7:
                # buffer[6] содержит число байт (N), packet size = N + 9
                psize = buffer[6] + 9
                if buflen >= psize and verify_modbus_crc(buffer[0:psize]):
                    try:
                        readAddr = struct.unpack('>h', buffer[2:4])[0]
                        numshorts = int((psize - 9) / 2)
                        payload = struct.unpack(f">{numshorts}h", buffer[7:psize - 2])
                        publish(buffer[0], 10, readAddr, payload)
                    except Exception as e:
                        _logger.error(f"Ошибка распаковки payload func=16: {e}")
                    buffer = buffer[psize:]
                    continue
                else:
                    buffer = buffer[1:]
                    continue
            else:
                break
        else:
            # Неизвестная функция — сдвиг на 1 байт
            buffer = buffer[1:]
            continue

    return buffer

# ----- Обработчики MQTT -----
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        _logger.info("MQTT: подключение успешно (rc=0)")
        # Подписываемся на set топики
        try:
            client.subscribe(MQTT_TOPIC_PREFIX + "/set/#")
            _logger.info(f"MQTT: подписка на {MQTT_TOPIC_PREFIX}/set/#")
        except Exception as e:
            _logger.error(f"MQTT: ошибка подписки: {e}")
    else:
        _logger.warning(f"MQTT: подключение вернуло код rc={rc}")

def on_message(client, userdata, msg):
    global writemsg_bytes
    _logger.info(f"MQTT received: topic={msg.topic} payload={msg.payload}")
    try:
        # ожидаем структуру: <prefix>/<op>/<slave>/<addr>
        parts = msg.topic.split('/')
        # проверка
        if len(parts) < 4:
            _logger.warning(f"Некорректный топик для записи: {msg.topic}")
            return
        slave = int(parts[2])
        addr = int(parts[3])
        # payload может быть bytes — декодируем в int
        payload_str = msg.payload.decode('utf-8').strip()
        val = int(payload_str)
        # Разрешаем запись только в безопасный диапазон 2000-2006
        if 2000 <= addr <= 2006:
            # Формируем пакет: >BBhh  (как в твоём оригинале)
            # Предполагается: slave (1 byte), function=6 (short write?), addr (short), value (short)
            newm = struct.pack(">BBhh", slave, 6, addr, val)
            writemsg_bytes = newm
            _logger.info(f"Подготовлено сообщение на запись: {writemsg_bytes.hex()}")
        else:
            _logger.error(f"Write request outside safe range(0x2000-0x2006): {addr}")
    except ValueError:
        _logger.error(f"Невозможно конвертировать payload в int: {msg.payload}")
    except Exception as e:
        _logger.error(f"Ошибка в on_message: {e}")

# ----- MQTT: подключение с повтором -----
def connect_mqtt_with_retry(broker, port, user=None, password=None, keepalive=60, retry_delay=5):
    client = mqtt.Client(protocol=mqtt.MQTTv311)
    if user is not None:
        client.username_pw_set(user, password)
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            _logger.info(f"Попытка подключения к MQTT {broker}:{port} ...")
            client.connect(broker, port, keepalive)
            _logger.info("MQTT: connect() вернул управление")
            return client
        except Exception as e:
            _logger.error(f"MQTT: ошибка подключения: {e}")
            _logger.info(f"Повтор через {retry_delay} сек...")
            time.sleep(retry_delay)

# ----- Открытие последовательного порта с повторами -----
def open_serial_with_retry(port_name, baudrate=9600, timeout=0, retry_delay=5):
    while True:
        try:
            ser = serial.Serial(
                port=port_name,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout
            )
            # Проверим, открыт ли порт
            if ser.is_open:
                _logger.info(f"Serial port {ser.port} opened successfully")
                ser.reset_input_buffer()
                return ser
            else:
                _logger.error(f"Serial port {port_name} failed to open (is_open==False)")
        except serial.SerialException as e:
            _logger.error(f"Ошибка открытия serial {port_name}: {e}")
        except PermissionError as e:
            _logger.error(f"PermissionError opening serial {port_name}: {e}")
        except Exception as e:
            _logger.error(f"Неожиданная ошибка открытия serial {port_name}: {e}")

        _logger.info(f"Повтор открытия serial через {retry_delay} сек...")
        time.sleep(retry_delay)


# ----- Глобальные переменные -----
writemsg_bytes = b''  # теперь — байтовая переменная
buffer = bytearray()
readAddr = 0

# ----- Основной запуск -----
if __name__ == "__main__":
    # берем параметры из ghp_config или хардкодим
    MQTT_BROKER = globals().get("MQTT_BROKER", "192.168.1.220")
    MQTT_PORT = globals().get("MQTT_PORT", 1883)
    MQTT_USER = globals().get("MQTT_USER", "celiv")
    MQTT_PASS = globals().get("MQTT_PASS", "230960")
    SERIAL_PORT = globals().get("SERIAL_PORT", "/dev/ttyUSB0")
    MQTT_TOPIC_PREFIX = globals().get("MQTT_TOPIC_PREFIX", "heatpump")

    _logger.info(f"Config: broker={MQTT_BROKER}:{MQTT_PORT} serial={SERIAL_PORT} topic_prefix={MQTT_TOPIC_PREFIX}")

    # Подключаемся к MQTT
    mqtt_client = connect_mqtt_with_retry(MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS)
    mqtt_client.loop_start()
    _logger.info("MQTT loop started")

    # Открываем serial (будет повторять попытки при ошибке)
    ser = open_serial_with_retry(SERIAL_PORT, baudrate=9600, timeout=0)

    print(f"✅ Последовательный порт {ser.port} открыт успешно!")
    print("🚀 Скрипт запущен. Ожидаю данные от порта...")

    try:
        while True:
            # Читаем доступные данные
            try:
                # читаем 1 байт, затем всё, что уже пришло
                data = ser.read(1)
                if ser.in_waiting:
                    data += ser.read(ser.in_waiting)
            except Exception as e:
                _logger.error(f"Ошибка чтения serial: {e}")
                data = b''

            if data:
                _logger.debug(f"Read {len(data)} bytes from serial")
                buffer += data
                # Обрабатываем буфер (функция возвращает остаток)
                buffer = decode_modbus_buffer(buffer)

                # Если имеется сообщение для записи, отправим его в порт (и добавим CRC)
                if len(writemsg_bytes) > 0:
                    try:
                        # добавляем CRC16 (little-endian)
                        crc = modbus_crc16(writemsg_bytes)
                        to_write = writemsg_bytes + crc.to_bytes(2, 'little')
                        _logger.info(f"WRITE -> {to_write.hex()}")
                        ser.write(to_write)
                    except Exception as e:
                        _logger.error(f"Ошибка записи в serial: {e}")
                    finally:
                        writemsg_bytes = b''
            else:
                _logger.debug("No data received from serial (timeout).")

            # Небольшая пауза, чтобы не засирать CPU
            time.sleep(0.25)

    except KeyboardInterrupt:
        print("🛑 Прерывание: выход из программы...")
        _logger.info("Exiting by KeyboardInterrupt")

    except Exception as e:
        _logger.exception(f"Необработанное исключение в основном цикле: {e}")

    finally:
        _logger.info("Остановка: закрываем ресурсы")
        try:
            if 'ser' in locals() and ser and ser.is_open:
                ser.close()
                _logger.info("Serial port closed")
        except Exception as e:
            _logger.error(f"Ошибка при закрытии serial: {e}")
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
            _logger.info("MQTT disconnected")
        except Exception as e:
            _logger.error(f"Ошибка при закрытии MQTT: {e}")

        print("🔌 Порт и MQTT-соединение закрыты.")
