import logging
import sys
import types
from pathlib import Path
import pytest


def _install_astrbot_stubs(base_data_dir: Path):
    """Install lightweight astrbot.api stubs so plugin modules can be imported and tested standalone."""
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    comp_mod = types.ModuleType("astrbot.api.message_components")

    test_logger = logging.getLogger("astrbot_test")
    api_mod.logger = test_logger

    class EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"

    class _CommandGroupHandle:
        def __init__(self, name, alias=None):
            self.name = name
            self.alias = alias

        def command(self, sub_name, alias=None):
            def decorator(fn):
                return fn
            return decorator

    class _FilterNamespace:

        @staticmethod
        def event_message_type(*args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

        @staticmethod
        def command_group(name, alias=None):
            def decorator(fn):
                return _CommandGroupHandle(name, alias=alias)
            return decorator

        @staticmethod
        def command(name, alias=None):
            def decorator(fn):
                return fn
            return decorator

    _FilterNamespace.EventMessageType = EventMessageType

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

    class File:
        def __init__(self, name: str = "", file: str = "", url: str = "", file_id: str = ""):
            self.name = name
            self.file = file
            self.url = url
            self.file_id = file_id

        async def get_file(self):
            return self.file

    class AstrMessageEvent:
        def __init__(
            self,
            sender_id="10001",
            sender_name="Tester",
            self_id="99999",
            group_id=None,
            message_str="",
            raw_message=None,
            is_admin=False,
            role="member",
            is_at_or_wake_command=True,
            bot=None,
            messages=None,
        ):
            self._sender_id = str(sender_id)
            self._sender_name = sender_name
            self._self_id = str(self_id)
            self.message_str = message_str
            self._is_admin = is_admin
            self.role = role
            self.is_at_or_wake_command = is_at_or_wake_command
            self.bot = bot
            self._messages = list(messages or [])
            self.sent_chains = []
            self.stopped = False
            self.message_obj = types.SimpleNamespace(
                group_id=group_id,
                raw_message=raw_message if raw_message is not None else {},
                sender={"user_id": sender_id, "role": role},
            )

        def get_sender_id(self):
            return self._sender_id

        def get_sender_name(self):
            return self._sender_name

        def get_self_id(self):
            return self._self_id

        def get_messages(self):
            return self._messages

        def is_admin(self):
            return self._is_admin

        def stop_event(self):
            self.stopped = True

        def plain_result(self, text: str):
            return {"type": "plain", "text": text}

        def chain_result(self, components):
            return {"type": "chain", "components": components}

        async def send(self, chain):
            self.sent_chains.append(chain)

    class Context:
        def __init__(self, wake_prefix=None):
            self._config = {"wake_prefix": wake_prefix or ["/"]}

        def get_config(self):
            return self._config

    class Star:
        def __init__(self, context: Context):
            self.context = context

    class StarTools:
        _data_root = base_data_dir

        @classmethod
        def get_data_dir(cls, plugin_name: str = "openlist"):
            p = cls._data_root / plugin_name
            p.mkdir(parents=True, exist_ok=True)
            return p

    event_mod.filter = _FilterNamespace
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    comp_mod.File = File

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = comp_mod
    return StarTools, Context, AstrMessageEvent, File


_BOOT_DIR = Path("/tmp/astrbot_openlist_test_boot")
_STAR_TOOLS, _CTX_CLS, _EVENT_CLS, _FILE_CLS = _install_astrbot_stubs(_BOOT_DIR)

# Ensure parent of package is importable as astrbot_plugin_openlist_bot
REPO_PARENT = str(Path(__file__).resolve().parents[2])
if REPO_PARENT not in sys.path:
    sys.path.insert(0, REPO_PARENT)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path):
    _STAR_TOOLS._data_root = tmp_path
    from astrbot_plugin_openlist_bot.lib.client import OpenlistClient
    OpenlistClient.clear_token_cache()
    yield tmp_path
    OpenlistClient.clear_token_cache()
