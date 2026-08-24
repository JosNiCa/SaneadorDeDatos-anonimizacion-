"""Seudonimizacion determinista, local y sin tablas de equivalencias persistentes."""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from datetime import datetime, timedelta


_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_MONTH_NAMES = tuple(_MONTHS)


def normalize(value: str) -> str:
    """Normaliza una clave de forma estable sin alterar el valor mostrado."""

    decomposed = unicodedata.normalize("NFD", value.upper())
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", without_marks).strip()


class Pseudonymizer:
    """Deriva valores sinteticos con HMAC-SHA256 a partir de una semilla secreta."""

    def __init__(self, seed: str) -> None:
        if len(seed.strip()) < 16:
            raise ValueError("La semilla debe tener al menos 16 caracteres.")
        self._key = seed.encode("utf-8")

    def _digest(self, namespace: str, value: str) -> bytes:
        message = f"{namespace}:{normalize(value)}".encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def token(self, namespace: str, value: str, length: int = 8) -> str:
        return self._digest(namespace, value).hex().upper()[:length]

    def company(self, entity_key: str) -> str:
        """Nombre corporativo inequívocamente ficticio y consistente por entidad."""

        return f"ENTIDAD SINTETICA {self.token('company', entity_key, 8)}"

    def rfc(self, original: str, entity_key: str) -> str:
        """RFC visualmente equivalente (12/13), sintetico y no validado contra SAT."""

        compact = re.sub(r"[^A-Z0-9&Ñ]", "", normalize(original))
        if len(compact) not in (12, 13):
            raise ValueError("RFC con longitud no compatible.")
        digest = self._digest("rfc", entity_key)
        prefix = "ZZZZ" if len(compact) == 13 else "ZZZ"
        # La porcion central conserva una forma de fecha sin tomar digitos del RFC fuente.
        year = 70 + digest[0] % 30
        month = 1 + digest[1] % 12
        day = 1 + digest[2] % 28
        suffix_alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        suffix = "".join(suffix_alphabet[byte % len(suffix_alphabet)] for byte in digest[3:6])
        return f"{prefix}{year:02d}{month:02d}{day:02d}{suffix}"

    def address(self, entity_key: str) -> str:
        number = 100 + int.from_bytes(self._digest("address", entity_key)[:2], "big") % 8900
        return f"VIA SINTETICA #{number} ZONA NEUTRA"

    def population(self, entity_key: str) -> str:
        code = self.token("population", entity_key, 2)
        return f"CIUDAD NEXORA, {code}"

    def certificate(self, original: str, entity_key: str) -> str:
        """Sustituye solo caracteres alfanumericos y conserva sus separadores."""

        digest = self._digest("certificate", f"{entity_key}:{original}")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        iterator = iter(digest * ((len(original) // len(digest)) + 1))
        return "".join(
            alphabet[next(iterator) % len(alphabet)] if char.isalnum() else char for char in original
        )

    def bank_account(self, original: str) -> str:
        """Conserva longitud y separadores, cambiando exclusivamente los digitos."""

        digest = self._digest("bank-account", original)
        values = iter(digest * ((sum(char.isdigit() for char in original) // len(digest)) + 1))
        result = "".join(str(next(values) % 10) if char.isdigit() else char for char in original)
        # Evita que, por una colision improbable, el identificador se conserve intacto.
        if result == original:
            result = "".join("9" if char == "0" else "0" if char.isdigit() else char for char in original)
        return result

    def date_offset(self) -> timedelta:
        digest = self._digest("date-offset", "batch")
        return timedelta(days=-(366 + int.from_bytes(digest[:2], "big") % 3285))

    def replace_date(self, original: str) -> str:
        """Desplaza fechas de cabecera conservando uno de los formatos permitidos."""

        value = original.strip()
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
            try:
                parsed = datetime.strptime(value, pattern) + self.date_offset()
                if "%H" in pattern:
                    # Tambien cambia la hora de creacion sin abandonar el formato.
                    minute_shift = self._digest("time-offset", value)[0] % (12 * 60) + 1
                    parsed += timedelta(minutes=minute_shift)
                return parsed.strftime(pattern)
            except ValueError:
                pass

        match = re.fullmatch(r"(\d{1,2})/([A-Za-záéíóúñ]+)/(\d{2,4})", value, re.IGNORECASE)
        if not match:
            raise ValueError("Formato de fecha de cabecera no admitido.")
        day, month_text, year_text = match.groups()
        normalized_month = normalize(month_text).lower()
        month_number = _MONTHS.get(normalized_month)
        if month_number is None:
            raise ValueError("Mes en fecha de cabecera no admitido.")
        year = int(year_text)
        if len(year_text) == 2:
            year += 2000 if year < 70 else 1900
        parsed = datetime(year, month_number, int(day)) + self.date_offset()
        replacement_month = _MONTH_NAMES[parsed.month - 1]
        if month_text.isupper():
            replacement_month = replacement_month.upper()
        elif month_text[:1].isupper():
            replacement_month = replacement_month.capitalize()
        replacement_year = f"{parsed.year % 100:02d}" if len(year_text) == 2 else f"{parsed.year:04d}"
        return f"{parsed.day:0{len(day)}d}/{replacement_month}/{replacement_year}"

    def exercise_and_period(self, exercise: str, period: str) -> tuple[str, str]:
        """Aplica el mismo desplazamiento para ejercicio y periodo de una balanza."""

        if not re.fullmatch(r"\d{4}", exercise) or not re.fullmatch(r"\d{1,2}", period):
            raise ValueError("Ejercicio o periodo no compatible.")
        shifted = datetime(int(exercise), int(period), 15) + self.date_offset()
        period_format = f"0{len(period)}d" if len(period) > 1 else "d"
        return str(shifted.year), format(shifted.month, period_format)
