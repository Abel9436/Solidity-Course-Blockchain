"""
Interactive SQL REPL (Read-Eval-Print Loop).

Provides a command-line interface for interacting with the PySQLEngine
database, similar to psql (PostgreSQL), mysql, or sqlite3.

Features:
- Multi-line SQL input (statements end with semicolon)
- Formatted table output
- Special commands: .help, .tables, .schema, .stats, .explain, .quit
- Command history
- Error handling with helpful messages
"""

from __future__ import annotations

import sys
import time
from typing import Optional

from .engine import Database


# ANSI color codes for terminal output
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


BANNER = f"""
{Colors.CYAN}{Colors.BOLD}
  ╔═══════════════════════════════════════════════════════╗
  ║                                                       ║
  ║   ██████╗ ██╗   ██╗███████╗ ██████╗ ██╗               ║
  ║   ██╔══██╗╚██╗ ██╔╝██╔════╝██╔═══██╗██║               ║
  ║   ██████╔╝ ╚████╔╝ ███████╗██║   ██║██║               ║
  ║   ██╔═══╝   ╚██╔╝  ╚════██║██║▄▄ ██║██║               ║
  ║   ██║        ██║   ███████║╚██████╔╝███████╗           ║
  ║   ╚═╝        ╚═╝   ╚══════╝ ╚══▀▀═╝ ╚══════╝           ║
  ║                                                       ║
  ║   SQL Database Engine — Built from Scratch in Python  ║
  ║   Type .help for available commands                   ║
  ║                                                       ║
  ╚═══════════════════════════════════════════════════════╝
{Colors.RESET}"""

HELP_TEXT = f"""
{Colors.BOLD}Available Commands:{Colors.RESET}

  {Colors.GREEN}.help{Colors.RESET}              Show this help message
  {Colors.GREEN}.tables{Colors.RESET}            List all tables in the database
  {Colors.GREEN}.schema <table>{Colors.RESET}    Show the schema of a table
  {Colors.GREEN}.stats{Colors.RESET}             Show database statistics
  {Colors.GREEN}.explain <SQL>{Colors.RESET}     Show query execution plan
  {Colors.GREEN}.indexes <table>{Colors.RESET}   Show indexes for a table
  {Colors.GREEN}.quit{Colors.RESET}              Exit the REPL
  {Colors.GREEN}.exit{Colors.RESET}              Exit the REPL

{Colors.BOLD}SQL Support:{Colors.RESET}
  SELECT, INSERT, UPDATE, DELETE, CREATE TABLE, DROP TABLE,
  CREATE INDEX, JOIN (INNER/LEFT/RIGHT/FULL/CROSS),
  GROUP BY, HAVING, ORDER BY, LIMIT/OFFSET, DISTINCT,
  Subqueries, CASE/WHEN, CAST, BETWEEN, IN, LIKE, IS NULL,
  Aggregate functions (COUNT, SUM, AVG, MIN, MAX),
  BEGIN/COMMIT/ROLLBACK transactions

{Colors.DIM}End SQL statements with a semicolon (;)
Multi-line input is supported{Colors.RESET}
"""


def main() -> None:
    """Main entry point for the PySQLEngine REPL."""
    import argparse

    arg_parser = argparse.ArgumentParser(
        description="PySQLEngine — A SQL database engine built from scratch in Python"
    )
    arg_parser.add_argument(
        "database",
        nargs="?",
        default="pysql_data",
        help="Path to the database directory (default: pysql_data)",
    )
    arg_parser.add_argument(
        "--pool-size",
        type=int,
        default=1024,
        help="Buffer pool size in pages (default: 1024)",
    )
    arg_parser.add_argument(
        "-c", "--command",
        type=str,
        help="Execute a single SQL command and exit",
    )

    args = arg_parser.parse_args()

    # Initialize database
    db = Database(args.database, buffer_pool_size=args.pool_size)

    # Single command mode
    if args.command:
        try:
            result = db.execute(args.command)
            if result.columns:
                print(result.to_table_string())
            elif result.message:
                print(result.message)
            else:
                print(f"{result.affected_rows} row(s) affected")
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            db.close()
        return

    # Interactive REPL mode
    print(BANNER)
    print(f"  {Colors.DIM}Connected to database: {args.database}{Colors.RESET}")
    print()

    buffer = ""
    prompt = f"{Colors.GREEN}pysql>{Colors.RESET} "
    continuation = f"{Colors.DIM}   ...>{Colors.RESET} "

    try:
        while True:
            try:
                current_prompt = continuation if buffer else prompt
                line = input(current_prompt)
            except EOFError:
                print()
                break

            stripped = line.strip()

            # Handle empty input
            if not stripped and not buffer:
                continue

            # Handle dot-commands (meta-commands)
            if not buffer and stripped.startswith("."):
                _handle_meta_command(stripped, db)
                continue

            # Accumulate SQL
            buffer += " " + line if buffer else line

            # Check if statement is complete (ends with semicolon)
            if buffer.rstrip().endswith(";"):
                sql = buffer.rstrip().rstrip(";")
                buffer = ""

                if not sql.strip():
                    continue

                _execute_sql(db, sql)
            elif not buffer.strip():
                buffer = ""

    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Interrupted. Use .quit to exit.{Colors.RESET}")
    finally:
        db.close()
        print(f"\n{Colors.DIM}Goodbye!{Colors.RESET}")


