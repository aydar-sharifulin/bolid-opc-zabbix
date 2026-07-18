from dotenv import load_dotenv
import os

load_dotenv()

OPC_URL = os.getenv("OPC_URL")
OPC_ROOT_NODE = os.getenv("OPC_ROOT_NODE")

ZABBIX_SERVER = os.getenv("ZABBIX_SERVER")
ZABBIX_HOST = os.getenv("ZABBIX_HOST")

DISCOVERY_KEY = os.getenv("DISCOVERY_KEY", "bolid.discovery")

STATE_FILE = os.getenv("STATE_FILE", "last_states.json")
