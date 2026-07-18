from typing import Optional

import json
import os
import re
import subprocess
import tempfile
import time

from opcua import Client

from bolid.config import (
    OPC_ROOT_NODE,
    OPC_URL,
    STATE_FILE,
    ZABBIX_HOST,
    ZABBIX_SERVER,
)
from bolid.state_map import STATE_MAP


SUPPORTED_OBJECT_TYPES = (
    "Input_",
    "Output_",
    "Section_",
    "GroupSection_",
    "Reader_",
    "Door_",
    "Camera_",
    "Device_",
)


def extract_id(name: str) -> Optional[str]:
    """
    Извлекает цифровой идентификатор из имени объекта.

    Примеры:
        Input_123_SmokeDetector -> 123
        Input_123               -> 123
        GroupSection_45_Main    -> 45
    """
    match = re.match(r"^[A-Za-z]+_(\d+)(?:_|$)", name)

    if match:
        return match.group(1)

    return None


def extract_path(path: str) -> str:
    """
    Возвращает сокращённый путь объекта.

    Если в пути присутствует Section_, возвращает название секции
    без префикса и идентификатора. В противном случае возвращает
    полный путь.
    """
    parts = path.split("/")

    for part in parts:
        if part.startswith("Section_"):
            section_parts = part.split("_", 2)

            if len(section_parts) >= 3:
                return section_parts[2]

            return part

    return path


def detect_object_type(name: str) -> Optional[str]:
    """
    Определяет тип объекта БОЛИД по имени узла OPC UA.
    """
    for object_type in SUPPORTED_OBJECT_TYPES:
        if name.startswith(object_type):
            return object_type

    return None


def decode_state(object_type: str, code: int) -> str:
    """
    Преобразует числовой код состояния в текстовое описание.
    """
    return STATE_MAP.get(object_type, {}).get(
        code,
        f"Неизвестный код: {code}",
    )


