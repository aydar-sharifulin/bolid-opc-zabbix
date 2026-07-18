import os
from dotenv import load_dotenv

load_dotenv()


def getenv(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is not set")
    return value


OPC_URL = getenv("OPC_URL")
OPC_ROOT_NODE = getenv("OPC_ROOT_NODE")

ZABBIX_SERVER = getenv("ZABBIX_SERVER")
ZABBIX_HOST = getenv("ZABBIX_HOST")

DISCOVERY_KEY = os.getenv("DISCOVERY_KEY", "bolid.discovery")
STATE_FILE = os.getenv("STATE_FILE", "last_states.json")
