from typing import Optional

import json
import re
import subprocess

from opcua import Client

from bolid.config import (
    DISCOVERY_KEY,
    OPC_ROOT_NODE,
    OPC_URL,
    ZABBIX_HOST,
    ZABBIX_SERVER,
)


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


def zabbix_send_discovery(key: str, value: str) -> bool:
    """
    Передаёт JSON Low-Level Discovery в Zabbix через zabbix_sender.

    Значение передаётся отдельным аргументом командной строки без shell,
    поэтому JSON с пробелами, кавычками и символами Unicode обрабатывается
    корректно.
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
                value,
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
                "[ZABBIX ERROR] zabbix_sender завершился "
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


def detect_object_type(name: str) -> Optional[str]:
    """
    Определяет тип объекта БОЛИД по имени узла OPC UA.

    Возвращает имя поддерживаемого типа вместе с завершающим
    символом подчёркивания либо None.
    """
    for object_type in SUPPORTED_OBJECT_TYPES:
        if name.startswith(object_type):
            return object_type

    return None


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

    Если в пути присутствует узел Section_, возвращает его название
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

    Возвращает найденный узел либо None.
    """
    objects_node = client.get_objects_node()

    try:
        children = objects_node.get_children()
    except Exception as error:
        print(f"[ERROR] Не удалось получить корневые объекты OPC UA: {error}")
        return None

    for child in children:
        try:
            child_name = child.get_browse_name().Name

            if child_name == OPC_ROOT_NODE:
                return child

        except Exception:
            continue

    return None


def build_discovery_data(found_nodes: list) -> tuple:
    """
    Формирует список объектов для Zabbix Low-Level Discovery.

    Возвращает:

        discovery_data — список LLD-объектов;
        skipped_count — количество пропущенных объектов.
    """
    discovery_data = []
    skipped_count = 0

    for (
        object_node,
        object_type,
        name_node,
        _state_node,
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

            display_name = object_name

            if name_node is not None:
                try:
                    name_value = name_node.get_value()

                    if name_value is not None and str(name_value).strip():
                        display_name = str(name_value)

                except Exception as error:
                    print(
                        "[WARNING] Не удалось прочитать Name объекта "
                        f"{object_name}: {error}"
                    )

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


def main() -> None:
    """
    Подключается к OPC UA, выполняет обнаружение объектов и передаёт
    результат Low-Level Discovery в Zabbix.
    """
    client = Client(OPC_URL)
    connected = False

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

        print(
            "Обнаружено OPC UA-объектов с переменной State: "
            f"{len(found_nodes)}"
        )

        discovery_data, skipped_count = build_discovery_data(found_nodes)

        payload = json.dumps(
            {"data": discovery_data},
            ensure_ascii=False,
            separators=(",", ":"),
        )

        print("Сформированный Low-Level Discovery JSON:")
        print(payload)

        sent = zabbix_send_discovery(
            key=DISCOVERY_KEY,
            value=payload,
        )

        print(f"Подготовлено объектов для discovery: {len(discovery_data)}")
        print(f"Пропущено объектов: {skipped_count}")

        if sent:
            print("Discovery успешно передан в Zabbix.")
        else:
            print(
                "[ERROR] Discovery сформирован, "
                "но не передан в Zabbix."
            )

    except Exception as error:
        print(f"[ERROR] Ошибка выполнения discovery: {error}")

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


if __name__ == "__main__":
    main()
