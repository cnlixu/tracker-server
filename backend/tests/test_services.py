"""Per-terminal JT/T 808 registration credential service tests."""

import asyncio

from backend.app.models import TerminalRegistration
from backend.app import services


PHONE = "13800138000"
IMEI = "862288087606784"


def registration() -> TerminalRegistration:
    return TerminalRegistration(
        terminal_phone=PHONE,
        province_id=51,
        city_id=5101,
        manufacturer_id="JZG043",
        terminal_model="JZG043A",
        terminal_id="89860064221642652497",
        plate_color=0,
        plate_number="0",
    )


def test_registration_issues_random_code_and_authentication_binds_imei(monkeypatch) -> None:
    async def scenario() -> None:
        stored = {}

        async def save(_pool, value, digest):
            stored[value.terminal_phone] = [digest, None]

        async def get(_pool, terminal_phone):
            value = stored.get(terminal_phone)
            return None if value is None else tuple(value)

        async def mark(_pool, terminal_phone, imei):
            value = stored.get(terminal_phone)
            if value is None or value[1] not in (None, imei):
                return False
            value[1] = imei
            return True

        monkeypatch.setattr(services, "save_terminal_registration", save)
        monkeypatch.setattr(services, "get_terminal_auth_credential", get)
        monkeypatch.setattr(services, "mark_terminal_authenticated", mark)

        service = services.TrackerService(pool=object())
        auth_code = await service.register_terminal(registration())

        assert 1 <= len(auth_code) <= 255
        assert auth_code not in str(stored[PHONE][0])
        assert await service.authenticate_terminal(PHONE, IMEI, auth_code) is True
        assert stored[PHONE][1] == IMEI
        assert await service.authenticate_terminal(PHONE, IMEI, "wrong") is False
        assert await service.authenticate_terminal(PHONE, "100000000000000", auth_code) is False

    asyncio.run(scenario())


def test_re_registration_rotates_the_previous_code(monkeypatch) -> None:
    async def scenario() -> None:
        stored = {}

        async def save(_pool, value, digest):
            stored[value.terminal_phone] = [digest, None]

        async def get(_pool, terminal_phone):
            value = stored.get(terminal_phone)
            return None if value is None else tuple(value)

        async def mark(_pool, terminal_phone, imei):
            stored[terminal_phone][1] = imei
            return True

        monkeypatch.setattr(services, "save_terminal_registration", save)
        monkeypatch.setattr(services, "get_terminal_auth_credential", get)
        monkeypatch.setattr(services, "mark_terminal_authenticated", mark)

        service = services.TrackerService(pool=object())
        old_code = await service.register_terminal(registration())
        new_code = await service.register_terminal(registration())

        assert new_code != old_code
        assert await service.authenticate_terminal(PHONE, IMEI, old_code) is False
        assert await service.authenticate_terminal(PHONE, IMEI, new_code) is True

    asyncio.run(scenario())
