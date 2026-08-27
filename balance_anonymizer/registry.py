"""Registro local irreversible de asignaciones sintéticas."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1


class RegistryError(RuntimeError):
    """Fallo seguro del registro sin valores originales."""


class PseudonymRegistry:
    """Persiste únicamente HMAC, namespace y valor sintético."""

    def __init__(self, path: Path, *, algorithm_version: str = "2") -> None:
        self.path = path.resolve()
        self.algorithm_version = algorithm_version
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RegistryError("El registro usa una versión de esquema más reciente.")
        if version == 0:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE assignments (
                        namespace TEXT NOT NULL,
                        identifier_hmac TEXT NOT NULL,
                        token_type TEXT NOT NULL,
                        synthetic_value TEXT NOT NULL,
                        algorithm_version TEXT NOT NULL,
                        PRIMARY KEY (namespace, identifier_hmac, algorithm_version),
                        UNIQUE (namespace, synthetic_value, algorithm_version)
                    );
                    CREATE TABLE collisions (
                        namespace TEXT NOT NULL,
                        collision_hmac TEXT NOT NULL,
                        algorithm_version TEXT NOT NULL,
                        occurrences INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (namespace, collision_hmac, algorithm_version)
                    );
                    PRAGMA user_version = 1;
                    """
                )

    def get_or_assign(
        self,
        namespace: str,
        identifier_hmac: str,
        token_type: str,
        synthetic_value: str,
    ) -> str:
        """Obtiene o inserta una asignación sin recibir el identificador original."""

        existing = self._connection.execute(
            """
            SELECT synthetic_value FROM assignments
            WHERE namespace = ? AND identifier_hmac = ? AND algorithm_version = ?
            """,
            (namespace, identifier_hmac, self.algorithm_version),
        ).fetchone()
        if existing:
            return str(existing[0])
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO assignments
                    (namespace, identifier_hmac, token_type, synthetic_value, algorithm_version)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        namespace,
                        identifier_hmac,
                        token_type,
                        synthetic_value,
                        self.algorithm_version,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            collision_hmac = identifier_hmac[:32]
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO collisions
                    (namespace, collision_hmac, algorithm_version, occurrences)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(namespace, collision_hmac, algorithm_version)
                    DO UPDATE SET occurrences = occurrences + 1
                    """,
                    (namespace, collision_hmac, self.algorithm_version),
                )
            raise RegistryError("Se detectó una colisión de asignación sintética.") from exc
        return synthetic_value

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PseudonymRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
