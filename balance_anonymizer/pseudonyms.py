"""Seudonimizacion determinista, local y sin tablas persistentes."""

from __future__ import annotations

import calendar
import hashlib
import hmac
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import PseudonymRegistry


_MONTHS = {
    "enero": 1,
    "ene": 1,
    "febrero": 2,
    "feb": 2,
    "marzo": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "mayo": 5,
    "may": 5,
    "junio": 6,
    "jun": 6,
    "julio": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "septiembre": 9,
    "setiembre": 9,
    "sept": 9,
    "sep": 9,
    "octubre": 10,
    "oct": 10,
    "noviembre": 11,
    "nov": 11,
    "diciembre": 12,
    "dic": 12,
}
_MONTH_NAMES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
_MONTH_ABBREVIATIONS = (
    "ene",
    "feb",
    "mar",
    "abr",
    "may",
    "jun",
    "jul",
    "ago",
    "sep",
    "oct",
    "nov",
    "dic",
)
_MONTH_PATTERN = r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{2,15}"

_NUMERIC_DATE_RE = re.compile(
    r"(?<!\d)(?P<first>\d{1,4})(?P<separator>[-/.])"
    r"(?P<second>\d{1,2})(?P=separator)(?P<third>\d{1,4})(?!\d)"
)
_TEXTUAL_SEPARATOR_DATE_RE = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})(?P<first_separator>[-/.])"
    rf"(?P<month>(?:{_MONTH_PATTERN})\.?)"
    rf"(?P<second_separator>[-/.])(?P<year>\d{{2,4}})(?!\d)",
    re.IGNORECASE,
)
_TEXTUAL_DE_DATE_RE = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})(?P<first_separator>\s+de\s+)"
    rf"(?P<month>(?:{_MONTH_PATTERN})\.?)"
    rf"(?P<second_separator>\s+de\s+)(?P<year>\d{{2,4}})(?!\d)",
    re.IGNORECASE,
)
_TEXTUAL_SPACE_DATE_RE = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})(?P<first_separator>\s+)"
    rf"(?P<month>(?:{_MONTH_PATTERN})\.?)"
    rf"(?P<second_separator>\s+)(?P<year>\d{{2,4}})(?!\d)",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?"
    r"(?P<meridiem>\s*[AaPp]\.?(?:\s*)[Mm]\.?)?(?!\d)"
)

_LEGAL_SUFFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("SOCIEDAD", "ANONIMA", "PROMOTORA", "DE", "INVERSION", "DE", "CAPITAL", "VARIABLE"), "S.A.P.I. DE C.V."),
    (("SAPI", "DE", "CV"), "S.A.P.I. DE C.V."),
    (("S", "A", "P", "I", "DE", "C", "V"), "S.A.P.I. DE C.V."),
    (("SOCIEDAD", "DE", "RESPONSABILIDAD", "LIMITADA", "DE", "CAPITAL", "VARIABLE"), "S. DE R.L. DE C.V."),
    (("S", "DE", "RL", "DE", "CV"), "S. DE R.L. DE C.V."),
    (("S", "DE", "R", "L", "DE", "C", "V"), "S. DE R.L. DE C.V."),
    (("SOCIEDAD", "ANONIMA", "DE", "CAPITAL", "VARIABLE"), "S.A. DE C.V."),
    (("SA", "DE", "CV"), "S.A. DE C.V."),
    (("S", "A", "DE", "C", "V"), "S.A. DE C.V."),
    (("SOCIEDAD", "POR", "ACCIONES", "SIMPLIFICADA"), "S.A.S."),
    (("SAS",), "S.A.S."),
    (("S", "A", "S"), "S.A.S."),
    (("ASOCIACION", "CIVIL"), "A.C."),
    (("AC",), "A.C."),
    (("A", "C"), "A.C."),
    (("SOCIEDAD", "CIVIL"), "S.C."),
    (("SC",), "S.C."),
    (("S", "C"), "S.C."),
    (("SOCIEDAD", "ANONIMA"), "S.A."),
    (("SA",), "S.A."),
    (("S", "A"), "S.A."),
)


def normalize(value: str) -> str:
    """Normaliza una clave de forma estable sin alterar el valor mostrado."""

    decomposed = unicodedata.normalize("NFD", value.upper())
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", without_marks).strip()


