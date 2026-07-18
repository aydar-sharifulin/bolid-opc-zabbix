from opcua import Client
from bolid.state_map import STATE_MAP

import json
import os
import re
import subprocess
import time

start_time = time.perf_counter()

OPC_URL = "opc.tcp://192.168.10.100:4848"
STATE_FILE = "/home/sharifay/last_states.json"
ZABBIX_SERVER = "127.0.0.1"
ZABBIX_HOST = "Test OPC"



def extract_id(name: str):
    m = re.match(r"^[A-Za-z]+_(\d+)_", name)
    if m:
        return m.group(1)
    return None


def extract_path(path: str):
    parts = path.split("/")
    for part in parts:
        if part.startswith("Section_"):
            x = part.split("_", 2)
            if len(x) >= 3:
                return x[2]
            return part
    return path


def detect_object_type(name: str):
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


def decode_state(obj_type: str, code: int) -> str:
    return STATE_MAP.get(obj_type, {}).get(code, f"Неизвестный код: {code}")


def find_state_nodes(node, path=""):
    results = []

    try:
        children = node.get_children()
    except Exception:
        return results

    current_name = node.get_browse_name().Name
    current_path = f"{path}/{current_name}" if path else current_name
    obj_type = detect_object_type(current_name)

    if obj_type:
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
                pass

        if state_node:
            results.append((node, obj_type, name_node, state_node, current_path))

    for child in children:
        results.extend(find_state_nodes(child, current_path))

    return results


def load_previous_states():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_current_states(states):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(states, f, ensure_ascii=False, indent=2)


def zabbix_send(key: str, value):
    result = subprocess.run(
        [
            "zabbix_sender",
            "-z", ZABBIX_SERVER,
            "-s", ZABBIX_HOST,
            "-k", key,
            "-o", str(value),
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


def main():
    client = Client(OPC_URL)
    connected = False
    previous_states = load_previous_states()
    current_states = {}
    ok_count = 0
    bad_count = 0

    try:
        client.connect()
        connected = True
        print(f"Подключено к {OPC_URL}")

        objects = client.get_objects_node()
        target = None

        for child in objects.get_children():
            try:
                if child.get_browse_name().Name == "WorkPlace_saml1":
                    target = child
                    break
            except Exception:
                pass

        if not target:
            print("WorkPlace_saml1 не найден")
            return

        found = find_state_nodes(target)

        for obj_node, obj_type, name_node, state_node, path in found:
            try:
                object_name = obj_node.get_browse_name().Name
                obj_id = extract_id(object_name)
                if not obj_id:
                    continue

                display_name = name_node.get_value() if name_node else object_name
                short_path = extract_path(path)

                dv = state_node.get_data_value()
                state_code = int(dv.Value.Value)
                ts = dv.SourceTimestamp or dv.ServerTimestamp
                state_text = decode_state(obj_type, state_code)

                state_key = f"{obj_type}:{obj_id}"
                current_states[state_key] = state_code
                prev_code = previous_states.get(state_key)

                zabbix_send(f"bolid.state_code[{obj_type},{obj_id}]", state_code)
                zabbix_send(f"bolid.state_text[{obj_type},{obj_id}]", state_text)
                zabbix_send(f"bolid.name[{obj_type},{obj_id}]", display_name)
                zabbix_send(f"bolid.path[{obj_type},{obj_id}]", short_path)

                ok_count += 1

                if prev_code is None:
                    print(f"[INIT] [{obj_type}] {display_name}: {state_code} ({state_text}) @ {ts}")
                elif prev_code != state_code:
                    prev_text = decode_state(obj_type, int(prev_code))
                    print(
                        f"[CHANGE] [{obj_type}] {display_name}: "
                        f"{prev_code} ({prev_text}) -> {state_code} ({state_text}) @ {ts}"
                    )

            except Exception as e:
                err = str(e)

                if "The operation failed.(Bad)" in err:
                    bad_count += 1
                else:
                    print(f"Ошибка чтения {path}: {e}")

    finally:
        if connected:
            client.disconnect()
            print("Соединение закрыто.")

        save_current_states(current_states)

        elapsed = time.perf_counter() - start_time
        print(f"Время выполнения: {elapsed:.3f} сек")
        print(f"Успешно прочитано: {ok_count}")
        print(f"Недоступно (Bad): {bad_count}")


if __name__ == "__main__":
    main()
