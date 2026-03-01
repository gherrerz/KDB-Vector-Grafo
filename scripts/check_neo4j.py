import os
import sys
from dotenv import load_dotenv
from neo4j import GraphDatabase


def main() -> int:
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
        print(f"❌ Variables faltantes: {', '.join(missing)}")
        return 1

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session(database=database) as session:
            result = session.run("RETURN 1 AS ok")
            value = result.single()["ok"]
        driver.close()

        if value == 1:
            print("✅ Neo4j conectado correctamente")
            print(f"- URI: {uri}")
            print(f"- Database: {database}")
            return 0
        print("❌ Neo4j respondió un valor inesperado")
        return 1
    except Exception as exc:
        print(f"❌ Error conectando Neo4j: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