def canonical_association_key(entity: str, identifier: str) -> str:
    """Clave común para la misma asociación escrita con distinto formato."""

    normalized_entity = normalize(entity)
    normalized_entity = re.sub(
        r"\b(?:CTA|CUENTA|CLABE|TARJETA|CLIENTE)\.?\s*[:.#-]?\s*$",
        "",
        normalized_entity,
    ).strip(" :-;,.\t")
    digits = re.sub(r"\D", "", identifier)
    normalized_identifier = digits or re.sub(r"[^A-Z0-9&Ñ]", "", normalize(identifier))
    return normalize(f"{normalized_entity}:{normalized_identifier}")


def _canonical_legal_suffix(value: str | None) -> str:
    """Devuelve solo un sufijo juridico conocido, nunca el nombre fuente."""

    if not value:
        return ""
    tokens = tuple(re.findall(r"[A-Z]+", normalize(value)))
    for suffix_tokens, replacement in _LEGAL_SUFFIXES:
        if len(tokens) >= len(suffix_tokens) and tokens[-len(suffix_tokens) :] == suffix_tokens:
            return replacement
    return ""


def _case_like(template: str, replacement: str) -> str:
    letters = "".join(char for char in template if char.isalpha())
    if letters.isupper():
        return replacement.upper()
    if letters.islower():
        return replacement.lower()
    if letters[:1].isupper() and letters[1:].islower():
        return replacement.capitalize()
    return replacement


def _year_from_text(value: str) -> int:
    year = int(value)
    if len(value) == 2:
        return year + (2000 if year < 70 else 1900)
    return year


def _format_year(year: int, width: int) -> str:
    if width == 2:
        return f"{year % 100:02d}"
    return f"{year:0{width}d}"


def _fits_width(value: int, width: int) -> bool:
    return len(str(abs(value))) <= width


