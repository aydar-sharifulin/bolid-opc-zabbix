from opcua import Client

import json
import os
import re
import subprocess
import tempfile
from dotenv import load_dotenv

load_dotenv()

OPC_URL = os.getenv("OPC_URL")
OPC_ROOT_NODE = os.getenv("OPC_ROOT_NODE")

ZABBIX_SERVER = os.getenv("ZABBIX_SERVER")
ZABBIX_HOST = os.getenv("ZABBIX_HOST")

DISCOVERY_KEY = os.getenv("DISCOVERY_KEY", "bolid.discovery")

def zabbix_send_discovery(key: str, value: str) -> bool:
    """
    Передаёт JSON Low-Level Discovery в Zabbix через временный файл.

    Использование входного файла позволяет корректно передавать JSON,
    содержащий пробелы, кавычки и символы Unicode.
    """
    temp_name = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            encoding="utf-8",
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(f'"{ZABBIX_HOST}" {key} {value}\n')

        result = subprocess.run(
            [
                "zabbix_sender",
                "-z",
                ZABBIX_SERVER,
                "-i",
                temp_name,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        if result.stdout:
            print(result.stdout.strip())

        if result.stderr:
            print(result.stderr.strip())

        if result.returncode != 0:
            print(
                f"[ZABBIX ERROR] zabbix_sender завершился "
                f"с кодом {result.returncode}"
            )
            return False

        return True

    except FileNotFoundError:
        print(
            "[ZABBIX ERROR] Команда zabbix_sender не найдена. "
            "Установите пакет zabbix-sender."
        )
        return False

    except OSError as error:
        print(f"[ZABBIX ERROR] Не удалось запустить zabbix_sender: {error}")
        return False

    finally:
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError as error:
                print(
                    f"[WARNING] Не удалось удалить временный файл "
                    f"{temp_name}: {error}"
                )


def detect_object_type(name: str):
    """
    Определяет тип объекта БОЛИД по имени узла OPC UA.
    """
    if name.startswith("Input_"):
        return "Input_"

    if name.startswith("Output_"):
        return "Output_"

    if name.startswith("Section_"):
        return "section_"

    if name.startswith("GroupSection_"):
        return "GroupSection_"

    if name.startswith("Reader_"):
        return "Reader_"

    if name.startswith("Door_"):
        return "Door_"

    if name.startswith("Camera_"):
        return "Camera_"

    if name.startswith("Device_"):
        return "Device_"

    return None


def extract_id(name: str):
    """
    Извлекает цифровой идентификатор из имени объекта.

    Пример:
        Input_123_SmokeDetector -> 123
    """
    match = re.match(r"^[A-Za-z]+_(\d+)_", name)

    if match:
        return match.group(1)

    return None


def extract_path(path: str):
    """
    Возвращает сокращённый путь объекта.

    Если в пути присутствует Section_, возвращается название секции.
    В противном случае возвращается полный путь.
    """
    parts = path.split("/")

    for part in parts:
        if part.startswith("Section_"):
            section_parts = part.split("_", 2)

            if len(section_parts) >= 3:
                return section_parts[2]

            return part

    return path


def find_state_nodes(node, path=""):
    """
    Рекурсивно обходит дерево OPC UA и находит поддерживаемые объекты,
    содержащие переменную State.
    """
    results = []

    try:
        children = node.get_children()
        current_name = node.get_browse_name().Name
    except Exception:
        return results

    current_path = f"{path}/{current_name}" if path else current_name
    object_type = detect_object_type(current_name)

    if object_type:
        state_node = None
        name_node = None

        for child in children:
            try:
                child_name = child.get_browse_name().Name

                if child_name == "State":
                    state_node = child
                elif child_name == "Name":
                    name_node = child

            except Exception:
                continue

        if state_node is not None:
            results.append(
                (
                    node,
                    object_type,
                    name_node,
                    state_node,
                    current_path,
                )
            )

    for child in children:
        results.extend(find_state_nodes(child, current_path))

    return results


def find_root_node(client: Client):
    """
    Ищет рабочий корневой узел Орион Про среди объектов OPC UA.
    """
    objects_node = client.get_objects_node()

    for child in objects_node.get_children():
        try:
            if child.get_browse_name().Name == OPC_ROOT_NODE:
                return child
        except Exception:
            continue

    return None


def build_discovery_data(found_nodes):
    """
    Формирует список объектов для Zabbix Low-Level Discovery.
    """
    discovery_data = []
    skipped_count = 0

    for object_node, object_type, name_node, state_node, path in found_nodes:
        try:
            object_name = object_node.get_browse_name().Name
            object_id = extract_id(object_name)

            if not object_id:
                skipped_count += 1
                continue

            if name_node is not None:
                try:
                    display_name = name_node.get_value()
                except Exception:
                    display_name = object_name
            else:
                display_name = object_name

            short_path = extract_path(path)

            discovery_data.append(
                {
                    "{#ID}": str(object_id),
                    "{#NAME}": str(display_name),
                    "{#PATH}": str(short_path),
                    "{#TYPE}": object_type,
                }
            )

        except Exception as error:
            skipped_count += 1
            print(f"[WARNING] Не удалось обработать объект {path}: {error}")

    return discovery_data, skipped_count


def main():
    client = Client(OPC_URL)
    connected = False

    try:
        client.connect()
        connected = True
        print(f"Подключено к {OPC_URL}")

        target_node = find_root_node(client)

        if target_node is None:
            print(f'Узел "{OPC_ROOT_NODE}" не найден')
            return

        found_nodes = find_state_nodes(target_node)

        print(f"Обнаружено OPC UA-объектов со State: {len(found_nodes)}")

        discovery_data, skipped_count = build_discovery_data(found_nodes)

        payload = json.dumps(
            {"data": discovery_data},
            ensure_ascii=False,
            separators=(",", ":"),
        )

        print(payload)

        sent = zabbix_send_discovery(DISCOVERY_KEY, payload)

        print(f"Передано объектов в discovery: {len(discovery_data)}")
        print(f"Пропущено объектов: {skipped_count}")

        if sent:
            print("Discovery успешно передан в Zabbix.")
        else:
            print("Discovery сформирован, но не передан в Zabbix.")

    except Exception as error:
        print(f"[ERROR] Ошибка выполнения discovery: {error}")

    finally:
        if connected:
            try:
                client.disconnect()
                print("Соединение закрыто.")
            except Exception as error:
                print(f"[WARNING] Ошибка отключения от OPC UA: {error}")


if __name__ == "__main__":
    main()