def _handle_meta_command(command: str, db: Database) -> None:
    """Handle dot-commands (meta-commands like .tables, .help, etc.)."""
    parts = command.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in (".quit", ".exit", ".q"):
        db.close()
        print(f"{Colors.DIM}Goodbye!{Colors.RESET}")
        sys.exit(0)

    elif cmd == ".help":
        print(HELP_TEXT)

    elif cmd == ".tables":
        tables = db._catalog.list_tables()
        if tables:
            print(f"\n{Colors.BOLD}Tables:{Colors.RESET}")
            for t in tables:
                info = db._catalog.get_table(t)
                cols = len(info.columns) if info else 0
                rows = info.row_count_estimate if info else 0
                print(f"  {Colors.CYAN}{t}{Colors.RESET} ({cols} columns, ~{rows} rows)")
            print()
        else:
            print(f"{Colors.DIM}No tables found.{Colors.RESET}\n")

    elif cmd == ".schema":
        if not arg:
            print(f"{Colors.RED}Usage: .schema <table_name>{Colors.RESET}")
            return

        info = db._catalog.get_table(arg)
        if info is None:
            print(f"{Colors.RED}Table '{arg}' not found.{Colors.RESET}")
            return

        print(f"\n{Colors.BOLD}CREATE TABLE {arg} ({Colors.RESET}")
        for i, col in enumerate(info.columns):
            constraints_str = ""
            if col.get("constraints"):
                constraints_str = " " + " ".join(col["constraints"])
            comma = "," if i < len(info.columns) - 1 else ""
            print(f"  {Colors.CYAN}{col['name']}{Colors.RESET} "
                  f"{col['type']}{constraints_str}{comma}")
        print(f"{Colors.BOLD});{Colors.RESET}\n")

    elif cmd == ".stats":
        stats = db.get_stats()
        print(f"\n{Colors.BOLD}Database Statistics:{Colors.RESET}")
        print(f"  Tables: {', '.join(stats['tables']) or 'none'}")
        bp = stats['buffer_pool']
        print(f"  Buffer Pool: {bp['pages_in_pool']}/{bp['pool_size']} pages, "
              f"hit rate: {bp['hit_rate']}")
        print(f"  Disk Pages: {stats['disk']['total_pages']}")
        mvcc = stats['mvcc']
        print(f"  MVCC: {mvcc['active_transactions']} active, "
              f"{mvcc['committed_transactions']} committed txns")
        print(f"  WAL: next LSN={stats['wal']['next_lsn']}, "
              f"flushed={stats['wal']['flushed_lsn']}\n")

    elif cmd == ".explain":
        if not arg:
            print(f"{Colors.RED}Usage: .explain <SQL statement>{Colors.RESET}")
            return
        try:
            plan = db.explain(arg)
            print(f"\n{Colors.BOLD}Query Plan:{Colors.RESET}")
            print(plan)
        except Exception as e:
            print(f"{Colors.RED}Error: {e}{Colors.RESET}")

    elif cmd == ".indexes":
        if not arg:
            print(f"{Colors.RED}Usage: .indexes <table_name>{Colors.RESET}")
            return
        indexes = db._catalog.get_indexes_for_table(arg)
        if indexes:
            print(f"\n{Colors.BOLD}Indexes on {arg}:{Colors.RESET}")
            for idx in indexes:
                unique_str = "UNIQUE " if idx.is_unique else ""
                primary_str = " (PRIMARY KEY)" if idx.is_primary else ""
                print(f"  {Colors.CYAN}{idx.name}{Colors.RESET}: "
                      f"{unique_str}({', '.join(idx.columns)}){primary_str}")
            print()
        else:
            print(f"{Colors.DIM}No indexes found for '{arg}'.{Colors.RESET}\n")

    else:
        print(f"{Colors.RED}Unknown command: {cmd}. Type .help for help.{Colors.RESET}")


def _execute_sql(db: Database, sql: str) -> None:
    """Execute a SQL statement and print the result."""
    start_time = time.perf_counter()

    try:
        result = db.execute(sql)
        elapsed = time.perf_counter() - start_time

        if result.columns and result.rows:
            print(result.to_table_string())
        elif result.message:
            print(f"{Colors.GREEN}{result.message}{Colors.RESET}")
        elif result.affected_rows > 0:
            print(f"{Colors.GREEN}{result.affected_rows} row(s) affected{Colors.RESET}")
        elif result.columns:
            print(result.to_table_string())  # Empty result with headers

        print(f"{Colors.DIM}Time: {elapsed*1000:.2f}ms{Colors.RESET}\n")

    except Exception as e:
        print(f"{Colors.RED}Error: {e}{Colors.RESET}\n")


if __name__ == "__main__":
    main()
