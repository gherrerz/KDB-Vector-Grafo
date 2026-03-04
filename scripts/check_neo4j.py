"""Utility script to validate Neo4j connectivity from environment vars."""

import os
import sys
import logging

from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable


LOGGER = logging.getLogger(__name__)


def main() -> int:
    """Validate Neo4j connectivity and return a process exit code."""
    load_dotenv()

    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE", "neo4j")

    missing = [
        key
        for key, value in {
            "NEO4J_URI": uri,
            "NEO4J_USER": user,
            "NEO4J_PASSWORD": password,
        }.items()
        if not value
    ]

    if missing:
        LOGGER.error("❌ Variables faltantes: %s", ", ".join(missing))
        return 1

    driver = None
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session(database=database) as session:
            result = session.run("RETURN 1 AS ok")
            value = result.single()["ok"]

        if value == 1:
            LOGGER.info("✅ Neo4j conectado correctamente")
            LOGGER.info("- URI: %s", uri)
            LOGGER.info("- Database: %s", database)
            return 0
        LOGGER.error("❌ Neo4j respondió un valor inesperado")
        return 1
    except (AuthError, ServiceUnavailable, Neo4jError) as exc:
        LOGGER.error("❌ Error conectando Neo4j: %s", exc)
        return 1
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
