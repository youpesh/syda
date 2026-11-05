"""Turn a pasted CREATE TABLE / ALTER TABLE script into Syda schema dicts. Parse-only: nothing is executed.

Output matches DatabaseSchemaLoader.load_schemas(), so pasted DDL and a live connection are interchangeable.
"""
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

DIALECTS = {"PostgreSQL": "postgres", "MySQL": "mysql", "Oracle": "oracle", "SQL Server": "tsql"}
# Oracle exports (DBMS_METADATA, SQL Developer) put these inside the column list; sqlglot rejects them.
ORACLE_NOISE = {"ENABLE", "DISABLE", "VALIDATE", "NOVALIDATE", "RELY", "NORELY"}
T = exp.DataType.Type


def _column_type(kind: exp.DataType | None, read: str) -> str:
    if kind is None:
        return "string"
    if kind.this in (T.BOOLEAN, T.BIT):
        return "boolean"
    if kind.this in exp.DataType.INTEGER_TYPES:
        return "integer"
    if kind.this in exp.DataType.REAL_TYPES:
        params = [p.name for p in kind.expressions]
        # Scale 0 means whole numbers: DECIMAL(10), NUMBER(10,0), and Oracle's bare NUMBER.
        whole = len(params) == 1 or (len(params) == 2 and params[1] == "0") or (not params and read == "oracle")
        return "integer" if kind.this == T.DECIMAL and whole else "float"
    if kind.this in exp.DataType.TEMPORAL_TYPES:
        return "date"  # same mapping as DatabaseSchemaLoader
    return "string"


def _table_definition(sql: str, dialect: sqlglot.Dialect, oracle: bool) -> str:
    """Keep `CREATE TABLE name (columns)` and drop the physical clauses sqlglot can't parse
    (SQL Server TEXTIMAGE_ON, Oracle SEGMENT CREATION / STORAGE / USING INDEX, MySQL ENGINE=...)."""
    tokens = dialect.tokenize(sql)
    if not tokens or tokens[0].token_type != TokenType.CREATE:
        return sql
    cuts, depth, skip_from, seen_table = [], 0, None, False
    for i, tok in enumerate(tokens):
        kind = tok.token_type
        seen_table = seen_table or kind == TokenType.TABLE
        if skip_from is not None and depth == 1 and kind in (TokenType.COMMA, TokenType.R_PAREN):
            cuts.append((skip_from, tok.start))
            skip_from = None
        if kind == TokenType.L_PAREN:
            if not seen_table:
                return sql
            depth += 1
        elif kind == TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                end = tok.end + 1
                break
        elif oracle and depth == 1 and skip_from is None and kind not in (TokenType.IDENTIFIER, TokenType.STRING):
            word = tok.text.upper()
            if word == "USING" and i + 1 < len(tokens) and tokens[i + 1].text.upper() == "INDEX":
                skip_from = tok.start
            elif word in ORACLE_NOISE:
                cuts.append((tok.start, tok.end + 1))
    else:
        return sql
    out = sql[:end]
    for start, stop in reversed(cuts):
        out = out[:start] + out[stop:]
    return out


def _statements(ddl: str, read: str):
    """Parse statement by statement so one unsupported statement (a procedure, a GRANT) doesn't sink the rest."""
    dialect = sqlglot.Dialect.get_or_raise(read)
    ddl = re.sub(r"(?im)^\s*(GO|/)\s*$", ";", ddl)  # SQL Server batch separator, SQL*Plus terminator
    start = 0
    for tok in dialect.tokenize(ddl) + [None]:
        if tok is not None and tok.token_type != TokenType.SEMICOLON:
            continue
        chunk, start = ddl[start:tok.start if tok else len(ddl)], (tok.end + 1 if tok else start)
        if not chunk.strip():
            continue
        try:
            yield sqlglot.parse_one(_table_definition(chunk, dialect, read == "oracle"), read=read)
        except SqlglotError:
            continue  # ponytail: silently skip non-table DDL; report skipped count if users get confused


def _names(node: exp.Expression) -> list[str]:
    return [i.name for e in node.expressions if (i := e.find(exp.Identifier))]