def find_state_nodes(node, path: str = "") -> list:
    """
    Рекурсивно обходит дерево OPC UA и находит поддерживаемые объекты,
    содержащие дочернюю переменную State.

    Возвращает список кортежей:

        (
            object_node,
            object_type,
            name_node,
            state_node,
            object_path,
        )
    """
    results = []

    try:
        children = node.get_children()
        current_name = node.get_browse_name().Name
    except Exception as error:
        print(f"[WARNING] Не удалось прочитать узел OPC UA: {error}")
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
    Ищет настроенный корневой узел Орион Про среди объектов OPC UA.
    """
    objects_node = client.get_objects_node()

    try:
        children = objects_node.get_children()
    except Exception as error:
        print(f"[ERROR] Не удалось получить корневые объекты OPC UA: {error}")
        return None

    for child in children:
        try:
            if child.get_browse_name().Name == OPC_ROOT_NODE:
                return child
        except Exception:
            continue

    return None


def load_previous_states() -> dict:
    """
    Загружает состояния объектов, сохранённые после предыдущего опроса.
    """
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)

        if isinstance(data, dict):
            return data

        print(
            f"[WARNING] Файл состояний {STATE_FILE} "
            "не содержит JSON-объект."
        )

    except (OSError, json.JSONDecodeError) as error:
        print(
            f"[WARNING] Не удалось загрузить файл состояний "
            f"{STATE_FILE}: {error}"
        )

    return {}


def save_current_states(states: dict) -> bool:
    """
    Атомарно сохраняет текущие состояния объектов в JSON-файл.
    """
    state_directory = os.path.dirname(os.path.abspath(STATE_FILE))

    try:
        os.makedirs(state_directory, exist_ok=True)

        temp_name = None

        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=state_directory,
            encoding="utf-8",
        ) as temp_file:
            temp_name = temp_file.name

            json.dump(
                states,
                temp_file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )

            temp_file.write("\n")

        os.replace(temp_name, STATE_FILE)
        return True

    except OSError as error:
        print(
            f"[ERROR] Не удалось сохранить файл состояний "
            f"{STATE_FILE}: {error}"
        )

        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass

        return False


def zabbix_send(key: str, value) -> bool:
    """
    Передаёт одно значение в Zabbix через zabbix_sender.
    """
    try:
        result = subprocess.run(
            [
                "zabbix_sender",
                "-z",
                ZABBIX_SERVER,
                "-s",
                ZABBIX_HOST,
                "-k",
                key,
                "-o",
                str(value),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"[ZABBIX ERROR] key={key} value={value}")

            if result.stdout:
                print(result.stdout.strip())

            if result.stderr:
                print(result.stderr.strip())

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


def read_display_name(name_node, object_name: str) -> str:
    """
    Читает отображаемое имя объекта.

    Если переменная Name отсутствует или недоступна, возвращает
    техническое имя OPC UA-узла.
    """
    if name_node is None:
        return object_name

    try:
        value = name_node.get_value()

        if value is not None and str(value).strip():
            return str(value)

    except Exception as error:
        print(
            f"[WARNING] Не удалось прочитать Name объекта "
            f"{object_name}: {error}"
        )

    return object_name


def is_bad_status_error(error: Exception) -> bool:
    """
    Проверяет, относится ли ошибка к недоступному OPC UA-значению.
    """
    error_text = str(error)

    bad_status_markers = (
        "The operation failed.(Bad)",
        "BadNoCommunication",
        "BadDeviceFailure",
        "BadNotConnected",
        "BadOutOfService",
        "BadWaitingForInitialData",
        "BadNodeIdUnknown",
    )

    return any(marker in error_text for marker in bad_status_markers)


def main() -> None:
    """
    Подключается к OPC UA, читает состояния объектов и передаёт
    значения в Zabbix.
    """
    start_time = time.perf_counter()

    client = Client(OPC_URL)
    connected = False

    previous_states = load_previous_states()
    current_states = {}

    discovered_count = 0
    ok_count = 0
    bad_count = 0
    skipped_count = 0
    changed_count = 0
    zabbix_error_count = 0

    try:
        print(f"Подключение к OPC UA: {OPC_URL}")

        client.connect()
        connected = True

        print("Соединение с OPC UA установлено.")

        target_node = find_root_node(client)

        if target_node is None:
            print(f'[ERROR] Корневой узел "{OPC_ROOT_NODE}" не найден.')
            return

        print(f'Корневой узел найден: "{OPC_ROOT_NODE}"')

        found_nodes = find_state_nodes(target_node)
        discovered_count = len(found_nodes)

        print(
            "Обнаружено OPC UA-объектов с переменной State: "
            f"{discovered_count}"
        )

        for (
            object_node,
            object_type,
            name_node,
            state_node,
            path,
        ) in found_nodes:
            try:
                object_name = object_node.get_browse_name().Name
                object_id = extract_id(object_name)

                if object_id is None:
                    skipped_count += 1
                    print(
                        "[WARNING] Не удалось определить ID объекта: "
                        f"{object_name}"
                    )
                    continue

                display_name = read_display_name(
                    name_node=name_node,
                    object_name=object_name,
                )

                short_path = extract_path(path)

                data_value = state_node.get_data_value()
                raw_state_code = data_value.Value.Value

                if raw_state_code is None:
                    bad_count += 1
                    print(
                        f"[WARNING] Пустое состояние объекта: {path}"
                    )
                    continue

                state_code = int(raw_state_code)
                state_text = decode_state(object_type, state_code)

                timestamp = (
                    data_value.SourceTimestamp
                    or data_value.ServerTimestamp
                )

                state_key = f"{object_type}:{object_id}"
                current_states[state_key] = state_code

                previous_code = previous_states.get(state_key)

                metrics = (
                    (
                        f"bolid.state_code[{object_type},{object_id}]",
                        state_code,
                    ),
                    (
                        f"bolid.state_text[{object_type},{object_id}]",
                        state_text,
                    ),
                    (
                        f"bolid.name[{object_type},{object_id}]",
                        display_name,
                    ),
                    (
                        f"bolid.path[{object_type},{object_id}]",
                        short_path,
                    ),
                )

                for key, value in metrics:
                    if not zabbix_send(key, value):
                        zabbix_error_count += 1

                ok_count += 1

                if previous_code is None:
                    print(
                        f"[INIT] [{object_type}] {display_name}: "
                        f"{state_code} ({state_text}) @ {timestamp}"
                    )

                elif int(previous_code) != state_code:
                    previous_text = decode_state(
                        object_type,
                        int(previous_code),
                    )

                    changed_count += 1

                    print(
                        f"[CHANGE] [{object_type}] {display_name}: "
                        f"{previous_code} ({previous_text}) -> "
                        f"{state_code} ({state_text}) @ {timestamp}"
                    )

            except Exception as error:
                if is_bad_status_error(error):
                    bad_count += 1
                else:
                    print(f"[ERROR] Ошибка чтения {path}: {error}")

    except Exception as error:
        print(f"[ERROR] Ошибка выполнения опроса: {error}")

    finally:
        if connected:
            try:
                client.disconnect()
                print("Соединение с OPC UA закрыто.")

            except Exception as error:
                print(
                    "[WARNING] Ошибка отключения от OPC UA: "
                    f"{error}"
                )

        if current_states:
            save_current_states(current_states)
        else:
            print(
                "[WARNING] Текущие состояния не сохранены: "
                "ни один объект не был успешно прочитан."
            )

        elapsed = time.perf_counter() - start_time

        print()
        print("Итоги опроса:")
        print(f"Обнаружено объектов: {discovered_count}")
        print(f"Успешно прочитано: {ok_count}")
        print(f"Недоступно или Bad: {bad_count}")
        print(f"Пропущено объектов: {skipped_count}")
        print(f"Изменений состояний: {changed_count}")
        print(f"Ошибок передачи в Zabbix: {zabbix_error_count}")
        print(f"Время выполнения: {elapsed:.3f} сек")


if __name__ == "__main__":
    main()