class Pseudonymizer:
    """Deriva valores sinteticos con HMAC-SHA256 a partir de una semilla secreta."""

    def __init__(
        self,
        seed: str,
        *,
        registry: "PseudonymRegistry | None" = None,
        scope: str = "batch",
    ) -> None:
        if len(seed.strip()) < 16:
            raise ValueError("La semilla debe tener al menos 16 caracteres.")
        self._key = seed.encode("utf-8")
        self._registry = registry
        self._scope = normalize(scope) or "BATCH"
        self._temporal_fallback_count = 0

    def _digest(self, namespace: str, value: str) -> bytes:
        message = f"{namespace}:{normalize(value)}".encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def _byte_stream(self, namespace: str, value: str) -> Iterator[int]:
        counter = 0
        while True:
            yield from self._digest(namespace, f"{value}:{counter}")
            counter += 1

    def token(self, namespace: str, value: str, length: int = 8) -> str:
        return self._digest(namespace, value).hex().upper()[:length]

    def _registered(
        self,
        namespace: str,
        identifier: str,
        token_type: str,
        synthetic_value: str,
    ) -> str:
        if self._registry is None:
            return synthetic_value
        identifier_hmac = self._digest(f"registry-{namespace}", identifier).hex().upper()
        return self._registry.get_or_assign(
            namespace,
            identifier_hmac,
            token_type,
            synthetic_value,
        )

    def company_alternatives(self, entity_key: str, legal_suffix: str | None = None) -> tuple[str, ...]:
        """Nombres de empresa, del mas descriptivo al mas compacto."""

        token = self.token("company", entity_key, 8)
        suffix = _canonical_legal_suffix(legal_suffix)
        suffix_text = f" {suffix}" if suffix else ""
        primary = self._registered(
            "owner", entity_key, "company", f"ENTIDAD SINTETICA {token}{suffix_text}",
        )
        return (
            primary,
            f"ENTIDAD {token}{suffix_text}",
            f"E-{token}{suffix_text}",
        )

    def company(
        self,
        entity_key: str,
        legal_suffix: str | None = None,
        *,
        short: bool = False,
    ) -> str:
        """Empresa ficticia consistente, con sufijo juridico generico opcional."""

        alternatives = self.company_alternatives(entity_key, legal_suffix)
        return alternatives[-1] if short else alternatives[0]

    def short_company(self, entity_key: str, legal_suffix: str | None = None) -> str:
        return self.company(entity_key, legal_suffix, short=True)

    def compact_company(self, entity_key: str) -> str:
        token = self.token("company", entity_key, 8)
        return self._registered("owner", f"compact:{entity_key}", "company", f"E-SINT-{token}")

    def person_alternatives(self, entity_key: str) -> tuple[str, ...]:
        token = self.token("person", entity_key, 8)
        primary = self._registered(
            "owner", entity_key, "person", f"PERSONA SINTETICA {token}",
        )
        return (primary, f"PERSONA {token}", f"P-{token}")

    def person(self, entity_key: str, *, short: bool = False) -> str:
        alternatives = self.person_alternatives(entity_key)
        return alternatives[-1] if short else alternatives[0]

    def short_person(self, entity_key: str) -> str:
        return self.person(entity_key, short=True)

    def compact_person(self, entity_key: str) -> str:
        token = self.token("person", entity_key, 8)
        return self._registered("owner", f"compact:{entity_key}", "person", f"P-SINT-{token}")

    def user_alternatives(self, entity_key: str) -> tuple[str, ...]:
        token = self.token("user", entity_key, 8)
        primary = self._registered(
            "owner", entity_key, "user", f"USUARIO SINTETICO {token}",
        )
        return (primary, f"USUARIO {token}", f"U-{token}")

    def user(self, entity_key: str, *, short: bool = False) -> str:
        alternatives = self.user_alternatives(entity_key)
        return alternatives[-1] if short else alternatives[0]

    def short_user(self, entity_key: str) -> str:
        return self.user(entity_key, short=True)

    def bank_alternatives(self, association_key: str) -> tuple[str, ...]:
        token = self.token("bank", association_key, 8)
        primary = self._registered(
            "bank", association_key, "bank", f"BANCO FICTICIO NEXORA {token}",
        )
        return (
            primary,
            f"BANCO FICTICIO {token}",
            f"B-FICT-{token}",
        )

    def bank(self, association_key: str, *, short: bool = False) -> str:
        alternatives = self.bank_alternatives(association_key)
        return alternatives[-1] if short else alternatives[0]

    def short_bank(self, association_key: str) -> str:
        return self.bank(association_key, short=True)

    def rfc(self, original: str, entity_key: str) -> str:
        """RFC sintetico de 12, 13 o 14 caracteres, sin consultar al SAT."""

        compact = re.sub(r"[^A-Z0-9&Ñ]", "", normalize(original))
        if len(compact) not in (12, 13, 14):
            raise ValueError("RFC con longitud no compatible.")
        digest = self._digest("rfc", entity_key)
        prefix = "Z" * (len(compact) - 9)
        year = 70 + digest[0] % 30
        month = 1 + digest[1] % 12
        day = 1 + digest[2] % 28
        suffix_alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        suffix = "".join(suffix_alphabet[byte % len(suffix_alphabet)] for byte in digest[3:6])
        replacement = f"{prefix}{year:02d}{month:02d}{day:02d}{suffix}"
        if replacement == compact:
            last = suffix_alphabet[(suffix_alphabet.index(replacement[-1]) + 1) % len(suffix_alphabet)]
            replacement = f"{replacement[:-1]}{last}"
        return self._registered("rfc", entity_key, "rfc", replacement)

    def address_alternatives(self, entity_key: str) -> tuple[str, ...]:
        number = 100 + int.from_bytes(self._digest("address", entity_key)[:2], "big") % 8900
        primary = self._registered(
            "address", entity_key, "address", f"VIA SINTETICA #{number} ZONA NEUTRA",
        )
        return (
            primary,
            f"VIA SINT. #{number}",
            f"V-{number}",
        )

    def address(self, entity_key: str, *, short: bool = False) -> str:
        alternatives = self.address_alternatives(entity_key)
        return alternatives[-1] if short else alternatives[0]

    def short_address(self, entity_key: str) -> str:
        return self.address(entity_key, short=True)

    def population_alternatives(self, entity_key: str) -> tuple[str, ...]:
        # Ocho caracteres mantienen compacta la cabecera y evitan que el
        # registro colisione con lotes de apenas unas decenas de entidades.
        code = self.token("population", entity_key, 8)
        primary = self._registered(
            "address", f"population:{entity_key}", "population", f"CIUDAD NEXORA, {code}",
        )
        return (primary, f"NEXORA, {code}", f"NX-{code}")

    def population(self, entity_key: str, *, short: bool = False) -> str:
        alternatives = self.population_alternatives(entity_key)
        return alternatives[-1] if short else alternatives[0]

    def short_population(self, entity_key: str) -> str:
        return self.population(entity_key, short=True)

    def certificate(self, original: str, entity_key: str) -> str:
        """Sustituye solo caracteres alfanumericos y conserva sus separadores."""

        digest = self._digest("certificate", f"{entity_key}:{original}")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        values = iter(digest * ((len(original) // len(digest)) + 1))
        replacement = "".join(
            alphabet[next(values) % len(alphabet)] if char.isalnum() else char for char in original
        )
        return self._registered(
            "owner", f"certificate:{entity_key}:{normalize(original)}", "certificate", replacement,
        )

    def numeric_identifier(self, original: str, association_key: str) -> str:
        """Sustituye cada digito y conserva exactamente todos los separadores."""

        if not any(char.isdigit() for char in original):
            raise ValueError("El identificador numerico no contiene digitos.")
        original_digits = re.sub(r"\D", "", original)
        values = self._byte_stream(
            "numeric-identifier",
            f"{association_key}:{original_digits}",
        )
        replacement_digits: list[str] = []
        for char in original_digits:
            original_digit = int(char)
            new_digit = next(values) % 9
            if new_digit >= original_digit:
                new_digit += 1
            replacement_digits.append(str(new_digit))
        assigned_digits = self._registered(
            "account",
            f"{association_key}:{original_digits}",
            "numeric_identifier_digits",
            "".join(replacement_digits),
        )
        replacements = iter(assigned_digits)
        return "".join(next(replacements) if char.isdigit() else char for char in original)

    def bank_account(self, original: str, association_key: str | None = None) -> str:
        """API historica, con clave comun opcional para nuevas asociaciones."""

        return self.numeric_identifier(original, association_key or normalize(original))

    def bank_and_identifier(
        self,
        bank_original: str,
        identifier_original: str,
        association_key: str | None = None,
        *,
        short_bank: bool = False,
    ) -> tuple[str, str]:
        """Genera banco e identificador desde una clave comun de asociacion."""

        common_key = association_key or f"{normalize(bank_original)}|{normalize(identifier_original)}"
        return (
            self.bank(common_key, short=short_bank),
            self.numeric_identifier(identifier_original, common_key),
        )

    def bank_pair(
        self,
        bank_original: str,
        identifier_original: str,
        association_key: str | None = None,
        *,
        short_bank: bool = False,
    ) -> tuple[str, str]:
        return self.bank_and_identifier(
            bank_original,
            identifier_original,
            association_key,
            short_bank=short_bank,
        )

    def date_offset(self, entity_key: str | None = None) -> timedelta:
        """API histórica: aproximación en días del desplazamiento mensual."""

        return timedelta(days=self.month_offset(entity_key) * 30)

    def month_offset(self, entity_key: str | None = None) -> int:
        scope = normalize(entity_key or self._scope)
        digest = self._digest("date-offset", scope)
        candidates = tuple(-value for value in range(13, 121) if value % 12)
        # Un desfase mensual no es un identificador: varios sujetos pueden
        # compartirlo sin crear asociación entre ellos. Registrarlo como
        # valor único agotaba el espacio finito de 99 alternativas y podía
        # bloquear un lote válido con una colisión artificial.
        return candidates[int.from_bytes(digest[:2], "big") % len(candidates)]

    def shift_date(self, original: date) -> date:
        """Desplaza una fecha preservando fin de mes cuando corresponde."""

        month_index = original.year * 12 + original.month - 1 + self.month_offset()
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        last_day = calendar.monthrange(year, month)[1]
        if original.day == calendar.monthrange(original.year, original.month)[1]:
            day = last_day
        else:
            day = min(original.day, last_day)
        return date(year, month, day)

    @property
    def temporal_fallback_used(self) -> bool:
        return self._temporal_fallback_count > 0

    @property
    def temporal_fallback_count(self) -> int:
        return self._temporal_fallback_count

    @property
    def date_fallback_used(self) -> bool:
        """Alias para reportar el estado sin revelar valores fuente."""

        return self.temporal_fallback_used

    @property
    def date_fallback_count(self) -> int:
        return self.temporal_fallback_count

    def fallback_status(self) -> dict[str, int | bool]:
        """Resumen seguro: no incluye originales ni claves derivadas."""

        return {
            "temporal_fallback_used": self.temporal_fallback_used,
            "temporal_fallback_count": self.temporal_fallback_count,
        }

    def _record_fallbacks(self, count: int) -> None:
        self._temporal_fallback_count += count

    def _generated_date(
        self,
        source: str,
        *,
        year_width: int,
        month_width: int,
        day_width: int,
    ) -> tuple[int, int, int]:
        digest = self._digest("date-fallback", source)
        year = 1970 + int.from_bytes(digest[:2], "big") % 56
        month_limit = 9 if month_width == 1 else 12
        month = 1 + digest[2] % month_limit
        day_limit = calendar.monthrange(year, month)[1]
        if day_width == 1:
            day_limit = min(day_limit, 9)
        day = 1 + digest[3] % day_limit
        if year_width == 2:
            year = 1970 + digest[4] % 30
        return year, month, day

    def _shift_or_generate_date(
        self,
        source: str,
        year: int,
        month: int,
        day: int,
        *,
        year_width: int,
        month_width: int,
        day_width: int,
    ) -> tuple[int, int, int, bool]:
        try:
            original = date(year, month, day)
            shifted_date = self.shift_date(original)
            shifted = datetime(shifted_date.year, shifted_date.month, shifted_date.day)
        except (OverflowError, ValueError):
            generated = self._generated_date(
                source,
                year_width=year_width,
                month_width=month_width,
                day_width=day_width,
            )
            return generated[0], generated[1], generated[2], True
        if not (
            _fits_width(shifted.year % 100 if year_width == 2 else shifted.year, year_width)
            and _fits_width(shifted.month, month_width)
            and _fits_width(shifted.day, day_width)
        ):
            generated = self._generated_date(
                source,
                year_width=year_width,
                month_width=month_width,
                day_width=day_width,
            )
            return generated[0], generated[1], generated[2], False
        return shifted.year, shifted.month, shifted.day, False

    def _replace_numeric_date(self, match: re.Match[str]) -> tuple[str, bool]:
        first, second, third = match.group("first", "second", "third")
        separator = match.group("separator")
        if len(first) == 4 and len(third) <= 2:
            year_text, month_text, day_text = first, second, third
            order = "ymd"
        elif len(first) <= 2 and len(third) in (2, 4):
            day_text, month_text, year_text = first, second, third
            order = "dmy"
        else:
            raise ValueError("Formato de fecha de cabecera no admitido.")
        year, month, day, fallback = self._shift_or_generate_date(
            match.group(),
            _year_from_text(year_text),
            int(month_text),
            int(day_text),
            year_width=len(year_text),
            month_width=len(month_text),
            day_width=len(day_text),
        )
        formatted_year = _format_year(year, len(year_text))
        formatted_month = f"{month:0{len(month_text)}d}"
        formatted_day = f"{day:0{len(day_text)}d}"
        if order == "ymd":
            return separator.join((formatted_year, formatted_month, formatted_day)), fallback
        return separator.join((formatted_day, formatted_month, formatted_year)), fallback

    def _replace_textual_date(self, match: re.Match[str]) -> tuple[str, bool]:
        day_text, month_text, year_text = match.group("day", "month", "year")
        month_key = normalize(month_text.rstrip(".")).lower()
        month = _MONTHS.get(month_key, 0)
        year, replacement_month, day, fallback = self._shift_or_generate_date(
            match.group(),
            _year_from_text(year_text),
            month,
            int(day_text),
            year_width=len(year_text),
            month_width=2,
            day_width=len(day_text),
        )
        abbreviated = len(month_key) <= 4
        rendered_month = (
            _MONTH_ABBREVIATIONS[replacement_month - 1]
            if abbreviated
            else _MONTH_NAMES[replacement_month - 1]
        )
        rendered_month = _case_like(month_text, rendered_month)
        if month_text.endswith("."):
            rendered_month += "."
        rendered_day = f"{day:0{len(day_text)}d}"
        rendered_year = _format_year(year, len(year_text))
        return (
            f"{rendered_day}{match.group('first_separator')}"
            f"{rendered_month}{match.group('second_separator')}{rendered_year}",
            fallback,
        )

    def _date_matches(self, value: str) -> list[re.Match[str]]:
        candidates: list[re.Match[str]] = []
        for pattern in (
            _TEXTUAL_DE_DATE_RE,
            _TEXTUAL_SEPARATOR_DATE_RE,
            _TEXTUAL_SPACE_DATE_RE,
            _NUMERIC_DATE_RE,
        ):
            candidates.extend(pattern.finditer(value))
        selected: list[re.Match[str]] = []
        for match in sorted(candidates, key=lambda item: (item.start(), -(item.end() - item.start()))):
            if any(match.start() < current.end() and current.start() < match.end() for current in selected):
                continue
            selected.append(match)
        return sorted(selected, key=lambda item: item.start())

    def _replace_date_match(self, match: re.Match[str]) -> tuple[str, bool]:
        if match.re is _NUMERIC_DATE_RE:
            return self._replace_numeric_date(match)
        return self._replace_textual_date(match)

    def _replace_time_match(self, match: re.Match[str]) -> tuple[str, bool]:
        hour_text, minute_text, second_text = match.group("hour", "minute", "second")
        marker = match.group("meridiem") or ""
        hour, minute = int(hour_text), int(minute_text)
        second = int(second_text) if second_text is not None else 0
        marker_letters = "".join(char.upper() for char in marker if char.isalpha())
        uses_meridiem = bool(marker_letters)
        valid = (
            0 <= minute <= 59
            and 0 <= second <= 59
            and ((1 <= hour <= 12) if uses_meridiem else (0 <= hour <= 23))
        )
        digest = self._digest("time", match.group())
        fallback = not valid
        if valid:
            if uses_meridiem:
                is_pm = marker_letters.startswith("P")
                hour_24 = (hour % 12) + (12 if is_pm else 0)
            else:
                hour_24 = hour
            total_minutes = (hour_24 * 60 + minute + 1 + int.from_bytes(digest[:2], "big") % 719) % 1440
            replacement_hour_24, replacement_minute = divmod(total_minutes, 60)
            replacement_second = second
        else:
            replacement_hour_24 = digest[0] % 24
            replacement_minute = digest[1] % 60
            replacement_second = digest[2] % 60
        if uses_meridiem:
            replacement_is_pm = replacement_hour_24 >= 12
            replacement_hour = replacement_hour_24 % 12 or 12
            replacement_marker_letter = "P" if replacement_is_pm else "A"
            marker_chars = list(marker)
            for index, char in enumerate(marker_chars):
                if char.upper() in ("A", "P"):
                    marker_chars[index] = (
                        replacement_marker_letter if char.isupper() else replacement_marker_letter.lower()
                    )
                    break
            rendered_marker = "".join(marker_chars)
        else:
            replacement_hour = replacement_hour_24
            rendered_marker = ""
        if not _fits_width(replacement_hour, len(hour_text)):
            replacement_hour = 1 + digest[3] % 9
        rendered = f"{replacement_hour:0{len(hour_text)}d}:{replacement_minute:02d}"
        if second_text is not None:
            rendered += f":{replacement_second:02d}"
        return rendered + rendered_marker, fallback

    def _replace_components(
        self,
        value: str,
        date_matches: list[re.Match[str]],
        time_matches: list[re.Match[str]],
    ) -> tuple[str, int]:
        replacements: list[tuple[int, int, str, bool]] = []
        for match in date_matches:
            replacement, fallback = self._replace_date_match(match)
            replacements.append((match.start(), match.end(), replacement, fallback))
        for match in time_matches:
            if any(match.start() < end and start < match.end() for start, end, _, _ in replacements):
                continue
            replacement, fallback = self._replace_time_match(match)
            replacements.append((match.start(), match.end(), replacement, fallback))
        chunks: list[str] = []
        cursor = 0
        for start, end, replacement, _ in sorted(replacements):
            chunks.append(value[cursor:start])
            chunks.append(replacement)
            cursor = end
        chunks.append(value[cursor:])
        return "".join(chunks), sum(int(item[3]) for item in replacements)

    def replace_date_with_status(self, original: str) -> tuple[str, bool]:
        """Reemplaza una fecha, con hora opcional, e informa fallback sin PII."""

        date_matches = self._date_matches(original)
        time_matches = list(_TIME_RE.finditer(original))
        if len(date_matches) != 1:
            raise ValueError("Formato de fecha de cabecera no admitido.")
        covered = sorted(
            [(item.start(), item.end()) for item in date_matches + time_matches],
            key=lambda item: item[0],
        )
        residual_parts: list[str] = []
        cursor = 0
        for start, end in covered:
            residual_parts.append(original[cursor:start])
            cursor = end
        residual_parts.append(original[cursor:])
        if "".join(residual_parts).strip():
            raise ValueError("Formato de fecha de cabecera no admitido.")
        replacement, fallback_count = self._replace_components(original, date_matches, time_matches)
        self._record_fallbacks(fallback_count)
        return replacement, fallback_count > 0

    def replace_date(self, original: str) -> str:
        """API historica: reemplaza fecha/hora conservando su forma visual."""

        return self.replace_date_with_status(original)[0]

    def replace_time_with_status(self, original: str) -> tuple[str, bool]:
        time_matches = list(_TIME_RE.finditer(original))
        if len(time_matches) != 1:
            raise ValueError("Formato de hora de cabecera no admitido.")
        match = time_matches[0]
        if (original[: match.start()] + original[match.end() :]).strip():
            raise ValueError("Formato de hora de cabecera no admitido.")
        replacement, fallback = self._replace_time_match(match)
        self._record_fallbacks(int(fallback))
        return f"{original[:match.start()]}{replacement}{original[match.end():]}", fallback

    def replace_time(self, original: str) -> str:
        return self.replace_time_with_status(original)[0]

    def replace_date_range_with_status(self, original: str) -> tuple[str, bool]:
        date_matches = self._date_matches(original)
        if len(date_matches) < 2:
            raise ValueError("Formato de rango de fechas no admitido.")
        time_matches = list(_TIME_RE.finditer(original))
        replacement, fallback_count = self._replace_components(original, date_matches, time_matches)
        self._record_fallbacks(fallback_count)
        return replacement, fallback_count > 0

    def replace_date_range(self, original: str) -> str:
        return self.replace_date_range_with_status(original)[0]

    def replace_temporal_with_status(self, original: str) -> tuple[str, bool]:
        """Reemplaza fechas, rangos y horas presentes en un valor temporal."""

        date_matches = self._date_matches(original)
        time_matches = list(_TIME_RE.finditer(original))
        if not date_matches and not time_matches:
            raise ValueError("Formato temporal no admitido.")
        replacement, fallback_count = self._replace_components(original, date_matches, time_matches)
        self._record_fallbacks(fallback_count)
        return replacement, fallback_count > 0

    def replace_temporal(self, original: str) -> str:
        return self.replace_temporal_with_status(original)[0]

    def exercise_and_period(self, exercise: str, period: str) -> tuple[str, str]:
        """Aplica el mismo desplazamiento a ejercicio y periodo de cabecera."""

        if not re.fullmatch(r"\d{4}", exercise) or not re.fullmatch(r"\d{1,2}", period):
            raise ValueError("Ejercicio o periodo no compatible.")
        try:
            shifted = self.shift_date(date(int(exercise), int(period), 15))
            shifted_year, shifted_period = shifted.year, shifted.month
        except ValueError:
            digest = self._digest("exercise-period-fallback", f"{exercise}:{period}")
            shifted_year = 1970 + int.from_bytes(digest[:2], "big") % 56
            shifted_period = 1 + digest[2] % (9 if len(period) == 1 else 12)
            self._record_fallbacks(1)
        # La pareja puede ser distinta aunque uno de sus componentes coincida
        # por casualidad. Ambos campos son sensibles y deben cambiar por si
        # mismos, manteniendo un mes valido y el ancho fuente.
        if shifted_year == int(exercise):
            shifted_year = shifted_year - 1 if shifted_year > 1 else shifted_year + 1
        if shifted_period == int(period):
            limit = 9 if len(period) == 1 else 12
            shifted_period = shifted_period % limit + 1
        period_format = f"0{len(period)}d" if len(period) > 1 else "d"
        return str(shifted_year), format(shifted_period, period_format)