def schemas_from_ddl(ddl: str, dialect: str) -> dict[str, dict]:
    """Raises SqlglotError when the script can't even be tokenized (e.g. an unterminated string)."""
    read = DIALECTS[dialect]
    tables: dict[str, tuple[str, dict[str, str]]] = {}  # lower name -> (name, {column: syda type})
    pk, unique, not_null = set(), set(), set()  # {(lower table, lower column)}
    fks: dict[tuple, tuple[str, str | None]] = {}
    enums: dict[tuple, list[str]] = {}

    for statement in _statements(ddl, read):
        if isinstance(statement, exp.Create) and statement.args.get("kind") == "TABLE" and isinstance(statement.this, exp.Schema):
            table = statement.this.this.name
            columns = {}
            for col in statement.this.expressions:
                if not isinstance(col, exp.ColumnDef):
                    continue
                key = (table.lower(), col.name.lower())
                columns[col.name] = _column_type(col.kind, read)
                if col.kind is not None and col.kind.this == T.ENUM:
                    enums[key] = [lit.this for lit in col.kind.find_all(exp.Literal) if lit.is_string]
                for constraint in col.constraints:
                    c = constraint.kind
                    if isinstance(c, exp.PrimaryKeyColumnConstraint):
                        pk.add(key)
                    elif isinstance(c, exp.NotNullColumnConstraint) and not c.args.get("allow_null"):
                        not_null.add(key)
                    elif isinstance(c, exp.UniqueColumnConstraint):
                        unique.add(key)
                    elif isinstance(c, exp.Reference):
                        ref = c.this
                        fks[key] = (ref.this.name, (_names(ref) or [None])[0]) if isinstance(ref, exp.Schema) else (ref.name, None)
            tables[table.lower()] = (table, columns)
        elif isinstance(statement, exp.Alter) and statement.args.get("kind") == "TABLE":
            table = statement.this.name
        else:
            continue

        # Table-level constraints, from CREATE TABLE bodies and ALTER TABLE ... ADD CONSTRAINT alike.
        t = table.lower()
        for node in statement.find_all(exp.PrimaryKey, exp.ForeignKey, exp.UniqueColumnConstraint, exp.CheckColumnConstraint,
                                       exp.ClusteredColumnConstraint, exp.NonClusteredColumnConstraint):
            if isinstance(node, exp.PrimaryKey):
                pk.update((t, c.lower()) for c in _names(node))
            elif isinstance(node, exp.ForeignKey):
                ref = node.args.get("reference") and node.args["reference"].this
                if ref is None:
                    continue
                target, target_cols = (ref.this.name, _names(ref)) if isinstance(ref, exp.Schema) else (ref.name, [])
                for i, c in enumerate(_names(node)):
                    fks[(t, c.lower())] = (target, target_cols[i] if i < len(target_cols) else None)
            elif isinstance(node, exp.UniqueColumnConstraint):
                if isinstance(node.this, exp.Schema) and len(node.this.expressions) == 1:  # single-column only, like the DB loader
                    unique.add((t, _names(node.this)[0].lower()))
            elif isinstance(node, (exp.ClusteredColumnConstraint, exp.NonClusteredColumnConstraint)):
                # T-SQL `CONSTRAINT x PRIMARY KEY|UNIQUE [NON]CLUSTERED (cols)` keeps the columns here, beside (PK) or under (UNIQUE) the marker.
                cols = [i.name.lower() for o in node.this if (i := o.find(exp.Identifier))]
                siblings = {type(e) for e in node.parent.expressions} if isinstance(node.parent, exp.Constraint) else {type(node.parent)}
                if exp.PrimaryKeyColumnConstraint in siblings:
                    pk.update((t, col) for col in cols)
                elif exp.UniqueColumnConstraint in siblings and len(cols) == 1:
                    unique.add((t, cols[0]))
            elif node.find(exp.In, exp.Any) and (column := node.find(exp.Column)):
                # col IN ('a','b') or PostgreSQL's col = ANY (ARRAY['a'::text, ...])
                values = [lit.this for lit in node.find_all(exp.Literal) if lit.is_string]
                if values:
                    enums[(t, column.name.lower())] = values

    result = {}
    for t, (table, columns) in tables.items():
        schema = {}
        for column, kind in columns.items():
            key = (t, column.lower())
            target = fks.get(key)
            if target and target[0].lower() in tables:
                ref_table, ref_columns = tables[target[0].lower()]
                by_lower = {c.lower(): c for c in ref_columns}
                ref_pk = [c for c in ref_columns if (target[0].lower(), c.lower()) in pk]
                field = by_lower.get((target[1] or "").lower()) or (ref_pk[0] if ref_pk else "id")
                col_def = {"type": "foreign_key", "references": {"schema": ref_table, "field": field}}
            else:  # ponytail: FK to a table outside the paste becomes a plain column
                col_def = {"type": kind}
            if key in pk:
                col_def.update(primary_key=True, not_null=True)
            else:
                if key in unique:
                    col_def["unique"] = True
                if key in not_null:
                    col_def["not_null"] = True
                if key in enums:
                    col_def["enum"] = enums[key]
            schema[column] = col_def
        result[table] = schema
    return result
