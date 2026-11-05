"""Exports preserve destination constraints and roll back the whole dataset."""
import pandas as pd
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from syda.db_schema_loader import DatabaseSchemaLoader


@pytest.fixture
def destination():
    engine = create_engine('sqlite://')
    @event.listens_for(engine, 'connect')
    def enable_foreign_keys(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent(id))')
    yield engine
    engine.dispose()


def count(engine, table):
    with engine.connect() as conn:
        return conn.scalar(text(f'SELECT COUNT(*) FROM {table}'))


def test_failed_child_insert_rolls_back_earlier_tables_and_batches(destination):
    # Two batches are written to parent before the invalid child is attempted.
    data = {'parent': pd.DataFrame({'id': range(1, 1002)}),
            'child': pd.DataFrame({'id': [1], 'parent_id': [9999]})}
    with pytest.raises((SQLAlchemyError, pd.errors.DatabaseError)):
        DatabaseSchemaLoader(destination).write_to_database(data)
    assert count(destination, 'parent') == count(destination, 'child') == 0


def test_replace_preserves_constraints_and_inserts_in_foreign_key_order(destination):
    with destination.begin() as conn:
        conn.exec_driver_sql('INSERT INTO parent VALUES (1)')
        conn.exec_driver_sql('INSERT INTO child VALUES (1, 1)')
    # Deliberately pass children first.
    DatabaseSchemaLoader(destination).write_to_database({
        'child': pd.DataFrame({'id': [2], 'parent_id': [2]}),
        'parent': pd.DataFrame({'id': [2]}),
    }, if_exists='replace')
    assert inspect(destination).get_foreign_keys('child')
    assert inspect(destination).get_pk_constraint('parent')['constrained_columns'] == ['id']
    with destination.connect() as conn:
        assert conn.execute(text('SELECT id, parent_id FROM child')).all() == [(2, 2)]


def test_failed_replace_restores_original_dataset(destination):
    with destination.begin() as conn:
        conn.exec_driver_sql('INSERT INTO parent VALUES (1)')
        conn.exec_driver_sql('INSERT INTO child VALUES (1, 1)')
    with pytest.raises((SQLAlchemyError, pd.errors.DatabaseError)):
        DatabaseSchemaLoader(destination).write_to_database({
            'parent': pd.DataFrame({'id': [2]}),
            'child': pd.DataFrame({'id': [2], 'parent_id': [999]}),
        }, if_exists='replace')
    with destination.connect() as conn:
        assert conn.execute(text('SELECT id FROM parent')).all() == [(1,)]
        assert conn.execute(text('SELECT id, parent_id FROM child')).all() == [(1, 1)]


def test_unknown_column_is_rejected_before_any_table_is_written(destination):
    with pytest.raises(ValueError, match='unexpected'):
        DatabaseSchemaLoader(destination).write_to_database({
            'parent': pd.DataFrame({'id': [1]}),
            'child': pd.DataFrame({'id': [1], 'unexpected': [1]}),
        })
    assert count(destination, 'parent') == 0


def test_fail_rejects_existing_table_before_creating_other_targets(destination):
    with pytest.raises(ValueError, match='already exists'):
        DatabaseSchemaLoader(destination).write_to_database({
            'new_table': pd.DataFrame({'id': [1]}), 'parent': pd.DataFrame({'id': [1]}),
        }, if_exists='fail')
    assert 'new_table' not in inspect(destination).get_table_names()
