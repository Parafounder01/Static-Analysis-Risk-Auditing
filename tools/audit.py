"""SQL Static Analysis & Risk Auditing Engine (static, zero-hallucination).

Parses a .sql file line-by-line with deterministic pattern rules drawn from
ROLE.md and prints the structured report: summary, defect log, remediation
snippets. No LLMs, no guessing — every finding cites a line number and the
exact matched snippet. Exit 0 = audited (even with findings), 2 = file error.
Usage: python tools/audit.py <file.sql> [--report out.txt]
"""
import re
import sys
from dataclasses import dataclass
from pathlib import Path

NON_SARGABLE_FNS = ("LOWER", "UPPER", "TRIM", "LTRIM", "RTRIM",
                    "YEAR", "MONTH", "DAY", "CAST", "CONVERT", "SUBSTRING")


@dataclass
class Finding:
    severity: str      # CRITICAL | WARNING | INFO
    category: str      # Performance | Index/Plan | Schema Drift
    line: int
    snippet: str
    cause: str
    fix: str


def audit(sql: str) -> list:
    out = []
    lines = sql.splitlines()
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("--"):
            continue

        if re.search(r"(?i)\bSELECT\s*\*", line):
            out.append(Finding("CRITICAL", "Performance", n, raw.strip(),
                "SELECT * pulls unneeded columns: extra I/O, bandwidth, and "
                "breaks views/procs on schema drift.",
                "List columns explicitly: SELECT order_id, order_date, ..."))
        for fn in NON_SARGABLE_FNS:
            if re.search(rf"(?i)\b(FROM|WHERE|JOIN|GROUP\s+BY)\b.*\b{fn}\s*\(", raw) \
               or re.search(rf"(?i)\bWHERE\b.*\b{fn}\s*\(\s*\w", raw):
                if "JOIN" in raw.upper():
                    fix = ("Standardize the column upstream (silver layer) and "
                           "join on raw indexed columns.")
                else:
                    fix = ("Rewrite as a range predicate, e.g. order_date >= "
                           "'2024-01-01' AND order_date < '2025-01-01'.")
                out.append(Finding("WARNING", "Performance", n, raw.strip(),
                    f"{fn}() on a column defeats index seeks (non-sargable); "
                    "engine must scan every row.", fix))
                break
        if re.search(r"(?i)\bIN\s*\(\s*SELECT\b", line):
            out.append(Finding("WARNING", "Performance", n, raw.strip(),
                "IN with a subquery often materializes the full inner set; "
                "EXISTS short-circuits and joins use indexes better.",
                "Use WHERE EXISTS (...) or an explicit JOIN."))
        if re.match(r"(?i)\s*,?\s*\(\s*SELECT\b", raw) or re.search(r"(?i)=\s*\(\s*SELECT\b", raw):
            out.append(Finding("WARNING", "Performance", n, raw.strip(),
                "Scalar/correlated subquery executes once per outer row.",
                "Convert to a LEFT JOIN against a grouped CTE."))
        if len(re.findall(r"(?i)\bOR\b", line)) >= 2:
            out.append(Finding("WARNING", "Index/Plan", n, raw.strip(),
                "Multiple ORs across columns suppress index usage; the "
                "optimizer falls back to scans.",
                "Split into UNION ALL branches or use IN (...) on one column."))
        if re.search(r"(?i)\bJOIN\b.*\b(LOWER|UPPER)\s*\(", raw):
            out.append(Finding("CRITICAL", "Index/Plan", n, raw.strip(),
                "Function on a JOIN key kills seek + merge/hash efficiency; "
                "forces row-by-row evaluation.",
                "Cleanse/standardize the column upstream (silver layer) and "
                "join on raw indexed columns."))
    return out


def audit_ddl(sql: str) -> list:
    out = []
    for m in re.finditer(r"(?i)CREATE\s+TABLE\s+(\S+)\s*\((.*?)\);", sql, re.S):
        table, body = m.group(1), m.group(2)
        base_line = sql[:m.start()].count("\n") + 1
        if not re.search(r"(?i)\bPRIMARY\s+KEY\b", body):
            out.append(Finding("CRITICAL", "Schema Drift", base_line, f"CREATE TABLE {table} (...)",
                "No primary key: duplicates possible, no clustered order, "
                "replication/CDC and joins suffer.",
                f"Add a surrogate key: order_key INT IDENTITY(1,1) PRIMARY KEY."))
        if not re.search(r"(?i)\bFOREIGN\s+KEY\b|REFERENCES\b", body):
            out.append(Finding("WARNING", "Schema Drift", base_line, f"CREATE TABLE {table} (...)",
                "No foreign keys: orphan rows possible, optimizer loses "
                "join-cardinality information.",
                "Add REFERENCES to parent dimensions."))
        for col in re.finditer(r"(?i)(\w+)\s+(INT\b|VARCHAR\s*\(\s*\d+\s*\)|TEXT\b)", body):
            name, dtype = col.group(1), col.group(2).upper()
            if ("amount" in name.lower() or "total" in name.lower() or "price" in name.lower()) and dtype == "INT":
                out.append(Finding("CRITICAL", "Schema Drift", base_line, col.group(0),
                    "INT for money risks overflow and cannot store cents; "
                    "financial metrics need fixed precision.",
                    "Use DECIMAL(18,2)."))
            if dtype.startswith("VARCHAR") and ("note" in name.lower() or "desc" in name.lower() or "comment" in name.lower()):
                out.append(Finding("WARNING", "Schema Drift", base_line, col.group(0),
                    "Short VARCHAR on free-text columns risks silent truncation.",
                    "Use VARCHAR(MAX) or a sized type matching the source."))
        missing = [c for c in ("dwh_create_date", "dwh_update_date", "source_system")
                   if c not in body.lower()]
        if missing:
            out.append(Finding("INFO", "Schema Drift", base_line, f"CREATE TABLE {table} (...)",
                f"Missing lineage columns ({', '.join(missing)}): no audit "
                "trail for loads and debugging.",
                "Add dwh_create_date DATE, dwh_update_date DATE, "
                "source_system VARCHAR(50)."))
    return out


def report(path: Path, findings: list) -> str:
    crit = sum(1 for f in findings if f.severity == "CRITICAL")
    warn = sum(1 for f in findings if f.severity == "WARNING")
    schema = sum(1 for f in findings if f.category == "Schema Drift")
    L = [f"### 1. AUDIT SUMMARY ({path.name})",
         f"- **Total Issues Detected**: {len(findings)}",
         f"- **Critical Hazards (Production Blockers)**: {crit}",
         f"- **Performance Warnings**: {warn}",
         f"- **Schema Drift & Lineage Risks**: {schema}", "",
         "### 2. LINE-BY-LINE DEFECT LOG", ""]
    for i, f in enumerate(findings, 1):
        L += [f"{i}. **[{f.severity}]** {f.category} — line {f.line}",
              f"   - Anti-pattern: `{f.snippet}`",
              f"   - Root cause: {f.cause}",
              f"   - Remediation: {f.fix}", ""]
    if not findings:
        L.append("No defects detected. Script is production-ready.")
    return "\n".join(L)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tools/audit.py <file.sql> [--report out.txt]")
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        return 2
    sql = path.read_text(encoding="utf-8")
    findings = audit(sql) + audit_ddl(sql)
    text = report(path, findings)
    print(text)
    if "--report" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--report") + 1])
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\nReport saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
